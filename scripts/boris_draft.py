"""Boris draft generator.

Turns recurring user corrections (from boris-candidates.json) into
ready-to-review CLAUDE.md rule drafts. Normally produces drafts only;
with --auto-apply the module applies drafts directly when trust tier >= 1.

Usage:
    python boris_draft.py
    python boris_draft.py --min-count 4
    python boris_draft.py --process-accepted
    python boris_draft.py --auto-apply
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_CANDIDATES = Path.home() / ".claude" / "logs" / "boris-candidates.json"
_DEFAULT_OUT_DIR = Path.home() / ".claude" / "logs" / "boris-drafts"

# ---------------------------------------------------------------------------
# Correction detector — filters out questions, system noise, and directives
# ---------------------------------------------------------------------------

# System noise prefixes — messages starting with these are never corrections.
# Kept in sync with incident_tracker._SYSTEM_NOISE_PREFIX.
_NOISE_PREFIXES: tuple[str, ...] = (
    "stop hook feedback",
    "<system-reminder>",
)

# Pure question starters (Bulgarian interrogatives without explicit negation).
# A message starting with these AND lacking a negation/error word is a question,
# not a correction. Checked case-insensitively after stripping leading whitespace.
_QUESTION_STARTERS: tuple[str, ...] = (
    "може ли",
    "дали ",
    "защо ",
    "какво ",
    "как ",
    "кой ",
    "кога ",
    "колко ",
    "къде ",
    "what ",
    "why ",
    "how ",
    "when ",
    "where ",
    "can ",
    "could ",
    "would ",
    "should ",
    "is ",
    "are ",
    "do ",
    "does ",
)

# Negation / error words: presence of ANY of these in the message makes it a
# correction even if it has a question starter or question mark.
_NEGATION_WORDS: frozenset[str] = frozenset({
    "не работи", "неработи", "не стартира", "не се отчете", "не показва",
    "не зарежда", "не се виждат", "не се вижда", "не дава",
    "не е ок", "не е вярно", "не е нормален", "не е нормално",
    "грешка", "проблем", "error", "bug", "не прави", "нищо не",
    "отново не", "пак не", "пак се", "отново се",
})


def _dedup_tokens(text: str) -> str:
    """Collapse runs of 3+ consecutive identical tokens (e.g. 'стоп стоп стоп').

    Consecutive duplicates beyond 2 repetitions are stripped.  This prevents
    frustrated user repetition from inflating token count and skewing the rule.

    Args:
        text: Raw message text.

    Returns:
        Text with triple-or-more consecutive token repetitions collapsed to 2.
    """
    tokens = text.split()
    if len(tokens) < 3:
        return text

    result: list[str] = []
    for tok in tokens:
        # Keep at most 2 consecutive identical tokens
        if len(result) >= 2 and result[-1] == tok and result[-2] == tok:
            continue
        result.append(tok)
    return " ".join(result)


def is_correction(message: str) -> bool:
    """Return True if *message* looks like a genuine user correction.

    Filters out:
    - System noise (Stop hook feedback, <system-reminder> blocks)
    - Pure questions (ending with '?' and no negation/error word)
    - Pure question starters without negation context
    - Empty / whitespace-only messages

    Args:
        message: Raw candidate message text.

    Returns:
        True if the message is a genuine correction that warrants a Boris rule.
    """
    if not message or not message.strip():
        return False

    text = message.strip()
    lower = text.lower()

    # 1. System noise — hard exclude
    for prefix in _NOISE_PREFIXES:
        if lower.startswith(prefix):
            return False

    # 2. Ends with '?' — candidate for question; may still be complaint
    ends_with_question = text.endswith("?")

    # 3. Check for negation/error words — these override question heuristics
    has_negation = any(neg in lower for neg in _NEGATION_WORDS)

    if ends_with_question and not has_negation:
        # Pure question — not a correction
        return False

    # 4. Starts with interrogative starter AND lacks negation → question
    for starter in _QUESTION_STARTERS:
        if lower.startswith(starter) and not has_negation:
            return False

    return True


def filter_corrections(examples: list[str]) -> list[str]:
    """Filter and de-duplicate a list of candidate messages.

    Applies is_correction() and token deduplication.  Preserves order.

    Args:
        examples: Raw candidate message list.

    Returns:
        Filtered list of genuine corrections (may be shorter than input).
    """
    seen: set[str] = set()
    result: list[str] = []
    for raw in examples:
        if not isinstance(raw, str) or not raw.strip():
            continue
        deduped = _dedup_tokens(raw.strip())
        if not is_correction(deduped):
            continue
        if deduped not in seen:
            seen.add(deduped)
            result.append(deduped)
    return result


# ---------------------------------------------------------------------------
# LLM rule synthesis
# ---------------------------------------------------------------------------

_RULE_SYNTHESIS_SYSTEM_PROMPT = (
    "You are a rule-synthesis assistant for an AI coding assistant's CLAUDE.md config. "
    "The user will give you 1-5 examples of corrections a human made to the AI's behaviour. "
    "Your job: synthesise ONE imperative rule in Bulgarian (max 140 characters) that an AI "
    "assistant should follow to avoid repeating the mistake. "
    "Requirements: "
    "(1) Output ONLY the rule text — no explanation, no markdown, no quotes. "
    "(2) The rule must be actionable and specific (not a restatement of the examples). "
    "(3) Start with an imperative verb (e.g. 'Винаги...', 'Не...', 'Преди...'). "
    "(4) Max 140 characters. "
    "If the examples are too vague to produce a specific rule, output exactly: "
    "NEEDS HUMAN REWRITE"
)

_RULE_MAX_CHARS = 140


def synthesize_rule_with_llm(
    examples: list[str],
    judge_text_fn=None,  # type: ignore[type-arg]
) -> str:
    """Call LLM to synthesize a single imperative rule from correction examples.

    Uses llm_judge.call_ollama_raw as the LLM transport (raw free-text, NOT
    judge_text — the synthesis prompt asks for plain rule text, which
    judge_text's {verdict:useful|junk} JSON parser would always reject).  If
    the LLM is unavailable or returns a bad result, falls back to a safe
    "NEEDS HUMAN REWRITE: <hint>" marker so that auto_apply_boris writes a
    human-readable placeholder instead of verbatim junk.

    Args:
        examples: Filtered correction examples to synthesize from.
        judge_text_fn: Injectable raw-LLM callable (for testing without
            network), same signature as llm_judge.call_ollama_raw. If None,
            imports call_ollama_raw from llm_judge at call time.

    Returns:
        Rule string (<=140 chars) or "NEEDS HUMAN REWRITE: <hint>" fallback.
    """
    if judge_text_fn is None:
        try:
            from llm_judge import call_ollama_raw as _call_ollama_raw
            judge_text_fn = _call_ollama_raw
        except ImportError:
            judge_text_fn = None

    # Build fallback hint (short summary from longest example)
    hint_text = max(examples, key=lambda x: len(x)) if examples else "no examples"
    hint = hint_text[:60].rsplit(" ", 1)[0] if len(hint_text) > 60 else hint_text
    fallback = f"NEEDS HUMAN REWRITE: {hint}"

    # No point calling LLM with zero examples
    if not examples:
        return fallback

    if judge_text_fn is None:
        return fallback

    user_text = "Correction examples:\n" + "\n".join(f"- {ex}" for ex in examples[:5])

    try:
        raw_rule = judge_text_fn(
            system_prompt=_RULE_SYNTHESIS_SYSTEM_PROMPT,
            user_text=user_text,
        )
    except Exception:
        return fallback

    # Tolerant of an injected judge_text_fn double still shaped like the old
    # {"verdict", "score", "reason"} dict (legacy judge_text contract) —
    # extract the free-text field. Real production path (call_ollama_raw)
    # always returns str | None.
    if isinstance(raw_rule, dict):
        verdict = raw_rule.get("verdict", "")
        reason = raw_rule.get("reason", "")
        raw_rule = verdict if verdict not in ("useful", "junk", "") else reason

    if not raw_rule:
        # LLM unavailable or returned empty content
        return fallback

    rule = raw_rule.strip()

    # Reject if it's just "NEEDS HUMAN REWRITE" from LLM (passthrough)
    if rule.upper().startswith("NEEDS HUMAN REWRITE"):
        return fallback

    # Reject if rule is a verbatim copy of (or contains) one of the examples —
    # checked BEFORE truncation so truncating can't dodge the echo check.
    if any(rule.strip() == ex.strip() or ex.strip() in rule for ex in examples):
        return fallback

    # Reject markdown-breaking / file-import-injection content: a fenced code
    # block, a leading '@' file-import token, or a leading markdown heading —
    # this rule text is spliced verbatim into CLAUDE.md's Learned Rules list.
    if "```" in rule:
        return fallback
    first_token = rule.split(None, 1)[0] if rule.split() else ""
    if first_token.startswith("@") or rule.lstrip().startswith("#"):
        return fallback

    # Truncate to max length AFTER the content-safety checks above.
    if len(rule) > _RULE_MAX_CHARS:
        rule = rule[:_RULE_MAX_CHARS].rsplit(" ", 1)[0]

    # Sanity: non-empty after trimming
    if not rule:
        return fallback

    return rule


# ---------------------------------------------------------------------------
# Negation prefixes to strip when building a rule hint (Bulgarian + English)
# ---------------------------------------------------------------------------

_NEG_PREFIXES: list[str] = [
    "не прави",
    "не така,",
    "не така",
    "не използвай",
    "не забравяй",
    "не го",
    "не е ок",
    "не е вярно",
    "не е",
    "не мога",
    "не се",
    "не пропускай",
    "не ",
    "wrong ",
    "no ",
    "stop ",
    "нищо не работи",
    "нищо ",
]


def summarize_corrections(examples: list[str]) -> str:
    """Condense correction examples into a short candidate rule line.

    Takes the most informative example (longest), strips leading negation
    words, and returns a lowercased imperative hint.  The result is a
    best-effort heuristic -- not a perfect rule -- so the human reviewer can
    edit it before approving.

    Note: This function operates on pre-filtered examples (use
    filter_corrections() upstream).  It does NOT call the LLM — that is done
    in generate_draft() with synthesize_rule_with_llm().

    Args:
        examples: List of raw correction strings from boris-candidates.json.

    Returns:
        A short, lowercased rule hint string.  Never empty.
    """
    # Coerce to clean list of strings (tolerate null / non-string elements)
    examples = [str(e) for e in (examples or []) if e is not None and str(e).strip()]
    if not examples:
        return "review corrections and define rule"

    # Pick the most informative example (longest non-empty string).
    candidate = max(examples, key=lambda x: len(x.strip()))
    text = candidate.strip().lower()

    # Strip known negation prefixes.
    for prefix in _NEG_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    # Trim to a sensible length so the rule hint stays readable.
    if len(text) > 120:
        # Cut at the last word boundary before 120 chars.
        cut = text[:120].rsplit(" ", 1)[0]
        text = cut + " ..."

    return text if text else "review corrections and define rule"


def _encode_to_path_hint(project_key: str) -> str:
    """Convert a boris-candidates project key to a human-readable path hint.

    Boris encodes paths as ``<Drive>--<Org>-<Project>``:
    - Leading ``X--`` maps to ``X:\\``
    - Double dashes ``--`` inside map to ``\\``
    - The first single dash after the org segment separates org from project,
      so ``J--Antigraviti-Facturka-bg``  -> ``{{CODE_PATH}}\\Facturka-bg``
      and ``J--Antigraviti-Davinci-Plugin-DCTL`` -> ``{{CODE_PATH}}\\Davinci-Plugin-DCTL``

    The heuristic: after stripping the drive prefix and expanding ``--`` to
    ``\\``, split each resulting path segment on its *first* single dash only
    to separate ``Org`` from ``Project``; single dashes inside the project
    name are preserved (e.g. ``Facturka-bg`` or ``Davinci-Plugin-DCTL``).

    Args:
        project_key: Raw key from boris-candidates.json.

    Returns:
        A Windows-style path hint string.
    """
    # Case-insensitive match for drive prefix "X--rest"
    drive_match = re.match(r"^([A-Za-z])--(.+)$", project_key)
    if not drive_match:
        return project_key

    drive = drive_match.group(1).upper()
    rest = drive_match.group(2)

    # Expand any remaining "--" (nested double-dash separators) to "\".
    rest = rest.replace("--", "\\")

    # For each path segment (split by "\"), further split on the FIRST
    # single dash to separate org from project name.
    # e.g. "Antigraviti-Facturka-bg"      -> ["Antigraviti", "Facturka-bg"]
    #      "Antigraviti-Davinci-Plugin-DCTL" -> ["Antigraviti", "Davinci-Plugin-DCTL"]
    #      "Obsidian-Resurch-Claude-Trading" -> ["Obsidian", "Resurch-Claude-Trading"]
    parts = rest.split("\\")
    expanded: list[str] = []
    for part in parts:
        dash_idx = part.find("-")
        if dash_idx > 0:
            expanded.append(part[:dash_idx])
            expanded.append(part[dash_idx + 1:])
        else:
            expanded.append(part)

    return drive + ":\\" + "\\".join(expanded)


def _resolve_real_project_dir(decoded_path: str) -> Path | None:
    """Resolve the *actual* project directory from a lossy path hint.

    The Boris encoding collapses space/dot/dash all to single dash, so a
    decoded path like ``{{CODE_PATH}}\\StroyOffice-Pro`` may correspond to
    the real folder ``StroyOffice Pro`` or ``StroyOffice.Pro``. Writing to
    the decoded path verbatim would create a *new* wrong-name folder and
    silently bypass the real project's CLAUDE.md.

    Strategy:
    1. If the decoded path exists as-is → return it (no ambiguity).
    2. Otherwise scan the parent for siblings whose normalized name (all
       of ``- . _ <space>`` → ``-``) equals the candidate's normalized form.
    3. Exactly one match → return it. Zero or multiple → return None
       (caller must refuse to write to avoid corrupting the wrong project).

    Returns None when resolution is ambiguous; caller MUST NOT fall back to
    creating a new folder at the lossy path.
    """
    p = Path(decoded_path)
    if p.exists():
        return p

    parent = p.parent
    if not parent.exists():
        # Parent dir itself unknown — cannot fuzzy match. Bail out.
        return None

    def _norm(name: str) -> str:
        return re.sub(r"[-._ ]+", "-", name).lower()

    target = _norm(p.name)
    candidates = [d for d in parent.iterdir() if d.is_dir() and _norm(d.name) == target]
    if len(candidates) == 1:
        return candidates[0]
    # 0 matches → project truly doesn't exist; multiple → genuinely ambiguous.
    return None


def _safe_filename(project_key: str) -> str:
    """Return a filesystem-safe filename stem from a project key.

    Strips characters that are invalid in Windows filenames.

    Args:
        project_key: Raw project key.

    Returns:
        A safe filename stem (no extension).
    """
    safe = re.sub(r'[<>:"/\\|?*]', "_", project_key)
    return safe


def generate_draft(
    project: str,
    info: dict,  # type: ignore[type-arg]
    judge_text_fn=None,  # type: ignore[type-arg]
) -> str:
    """Return a markdown draft string for a single project.

    The draft contains:
    - Project name
    - Proposed rule (LLM-synthesized from filtered corrections, or safe fallback)
    - Verbatim evidence (all examples, including filtered-out ones for audit)
    - Target CLAUDE.md path hint

    The "Proposed Rule" fenced code block is what --auto-apply reads and writes
    into CLAUDE.md.  It will contain either a proper imperative rule (<=140 chars)
    or "NEEDS HUMAN REWRITE: <hint>" — never verbatim junk from the examples.

    Args:
        project: Project key (as stored in boris-candidates.json).
        info: Dict with keys ``count`` (int) and ``examples`` (list[str]).
        judge_text_fn: Injectable LLM callable for testing (default: auto-import).

    Returns:
        A markdown string ready to save as a draft file.
    """
    raw_examples = info.get("examples") or []
    # Tolerate null / non-string elements in examples
    all_examples: list[str] = [str(e) for e in raw_examples if e is not None and str(e).strip()]
    try:
        count: int = int(info.get("count", len(all_examples)))
    except (TypeError, ValueError):
        count = len(all_examples)

    # Filter to genuine corrections before synthesis
    genuine = filter_corrections(all_examples)

    # LLM synthesizes the rule from filtered corrections; falls back to safe marker
    if genuine:
        rule_hint = synthesize_rule_with_llm(genuine, judge_text_fn=judge_text_fn)
    else:
        # No genuine corrections after filtering — still produce draft for audit
        rule_hint = "NEEDS HUMAN REWRITE: no genuine corrections detected after filtering"

    path_hint = _encode_to_path_hint(project)
    target_path = f"{path_hint}\\CLAUDE.md"

    evidence_lines = "\n".join(f"- {ex}" for ex in all_examples)
    filtered_note = (
        f"\n_(Filtered to {len(genuine)}/{len(all_examples)} genuine corrections before synthesis.)_\n"
        if len(genuine) != len(all_examples) else ""
    )

    draft = f"""\
# Boris Draft -- {project}

> **STATUS: PENDING HUMAN REVIEW**
> Do NOT apply this rule without reading and editing it first.

## Proposed Rule

```
{rule_hint}
```

## Target File

`{target_path}`

_(If the path looks wrong due to key encoding, adjust manually.)_

## Evidence

Correction count in window: **{count}**
{filtered_note}
Examples (verbatim):

{evidence_lines}

## Instructions

1. Edit the proposed rule above to match project conventions.
2. Copy the final rule line into `{target_path}`.
3. Delete this draft once applied (or keep for audit trail).
"""
    return draft


def write_drafts(
    boris_path: Path,
    out_dir: Path,
    min_count: int = 4,
    judge_text_fn=None,
) -> list[Path]:
    """Write draft files for all projects meeting the count threshold.

    Args:
        boris_path: Path to boris-candidates.json.
        out_dir: Directory to write ``{project}.md`` drafts into.
        min_count: Minimum correction count to generate a draft (inclusive).
        judge_text_fn: Injectable LLM transport, forwarded to generate_draft.
            Default None → generate_draft auto-imports the real llm_judge. Tests
            inject a stub so they never touch the network.

    Returns:
        List of Paths that were actually written.  Empty list on any error
        reading the input file.
    """
    try:
        raw = boris_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, dict):
        return []
    projects = data.get("projects") or {}
    if not isinstance(projects, dict):
        return []
    written: list[Path] = []

    def _safe_count(val: dict) -> int:
        try:
            return int(val.get("count", 0))
        except (TypeError, ValueError):
            return 0

    qualifying = {
        key: val for key, val in projects.items()
        if isinstance(val, dict) and _safe_count(val) >= min_count
    }

    if not qualifying:
        return []

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return []

    for project_key, info in qualifying.items():
        content = generate_draft(project_key, info, judge_text_fn=judge_text_fn)
        safe_name = _safe_filename(project_key)
        dest = out_dir / f"{safe_name}.md"
        try:
            dest.write_text(content, encoding="utf-8")
            written.append(dest)
        except OSError:
            continue

    return written


_DEFAULT_ACCEPTED_BORIS = Path.home() / ".claude" / "logs" / "accepted-boris.json"


def _extract_proposed_rule(draft_text: str) -> str | None:
    """Extract rule text from the first fenced code block after '## Proposed Rule'.

    Args:
        draft_text: Full markdown content of a boris draft file.

    Returns:
        The stripped rule string, or None if the block cannot be found.
    """
    # Find the ## Proposed Rule section first
    section_match = re.search(r"^## Proposed Rule\s*$", draft_text, re.MULTILINE)
    if not section_match:
        return None

    # Search for the first fenced code block (``` ... ```) after that position
    after = draft_text[section_match.end():]
    block_match = re.search(r"```[^\n]*\n(.*?)```", after, re.DOTALL)
    if not block_match:
        return None

    return block_match.group(1).strip()


def process_accepted_boris(
    accepted_path: Path = _DEFAULT_ACCEPTED_BORIS,
    drafts_dir: Path = _DEFAULT_OUT_DIR,
) -> list[str]:
    """Apply accepted boris rules to their target CLAUDE.md files.

    For each item_id in accepted-boris.json:
    1. Strip "boris_rule-" prefix to get project_key.
    2. Find draft file: drafts_dir / _safe_filename(project_key).md
    3. Extract rule text from the "## Proposed Rule" code block.
    4. Decode CLAUDE.md path using _encode_to_path_hint(project_key).
    5. If CLAUDE.md exists: backup it, then append rule under ## Learned Rules.
    6. If CLAUDE.md missing: create it with the rule.
    7. After processing all: write [] to accepted_path (clear queue).

    Args:
        accepted_path: Path to accepted-boris.json queue file.
        drafts_dir: Directory containing boris draft .md files.

    Returns:
        List of item_ids successfully applied.  Never raises — logs errors and
        continues on per-item failures.
    """
    # Load the queue
    try:
        raw = accepted_path.read_text(encoding="utf-8")
        queue: list[str] = json.loads(raw)
    except FileNotFoundError:
        return []
    except Exception as exc:
        log.error("process_accepted_boris: failed to read queue %s: %s", accepted_path, exc)
        return []

    if not isinstance(queue, list) or not queue:
        return []

    applied: list[str] = []

    for item_id in queue:
        try:
            # Strip the queue prefix to get the project key. The live queue
            # (self_improvement_queue._load_boris) emits "boris-<proj>" ids;
            # "boris_rule-" is kept for backward compatibility with older items.
            if item_id.startswith("boris_rule-"):
                project_key = item_id[len("boris_rule-"):]
            elif item_id.startswith("boris-"):
                project_key = item_id[len("boris-"):]
            else:
                project_key = item_id

            # Locate draft file
            draft_file = drafts_dir / (_safe_filename(project_key) + ".md")
            if not draft_file.exists():
                log.warning("process_accepted_boris: draft not found for %s at %s", item_id, draft_file)
                continue

            draft_text = draft_file.read_text(encoding="utf-8")
            rule_text = _extract_proposed_rule(draft_text)
            if not rule_text:
                log.warning("process_accepted_boris: could not extract rule from %s", draft_file)
                continue

            # Never write an un-synthesized placeholder into CLAUDE.md (mirrors
            # the same guard in auto_apply_boris). A human "accepting" an item
            # from the queue may not have opened the actual draft — the queue
            # label ("useful", high score) is derived from correction count,
            # not from whether LLM synthesis actually produced a real rule.
            if rule_text.strip().upper().startswith("NEEDS HUMAN REWRITE"):
                log.warning(
                    "process_accepted_boris: skipping %s — rule is a "
                    "NEEDS-HUMAN-REWRITE placeholder, not a synthesized rule",
                    project_key,
                )
                continue

            # Resolve CLAUDE.md path via path hint — fuzzy match against real
            # directories to handle space/dot/dash ambiguity in the encoding.
            path_hint = _encode_to_path_hint(project_key)
            real_dir = _resolve_real_project_dir(path_hint)
            if real_dir is None:
                # Refuse to write: would either create a wrong-name folder
                # (e.g. "StroyOffice-Pro" instead of "StroyOffice Pro") or
                # patch the wrong project's CLAUDE.md. Better to skip and
                # leave the draft for manual review.
                log.warning(
                    "process_accepted_boris: cannot resolve real directory for "
                    "project_key=%s (decoded hint=%s). Skipping — apply the "
                    "rule manually from %s",
                    project_key, path_hint, draft_file,
                )
                continue
            claude_md = real_dir / "CLAUDE.md"

            # Wrap in the same <!-- auto-applied:... --> markers auto_apply_boris
            # uses, so trust_tiers.rollback_last's marker-strip regex can find
            # and undo this entry too (previously this path wrote a bare "- rule"
            # line with no marker, making the recorded ledger rollback_marker a
            # silent no-op for every human-accepted rule).
            ts_str = datetime.now(timezone.utc).isoformat()
            open_marker = f"<!-- auto-applied:{item_id} tier:1 {ts_str} -->"
            close_marker = f"<!-- /auto-applied:{item_id} -->"
            block = f"{open_marker}\n- {rule_text}\n{close_marker}\n"

            if claude_md.exists():
                content = claude_md.read_text(encoding="utf-8")

                # Find ## Learned Rules section and append after its last line
                section_match = re.search(
                    r"(^## Learned Rules\s*\n)(.*?)(?=\n## |\Z)",
                    content,
                    re.MULTILINE | re.DOTALL,
                )
                if section_match:
                    # Append the rule line at the end of the section block
                    insert_pos = section_match.end()
                    content = content[:insert_pos] + block + content[insert_pos:]
                else:
                    # No ## Learned Rules section — append one at end of file
                    if not content.endswith("\n"):
                        content += "\n"
                    content += f"\n## Learned Rules\n{block}"

                claude_md.write_text(content, encoding="utf-8")
            else:
                # Create a minimal CLAUDE.md with the rule
                claude_md.parent.mkdir(parents=True, exist_ok=True)
                claude_md.write_text(
                    f"# Project Rules\n\n## Learned Rules\n{block}",
                    encoding="utf-8",
                )

            applied.append(item_id)
            log.info("process_accepted_boris: applied '%s' -> %s", rule_text[:60], claude_md)

            # Record in applied-ledger.jsonl so the apply-funnel KPI
            # (outcome_kpi._compute_apply_funnel) sees human-accepted applies,
            # not only auto-applied ones. Without this the ledger stays empty
            # even after real applies → apply_funnel reports ledger_missing
            # forever (the 2026-06 funnel bug: 3 rules accepted+applied on 06-11,
            # yet applied-ledger.jsonl never existed).
            try:
                from trust_tiers import record_application
                record_application({
                    "item_id": item_id,
                    "item_type": "boris_rule",
                    "tier": 1,  # human review = trusted decision
                    "target_file": str(claude_md),
                    "rollback_marker": item_id,
                    "source": "human_accept",
                })
            except Exception as ledger_exc:  # a ledger hiccup must never fail the apply
                log.warning("process_accepted_boris: ledger write skipped for %s: %s", item_id, ledger_exc)

        except Exception as exc:
            log.error("process_accepted_boris: error processing %s: %s", item_id, exc)
            continue

    # Clear the queue regardless of partial failures
    try:
        import os
        import tempfile
        accepted_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(accepted_path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump([], fh)
        os.replace(tmp, str(accepted_path))
    except Exception as exc:
        log.error("process_accepted_boris: failed to clear queue: %s", exc)

    return applied


_DEFAULT_LEDGER = Path.home() / ".claude" / "logs" / "applied-ledger.jsonl"
_DEFAULT_PENDING_REVIEW = Path.home() / ".claude" / "logs" / "auto-applied-pending-review.json"

# Maximum number of auto-applications per run (flood protection)
_AUTO_APPLY_MAX_PER_RUN = 2


def auto_apply_boris(
    drafts_dir: Path = _DEFAULT_OUT_DIR,
    ledger_path: Path | None = None,
    pending_review_path: Path = _DEFAULT_PENDING_REVIEW,
    effectiveness_path: Path | None = None,
    overrides_path: Path | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Auto-apply boris drafts whose trust tier is >= 1.

    ledger_path=None resolves _DEFAULT_LEDGER at CALL time so the suite-wide
    conftest ledger guard can redirect it (see trust_tiers.record_application).

    Scans *drafts_dir* for ``.md`` draft files.  For each draft that has not
    already been applied (deduplication via ledger) and whose tier >= 1, the
    rule is inserted into the target CLAUDE.md wrapped in auto-applied markers.
    At most :data:`_AUTO_APPLY_MAX_PER_RUN` drafts are applied per call.

    Tier 1 → also writes to pending-review.json (for daily_brief notification).
    Tier 2 → silent (ledger entry only).

    Args:
        drafts_dir: Directory containing boris draft ``.md`` files.
        ledger_path: Path to applied-ledger.jsonl for deduplication + rollback.
        pending_review_path: Path to auto-applied-pending-review.json.
        effectiveness_path: Override for effectiveness.json path (tests).
        overrides_path: Override for trust-overrides.json path (tests).
        now: Timestamp override for tests; defaults to UTC now.

    Returns:
        List of item_ids that were successfully applied this run.
    """
    # Import here to keep runtime import cost low and avoid circular deps
    import sys as _sys

    try:
        from trust_tiers import (
            get_tier,
            record_application,
            is_in_ledger,
            append_pending_review,
        )
    except ImportError:
        # trust_tiers.py not on path — skip silently (Tier 0 everywhere)
        print("[boris_draft] trust_tiers not available — auto-apply skipped", file=_sys.stderr)
        return []

    if ledger_path is None:
        ledger_path = _DEFAULT_LEDGER
    if now is None:
        now = datetime.now(timezone.utc)

    # Build kwargs for get_tier to support path injection in tests
    tier_kwargs: dict = {}
    if effectiveness_path is not None:
        tier_kwargs["effectiveness_path"] = effectiveness_path
    if overrides_path is not None:
        tier_kwargs["overrides_path"] = overrides_path

    tier = get_tier("boris_rule", **tier_kwargs)
    if tier == 0:
        return []

    if not drafts_dir.exists():
        return []

    draft_files = sorted(f for f in drafts_dir.iterdir() if f.is_file() and f.suffix == ".md")
    applied: list[str] = []

    for draft_file in draft_files:
        if len(applied) >= _AUTO_APPLY_MAX_PER_RUN:
            break

        # Derive project_key from filename stem (reverse of _safe_filename)
        project_key = draft_file.stem

        # item_id convention mirrors process_accepted_boris queue format
        item_id = f"auto-boris-{project_key}"

        # Deduplication: skip if already in ledger
        if is_in_ledger(item_id, ledger_path=ledger_path):
            continue

        try:
            draft_text = draft_file.read_text(encoding="utf-8")
        except OSError:
            continue

        rule_text = _extract_proposed_rule(draft_text)
        if not rule_text:
            continue

        # Never auto-write an un-synthesized placeholder into CLAUDE.md. When the
        # LLM was unavailable (or examples too vague) the rule is a "NEEDS HUMAN
        # REWRITE" marker — that must stay a draft for a human, not become a live
        # rule. Tier-1 auto-apply only writes genuine synthesized rules.
        if rule_text.strip().upper().startswith("NEEDS HUMAN REWRITE"):
            log.info("auto_apply_boris: skipping %s — rule is a NEEDS-HUMAN-REWRITE placeholder",
                     project_key)
            continue

        # Resolve target CLAUDE.md
        path_hint = _encode_to_path_hint(project_key)
        real_dir = _resolve_real_project_dir(path_hint)
        if real_dir is None:
            log.warning(
                "auto_apply_boris: cannot resolve directory for %s (hint=%s) — skipping",
                project_key, path_hint,
            )
            continue

        claude_md = real_dir / "CLAUDE.md"
        ts_str = now.isoformat()

        # Build the marked block
        open_marker = f"<!-- auto-applied:{item_id} tier:{tier} {ts_str} -->"
        close_marker = f"<!-- /auto-applied:{item_id} -->"
        rule_line = f"- {rule_text}"
        block = f"{open_marker}\n{rule_line}\n{close_marker}\n"

        try:
            # No file-copy backup here: the write below is already wrapped in
            # <!-- auto-applied:... --> markers and recorded in the ledger
            # (record_application below), which is what trust_tiers.rollback_last
            # actually reads to undo an entry. A raw .autoapply-backup-* copy
            # would have zero consumer (grep confirms rollback never reads it).
            if claude_md.exists():
                content = claude_md.read_text(encoding="utf-8")
                section_match = re.search(
                    r"(^## Learned Rules\s*\n)(.*?)(?=\n## |\Z)",
                    content,
                    re.MULTILINE | re.DOTALL,
                )
                if section_match:
                    insert_pos = section_match.end()
                    content = content[:insert_pos] + block + content[insert_pos:]
                else:
                    if not content.endswith("\n"):
                        content += "\n"
                    content += f"\n## Learned Rules\n{block}"
                claude_md.write_text(content, encoding="utf-8")
            else:
                claude_md.parent.mkdir(parents=True, exist_ok=True)
                claude_md.write_text(
                    f"# Project Rules\n\n## Learned Rules\n{block}",
                    encoding="utf-8",
                )
        except OSError as exc:
            log.error("auto_apply_boris: I/O error writing %s: %s", claude_md, exc)
            continue

        # Record in ledger
        ledger_entry: dict = {
            "item_id": item_id,
            "item_type": "boris_rule",
            "tier": tier,
            "target_file": str(claude_md),
            "rollback_marker": item_id,
            "draft_file": str(draft_file),
            "rule_preview": rule_text[:80],
        }
        record_application(ledger_entry, ledger_path=ledger_path, now=now)

        # Tier 1 → notify daily_brief
        if tier == 1:
            pending_entry: dict = {
                "item_id": item_id,
                "type": "boris_rule",
                "summary": rule_text[:120],
                "target_file": str(claude_md),
                "ts": ts_str,
            }
            append_pending_review(pending_entry, pending_path=pending_review_path)

        applied.append(item_id)
        log.info("auto_apply_boris: applied '%s' (tier=%d) -> %s", rule_text[:60], tier, claude_md)

    return applied


def main() -> None:
    """Entry point: read default candidates, write drafts, report to stderr."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate CLAUDE.md rule drafts from Boris correction candidates."
    )
    parser.add_argument(
        "--min-count",
        type=int,
        default=4,
        help="Minimum correction count to generate a draft (default: 4).",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=_DEFAULT_CANDIDATES,
        help=f"Path to boris-candidates.json (default: {_DEFAULT_CANDIDATES}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_DEFAULT_OUT_DIR,
        help=f"Directory for draft files (default: {_DEFAULT_OUT_DIR}).",
    )
    parser.add_argument(
        "--process-accepted",
        action="store_true",
        help="Apply accepted boris rules from accepted-boris.json to CLAUDE.md files.",
    )
    parser.add_argument(
        "--auto-apply",
        action="store_true",
        help="Auto-apply drafts whose trust tier >= 1 (with ledger + rollback support).",
    )
    args = parser.parse_args()

    if args.process_accepted:
        applied = process_accepted_boris(drafts_dir=args.output_dir)
        print(f"Boris rules applied: {len(applied)}", file=sys.stderr)
        for item_id in applied:
            print(f"  {item_id}", file=sys.stderr)
        return

    if args.auto_apply:
        applied = auto_apply_boris(drafts_dir=args.output_dir)
        print(f"Boris rules auto-applied: {len(applied)}", file=sys.stderr)
        for item_id in applied:
            print(f"  {item_id}", file=sys.stderr)
        return

    written = write_drafts(
        boris_path=args.input,
        out_dir=args.output_dir,
        min_count=args.min_count,
    )

    print(f"Boris drafts written: {len(written)}", file=sys.stderr)
    for path in written:
        print(f"  {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
