"""habit_to_skill.py — Habit graduation ladder: turn suggested_skill entries into SKILL.md drafts.

Reads habit-ledger.json (for entries with status == "suggested_skill") and
habits.json (for evidence metadata), then writes SKILL.md draft files to
~/.claude/logs/skill-drafts/{slug}/SKILL.md.

Normally NEVER writes to ~/.claude/skills/ — drafts only, pending human review.
With --auto-apply the module installs skills directly when:
  (a) trust tier for "habit" >= 1, AND
  (b) the draft directory contains judge.json with {"verdict": "useful"}.

Usage:
    python habit_to_skill.py
    python habit_to_skill.py --auto-apply
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Semantic gate: never scaffold a draft for a pure tool-ngram routine
# (e.g. "write-cat", "edit-powershell") — these have no reusable value and
# were the source of the ~480-junk-drafts-per-cycle explosion.
try:  # pragma: no cover - llm_judge is always present in practice
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from llm_judge import is_tool_ngram as _is_tool_ngram
except Exception:  # pragma: no cover
    def _is_tool_ngram(_name: str) -> bool:  # type: ignore
        return False

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_LEDGER = Path.home() / ".claude" / "logs" / "habit-ledger.json"
_DEFAULT_HABITS = Path.home() / ".claude" / "logs" / "habits.json"
_DEFAULT_OUT_DIR = Path.home() / ".claude" / "logs" / "skill-drafts"
_DEFAULT_ACCEPTED_HABITS = Path.home() / ".claude" / "logs" / "accepted-habits.json"

# Guard: never write inside skills/ — enforce at module level
_FORBIDDEN_PREFIX = Path.home() / ".claude" / "skills"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def slugify_routine(project: str, routine: list[str]) -> str:  # noqa: ARG001
    """Build a short filesystem-safe slug from a routine token list.

    Strips ``Bash:``, ``Skill:``, and ``Agent:`` prefixes from each token
    (keeping only the part after ``:``) then joins with ``-``, lowercases,
    strips non-alphanumeric chars (except ``-``), and collapses multiple
    consecutive dashes.  The project name is intentionally excluded to keep
    slugs short and reusable.

    Args:
        project: Project name (unused in slug, kept for API symmetry).
        routine: Ordered list of tool-signature tokens, e.g. ``["Edit", "Bash:python"]``.

    Returns:
        A lowercase slug string, e.g. ``"edit-python"``.

    Examples:
        >>> slugify_routine("P", ["Edit", "Bash:python"])
        'edit-python'
        >>> slugify_routine("P", ["Bash:grep", "Read", "Edit"])
        'grep-read-edit'
    """
    parts: list[str] = []
    for token in routine:
        if ":" in token:
            # Strip known namespace prefixes (Bash:, Skill:, Agent:, etc.)
            _, _, after = token.partition(":")
            parts.append(after)
        else:
            parts.append(token)

    raw = "-".join(parts).lower()
    # Keep only [a-z0-9-]
    cleaned = re.sub(r"[^a-z0-9-]", "", raw)
    # Collapse multiple consecutive dashes
    slug = re.sub(r"-{2,}", "-", cleaned)
    return slug.strip("-")


def generate_skill_md(habit: dict[str, Any]) -> str:
    """Generate a SKILL.md draft string from a habit dict.

    The draft contains YAML frontmatter, a bold PENDING header, ordered
    workflow steps derived from the routine, and evidence metadata.  It is
    purely informational — no side effects.

    Args:
        habit: A habit dict with keys ``project``, ``routine``, ``count``,
            ``distinctiveness``, and ``reward_ratio``.

    Returns:
        A multi-line string suitable for writing as SKILL.md.
    """
    project: str = habit.get("project", "unknown")
    routine: list[str] = habit.get("routine") or []
    count: int = int(habit.get("count", 0))
    distinctiveness: float = float(habit.get("distinctiveness", 0.0))
    reward_ratio: float = float(habit.get("reward_ratio", 0.0))

    slug = slugify_routine(project, routine)

    # Build ordered steps — show original token so the raw token appears in the output
    steps_lines = "\n".join(
        f"{i + 1}. `{token}`" for i, token in enumerate(routine)
    )

    # YAML frontmatter
    routine_summary = " > ".join(routine)
    frontmatter = (
        f"name: {slug}\n"
        f'description: "Recurring workflow [{routine_summary}] observed in project {project}"'
    )

    draft = f"""\
---
{frontmatter}
---

**PENDING HUMAN REVIEW — NOT INSTALLED**

> This skill draft was generated automatically by habit_to_skill.py.
> Review carefully before installing to ~/.claude/skills/.
> Delete or move to ~/.claude/skills/{slug}/ only after approval.

## Skill: {slug}

Observed pattern in project **{project}**: `{routine_summary}`

## Workflow Steps

{steps_lines}

## Evidence

| Field            | Value              |
|------------------|--------------------|
| Project          | {project}          |
| Observed count   | {count}            |
| Distinctiveness  | {distinctiveness:.2f} |
| Reward ratio     | {reward_ratio:.4f} |

## Instructions

1. Review the workflow steps above and confirm they describe a real recurring skill.
2. Add a `SKILL.py` or `SKILL.sh` implementation file alongside this `SKILL.md`.
3. When satisfied, move the `{slug}/` folder to `~/.claude/skills/{slug}/`.
4. Delete this draft or keep for audit trail.
"""
    return draft


def write_skill_drafts(
    ledger_path: Path,
    habits_path: Path,
    out_dir: Path,
) -> list[Path]:
    """Write SKILL.md drafts for all ledger entries with status ``suggested_skill``.

    For each qualifying ledger entry:
    - Finds the matching habit dict in habits.json (by project + routine key).
    - Generates a SKILL.md draft via :func:`generate_skill_md`.
    - Writes to ``{out_dir}/{slug}/SKILL.md`` (creates parent dirs).

    Never writes inside ``~/.claude/skills/``.  Never crashes: errors reading
    input files are silently skipped, returning an empty list.

    Args:
        ledger_path: Path to habit-ledger.json.
        habits_path: Path to habits.json.
        out_dir: Root directory for skill draft folders.

    Returns:
        List of :class:`~pathlib.Path` objects for successfully written SKILL.md files.
    """
    out_dir = Path(out_dir)

    # Safety guard: refuse to write inside the live skills directory.
    # Use path-parent containment (not str.startswith) to avoid Windows
    # case-sensitivity and sibling-prefix pitfalls (e.g. "skills-archive").
    try:
        resolved = out_dir.resolve()
        forbidden = _FORBIDDEN_PREFIX.resolve()
        if resolved == forbidden or forbidden in resolved.parents:
            print(
                f"[habit_to_skill] ERROR: out_dir {out_dir} is inside {_FORBIDDEN_PREFIX} — aborted",
                file=sys.stderr,
            )
            return []
    except Exception:
        pass

    # Load ledger
    try:
        ledger_text = Path(ledger_path).read_text(encoding="utf-8")
        ledger: dict[str, Any] = json.loads(ledger_text)
        if not isinstance(ledger, dict):
            return []
    except Exception:
        return []

    # Load habits list
    try:
        habits_text = Path(habits_path).read_text(encoding="utf-8")
        habits_raw = json.loads(habits_text)
        habits: list[dict[str, Any]] = habits_raw if isinstance(habits_raw, list) else []
    except Exception:
        return []

    # Build an index from the same key format used by habit_ledger.routine_key
    def _make_key(h: dict[str, Any]) -> str:
        proj = h.get("project", "")
        routine = h.get("routine") or []
        tokens = [str(t) for t in routine]
        return f"{proj}|{'>'.join(tokens)}"

    habits_index: dict[str, dict[str, Any]] = {_make_key(h): h for h in habits}

    written: list[Path] = []

    for key, entry in ledger.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "suggested_skill":
            continue

        habit = habits_index.get(key)
        if habit is None:
            # No matching habit in current habits.json — skip silently
            continue

        slug = slugify_routine(habit.get("project", ""), habit.get("routine") or [])
        if not slug:
            continue

        # Generation gate: pure tool-ngram routines have no reusable value.
        if _is_tool_ngram(slug):
            continue

        dest_dir = out_dir / slug
        dest_file = dest_dir / "SKILL.md"

        # Convergence guard: if this slug was already pruned as junk (lives in
        # the sibling -rejected dir) do NOT re-emit it — otherwise every
        # dreaming cycle regenerates the same drafts the judge just pruned.
        rejected_dir = out_dir.parent / (out_dir.name + "-rejected")
        if (rejected_dir / slug).exists():
            continue
        # Idempotency: already scaffolded — don't rewrite on every run.
        if dest_file.exists():
            written.append(dest_file)
            continue

        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            content = generate_skill_md(habit)
            dest_file.write_text(content, encoding="utf-8")
            written.append(dest_file)
        except Exception as exc:
            print(f"[habit_to_skill] warning: could not write {dest_file}: {exc}", file=sys.stderr)
            continue

    return written


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def process_accepted_habits(
    accepted_habits_path: Path = _DEFAULT_ACCEPTED_HABITS,
    ledger_path: Path = _DEFAULT_LEDGER,
    habits_path: Path = _DEFAULT_HABITS,
    out_dir: Path = _DEFAULT_OUT_DIR,
) -> list[Path]:
    """Process accepted-habits.json queue and scaffold approved habits.

    Reads the list of accepted item IDs, finds matching entries in the habit
    ledger and habits.json, generates skill scaffolds for each, then clears
    the accepted-habits.json queue.

    Item IDs have format ``habit-{project}-{routine-slug}``. Matching is done
    by checking whether the item_id contains the ledger entry's slug or
    ``habit_id`` field.

    Args:
        accepted_habits_path: Path to accepted-habits.json queue file.
        ledger_path: Path to habit-ledger.json.
        habits_path: Path to habits.json.
        out_dir: Root directory for skill draft folders.

    Returns:
        List of Path objects for successfully written SKILL.md files.
    """
    accepted_habits_path = Path(accepted_habits_path)

    # Load the accepted queue
    try:
        item_ids: list[str] = json.loads(
            accepted_habits_path.read_text(encoding="utf-8")
        )
        if not isinstance(item_ids, list):
            item_ids = []
    except Exception:
        item_ids = []

    if not item_ids:
        return []

    # Load ledger
    try:
        ledger_text = Path(ledger_path).read_text(encoding="utf-8")
        ledger: dict[str, Any] = json.loads(ledger_text)
        if not isinstance(ledger, dict):
            ledger = {}
    except Exception:
        ledger = {}

    # Load habits list
    try:
        habits_text = Path(habits_path).read_text(encoding="utf-8")
        habits_raw = json.loads(habits_text)
        habits: list[dict[str, Any]] = habits_raw if isinstance(habits_raw, list) else []
    except Exception:
        habits = []

    def _make_key(h: dict[str, Any]) -> str:
        proj = h.get("project", "")
        routine = h.get("routine") or []
        tokens = [str(t) for t in routine]
        return f"{proj}|{'>'.join(tokens)}"

    habits_index: dict[str, dict[str, Any]] = {_make_key(h): h for h in habits}

    # Safety guard — same as write_skill_drafts
    out_dir = Path(out_dir)
    try:
        resolved = out_dir.resolve()
        forbidden = _FORBIDDEN_PREFIX.resolve()
        if resolved == forbidden or forbidden in resolved.parents:
            print(
                f"[habit_to_skill] ERROR: out_dir {out_dir} is inside {_FORBIDDEN_PREFIX} — aborted",
                file=sys.stderr,
            )
            return []
    except Exception:
        pass

    written: list[Path] = []

    def _queue_item_id(h: dict[str, Any]) -> str:
        # Must mirror self_improvement_queue._load_habits id construction EXACTLY.
        # The accepted id is "habit-<proj>-<t0-t1-t2>".lower()[:60]; ledger keys
        # use '|' and '>' separators absent from that slug, so the old substring
        # match was structurally impossible and nothing ever got scaffolded.
        proj = h.get("project", "all")
        routine = h.get("routine") or []
        return f"habit-{proj}-{'-'.join(str(t) for t in routine[:3])}".lower()[:60]

    for item_id in item_ids:
        item_id_lower = item_id.lower()

        # Rebuild each habit's queue-id and exact-match against the accepted id.
        matched_habit: dict[str, Any] | None = None
        for h in habits_index.values():
            if _queue_item_id(h) == item_id_lower:
                matched_habit = h
                break

        if matched_habit is None:
            print(
                f"[habit_to_skill] warning: no ledger/habit match for accepted item '{item_id}'",
                file=sys.stderr,
            )
            continue

        slug = slugify_routine(
            matched_habit.get("project", ""), matched_habit.get("routine") or []
        )
        if not slug:
            continue

        # Generation gate: never scaffold a pure tool-ngram routine.
        if _is_tool_ngram(slug):
            print(
                f"[habit_to_skill] skipping '{item_id}' — tool-ngram slug '{slug}' has no reusable value",
                file=sys.stderr,
            )
            continue

        dest_dir = out_dir / slug
        dest_file = dest_dir / "SKILL.md"

        # Skip if already scaffolded
        if dest_file.exists():
            print(
                f"[habit_to_skill] skipping '{item_id}' — already scaffolded at {dest_file}",
                file=sys.stderr,
            )
            written.append(dest_file)
            continue

        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            content = generate_skill_md(matched_habit)
            dest_file.write_text(content, encoding="utf-8")
            written.append(dest_file)
            print(f"[habit_to_skill] scaffolded '{item_id}' -> {dest_file}", file=sys.stderr)
            # Record in applied-ledger.jsonl so the apply-funnel KPI counts
            # human-accepted habit scaffolds, not only auto-applied ones
            # (the 2026-06 funnel bug — see process_accepted_boris).
            try:
                from trust_tiers import record_application
                record_application({
                    "item_id": item_id,
                    "item_type": "habit",
                    "tier": 1,
                    "target_file": str(dest_file),
                    "rollback_marker": item_id,
                    "source": "human_accept",
                })
            except Exception as _lexc:  # ledger hiccup must never fail the scaffold
                print(f"[habit_to_skill] ledger write skipped for {item_id}: {_lexc}", file=sys.stderr)
            # NB: do NOT GC the draft here. auto_apply_habits() reads it from
            # skill-drafts/ on the next pipeline step (--auto-apply) and removes
            # it only AFTER a successful install. Deleting it now would orphan the
            # scaffold and break that handoff (accepted habits never get applied).
        except Exception as exc:
            print(
                f"[habit_to_skill] warning: could not write {dest_file}: {exc}",
                file=sys.stderr,
            )

    # Clear the accepted-habits queue
    try:
        accepted_habits_path.write_text("[]", encoding="utf-8")
    except Exception as exc:
        print(
            f"[habit_to_skill] warning: could not clear accepted-habits queue: {exc}",
            file=sys.stderr,
        )

    return written


_DEFAULT_SKILLS_DIR = Path.home() / ".claude" / "skills"
_DEFAULT_LEDGER_AA = Path.home() / ".claude" / "logs" / "applied-ledger.jsonl"
_DEFAULT_PENDING_REVIEW_AA = Path.home() / ".claude" / "logs" / "auto-applied-pending-review.json"

# Maximum skill installations per auto-apply run (flood protection)
_AUTO_APPLY_MAX_PER_RUN = 1


def _judge_says_useful(draft_dir: Path) -> bool:
    """Return True iff draft_dir/judge.json has {"verdict": "useful"}.

    Tolerant: missing or malformed file → False.

    Args:
        draft_dir: Path to the skill draft directory (slug/).

    Returns:
        True only when the judge file exists and verdict is "useful".
    """
    judge_file = draft_dir / "judge.json"
    try:
        data = json.loads(judge_file.read_text(encoding="utf-8"))
        return isinstance(data, dict) and data.get("verdict") == "useful"
    except Exception:
        return False


def _install_skill(
    slug: str,
    draft_dir: Path,
    skills_dir: Path,
    tier: int,
    item_id: str,
    now: datetime,
) -> Path | None:
    """Copy the skill draft into skills_dir/{slug}/SKILL.md with auto-applied markers.

    The SKILL.md frontmatter block is extended with an ``auto-applied`` comment
    immediately after the closing ``---`` so rollback can locate and strip the
    installed marker line.

    Args:
        slug: Skill slug name.
        draft_dir: Source draft directory containing SKILL.md.
        skills_dir: Destination root (``~/.claude/skills/``).
        tier: The trust tier used (embedded in marker).
        item_id: Unique ID for ledger/rollback.
        now: Timestamp for marker.

    Returns:
        Path to the installed SKILL.md on success, or None on failure.
    """
    source = draft_dir / "SKILL.md"
    if not source.exists():
        return None

    try:
        content = source.read_text(encoding="utf-8")
    except OSError:
        return None

    ts_str = now.isoformat()
    open_marker = f"<!-- auto-applied:{item_id} tier:{tier} {ts_str} -->"
    close_marker = f"<!-- /auto-applied:{item_id} -->"

    # Wrap entire content in markers so rollback can remove the whole installed file
    marked_content = f"{open_marker}\n{content}\n{close_marker}\n"

    dest_dir = skills_dir / slug
    dest_file = dest_dir / "SKILL.md"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file.write_text(marked_content, encoding="utf-8")
    except OSError:
        return None

    return dest_file


def auto_apply_habits(
    drafts_dir: Path = _DEFAULT_OUT_DIR,
    skills_dir: Path = _DEFAULT_SKILLS_DIR,
    ledger_path: Path | None = None,
    pending_review_path: Path = _DEFAULT_PENDING_REVIEW_AA,
    effectiveness_path: Path | None = None,
    overrides_path: Path | None = None,
    now: datetime | None = None,
) -> list[Path]:
    """Auto-install habit skills whose trust tier >= 1 AND judge says useful.

    ledger_path=None resolves _DEFAULT_LEDGER_AA at CALL time so the
    suite-wide conftest ledger guard can redirect it.

    Scans *drafts_dir* for skill draft subdirectories.  For each that:
      - has not already been applied (ledger dedup),
      - has a ``judge.json`` with ``{"verdict": "useful"}``,
      - and trust tier for "habit" >= 1,
    the skill is installed to *skills_dir/{slug}/SKILL.md*.

    At most :data:`_AUTO_APPLY_MAX_PER_RUN` skills are installed per call.

    Args:
        drafts_dir: Root directory of skill drafts (each sub-dir is a slug).
        skills_dir: Destination ``~/.claude/skills/``.
        ledger_path: Path to applied-ledger.jsonl.
        pending_review_path: Path to auto-applied-pending-review.json.
        effectiveness_path: Override for effectiveness.json path (tests).
        overrides_path: Override for trust-overrides.json path (tests).
        now: Timestamp override for tests; defaults to UTC now.

    Returns:
        List of installed SKILL.md :class:`~pathlib.Path` objects.
    """
    try:
        from trust_tiers import (
            get_tier,
            record_application,
            is_in_ledger,
            append_pending_review,
        )
    except ImportError:
        print("[habit_to_skill] trust_tiers not available — auto-apply skipped", file=sys.stderr)
        return []

    if ledger_path is None:
        ledger_path = _DEFAULT_LEDGER_AA
    if now is None:
        now = datetime.now(timezone.utc)

    tier_kwargs: dict = {}
    if effectiveness_path is not None:
        tier_kwargs["effectiveness_path"] = effectiveness_path
    if overrides_path is not None:
        tier_kwargs["overrides_path"] = overrides_path

    tier = get_tier("habit", **tier_kwargs)
    if tier == 0:
        return []

    if not drafts_dir.exists():
        return []

    installed: list[Path] = []

    for draft_dir in sorted(drafts_dir.iterdir()):
        if len(installed) >= _AUTO_APPLY_MAX_PER_RUN:
            break
        if not draft_dir.is_dir():
            continue

        slug = draft_dir.name
        item_id = f"auto-habit-{slug}"

        # Deduplication
        if is_in_ledger(item_id, ledger_path=ledger_path):
            continue

        # Judge gate: must have judge.json with verdict="useful"
        if not _judge_says_useful(draft_dir):
            continue

        dest = _install_skill(slug, draft_dir, skills_dir, tier, item_id, now)
        if dest is None:
            print(f"[habit_to_skill] auto-apply: failed to install {slug}", file=sys.stderr)
            continue

        ts_str = now.isoformat()
        ledger_entry: dict = {
            "item_id": item_id,
            "item_type": "habit",
            "tier": tier,
            "target_file": str(dest),
            "rollback_marker": item_id,
            "slug": slug,
        }
        record_application(ledger_entry, ledger_path=ledger_path, now=now)

        if tier == 1:
            pending_entry: dict = {
                "item_id": item_id,
                "type": "habit",
                "summary": f"Skill '{slug}' installed",
                "target_file": str(dest),
                "ts": ts_str,
            }
            append_pending_review(pending_entry, pending_path=pending_review_path)

        installed.append(dest)
        print(f"[habit_to_skill] auto-applied skill '{slug}' (tier={tier}) -> {dest}", file=sys.stderr)
        # GC: the draft is now a real skill — remove the source draft dir so it
        # doesn't linger in skill-drafts/ forever. Ledger dedup (is_in_ledger)
        # prevents re-install even though the source is gone.
        shutil.rmtree(draft_dir, ignore_errors=True)

    return installed


def main() -> None:
    """Run with default paths; report draft count to stderr."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate SKILL.md drafts from habit ledger"
    )
    parser.add_argument(
        "--process-accepted",
        action="store_true",
        help="Process accepted-habits.json queue and scaffold approved habits",
    )
    parser.add_argument(
        "--auto-apply",
        action="store_true",
        help="Auto-install skills whose trust tier >= 1 and judge says useful",
    )
    args = parser.parse_args()

    if args.process_accepted:
        written = process_accepted_habits()
        print(
            f"[habit_to_skill] processed accepted habits, skill drafts written: {len(written)}",
            file=sys.stderr,
        )
        for path in written:
            print(f"  {path}", file=sys.stderr)
    elif args.auto_apply:
        installed = auto_apply_habits()
        print(
            f"[habit_to_skill] auto-applied skills: {len(installed)}",
            file=sys.stderr,
        )
        for path in installed:
            print(f"  {path}", file=sys.stderr)
    else:
        written = write_skill_drafts(
            ledger_path=_DEFAULT_LEDGER,
            habits_path=_DEFAULT_HABITS,
            out_dir=_DEFAULT_OUT_DIR,
        )
        print(f"[habit_to_skill] skill drafts written: {len(written)}", file=sys.stderr)
        for path in written:
            print(f"  {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
