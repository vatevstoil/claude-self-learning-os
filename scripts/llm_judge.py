#!/usr/bin/env python3
"""llm_judge.py — Semantic filter for skill-draft noise before human review.

Two-layer approach:
1. Heuristic layer (free): detects pure tool-ngram directory names.
2. LLM layer (Ollama): scores remaining drafts for genuine reusability.

Usage:
    python llm_judge.py --prune-drafts               # DRY run: count junk/kept
    python llm_judge.py --prune-drafts --apply       # Move junk to rejected dir
    python llm_judge.py --judge-drafts [--max N]     # LLM-score non-junk drafts
    python llm_judge.py --judge-queue [--max N]      # LLM-score queue items
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths (patchable for tests)
# ---------------------------------------------------------------------------

_DRAFTS_DIR = Path.home() / ".claude" / "logs" / "skill-drafts"
_REJECTED_DIR = Path.home() / ".claude" / "logs" / "skill-drafts-rejected"
_QUEUE_PATH = Path.home() / ".claude" / "logs" / "improvement-queue.json"

# ---------------------------------------------------------------------------
# Heuristic layer
# ---------------------------------------------------------------------------

TOOL_TOKENS: frozenset[str] = frozenset(
    [
        "edit", "read", "grep", "echo", "cat", "ls", "write", "powershell",
        "bash",  # catches "Bash:grep", "Bash:python" etc. after colon-split
        "python", "python3", "pythonexe", "pythonioenco", "git", "gh", "glob",
        "find", "head", "tail", "sed", "awk", "npx", "npm", "node",
        "taskupdate", "taskcreate", "taskstop", "taskget", "tasklist",
        "taskoutput", "todowrite", "toolsearch", "markchapter", "requestaccess",
        "browserbatch", "sendmessage", "spawntask",
        "askuserquestion", "schedulewakeup", "usebrowser", "screenshot",
        "zoom", "leftclick", "computer", "computerbatch", "previeweval", "previewscreenshot",
        "previewconsolelogs", "previewsnapshot", "openapplication", "navigate",
        "javascripttool", "for", "until", "while", "rm", "cp", "mv", "mkdir",
        "pwd", "sleep", "export", "program", "curl", "wget", "exitplanmode",
        "enterplanmode", "websearch", "webfetch", "wait", "explore",
        "tabscontextmcp", "previewstart", "previewstop", "previewresize",
        # Multi-word agent/role tokens that contain '-' themselves:
        "general-purp", "code-reviewe", "python-pro", "frontend-dev",
        "backend-arch",
        # NOTE: bare "security-aud" was tried on 07-01 and reverted here —
        # it collided with the "security-aud-code-reviewe" real-agent-combo
        # test fixture (tests/test_integrity_guard.py); no junk draft on
        # disk currently needs it as a standalone token.
        # 'monitor'/'workflow'/'handoff'/'cd'/'wc' are bare generic words that
        # collide with real skill slugs (monitor-tool, agent-team-workflow,
        # workflow-orchestrator, handoff) when used as standalone tokens, so
        # they are only tool-tokens in these specific self-referential
        # tool-call compounds (greedy matcher supports hyphenated tokens):
        "cd-edit", "monitor-powershell", "workflow-read", "handoff-python",
    ]
)


def is_tool_ngram(name: str) -> bool:
    """Return True if *name* is entirely composed of tool tokens.

    Uses a greedy left-to-right match: at each position try to consume the
    longest token from TOOL_TOKENS before advancing.  Some tokens contain
    hyphens themselves (e.g. ``general-purp``), so a simple split on ``-`` is
    not sufficient.

    Args:
        name: Directory or file name to check (no path separators).

    Returns:
        True if every character in *name* is covered by tool tokens joined
        with ``-``.  False if any part is unrecognised.

    Examples:
        >>> is_tool_ngram("edit-edit-edit")
        True
        >>> is_tool_ngram("fix-flow")
        False
        >>> is_tool_ngram("general-purp-todowrite")
        True
    """
    if not name:
        return False
    # Split on both "-" and ":" so that "Bash:grep" is treated as two tokens
    # ("bash" and "grep") rather than one unrecognised compound token.
    # The slug builder in slugify_routine strips "Bash:" prefixes, but
    # queue item ids (habit-{proj}-{routine[:3]}) keep the raw token form
    # which may still carry "Bash:" prefixes.
    import re as _re
    parts = _re.split(r"[-:]", name.lower())
    # Filter out empty strings from consecutive separators
    parts = [p for p in parts if p]
    if not parts:
        return False
    n = len(parts)
    i = 0
    while i < n:
        matched = False
        # Try longest match first (greedy)
        for end in range(n, i, -1):
            candidate = "-".join(parts[i:end])
            if candidate in TOOL_TOKENS:
                i = end
                matched = True
                break
        if not matched:
            return False
    return True


# ---------------------------------------------------------------------------
# LLM layer
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_DRAFT = (
    "You are a skill-quality judge for an AI automation system. "
    "You receive the content of a skill draft directory (typically a SKILL.md file). "
    "Decide whether this draft represents a genuinely useful, reusable skill "
    "(a meaningful workflow pattern) or is just noise (a generic sequence of tool calls "
    "with no real semantic value, e.g. 'read then edit then read'). "
    "Reply ONLY with a JSON object: "
    '{"verdict": "useful" | "junk", "score": 0.0-1.0, "reason": "one sentence"}. '
    "score=1.0 means definitely useful, 0.0 means pure noise."
)

_SYSTEM_PROMPT_QUEUE = (
    "You are a quality judge for a self-improvement suggestion queue used by an AI assistant. "
    "Each item represents a potential improvement to the assistant's working patterns. "
    "Decide whether the item is genuinely useful (actionable insight that would improve "
    "the assistant's performance) or noise (vague, redundant, or too generic to act on). "
    "Reply ONLY with a JSON object: "
    '{"verdict": "useful" | "junk", "score": 0.0-1.0, "reason": "one sentence"}. '
    "score=1.0 means definitely useful, 0.0 means pure noise."
)


def call_ollama_raw(
    system_prompt: str,
    user_text: str,
    model: str | None = None,
    base_url: str = "http://localhost:11434/v1",
    timeout: int = 60,
) -> str | None:
    """Call Ollama /v1/chat/completions and return the raw content string.

    Never raises.  Returns None on any error (connection, timeout, bad JSON).
    Unlike judge_text, does NOT require/parse a {verdict: useful|junk} shape —
    use this for callers that want free-text LLM output (e.g. rule synthesis).

    Args:
        system_prompt: The instruction prompt.
        user_text: The content to send.
        model: Model name. Falls back to ``LLM_JUDGE_MODEL`` env var, then
            ``"gemma4:hermes"``.
        base_url: Ollama base URL.
        timeout: HTTP timeout in seconds.

    Returns:
        Raw response content string, or None on failure.
    """
    if model is None:
        model = os.environ.get("LLM_JUDGE_MODEL", "gemma4:hermes")

    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "stream": False,
        }
    ).encode("utf-8")

    url = base_url.rstrip("/") + "/chat/completions"

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except Exception:
        return None

    try:
        data = json.loads(raw)
        content: str = data["choices"][0]["message"]["content"]
    except Exception:
        return None

    return content


def judge_text(
    system_prompt: str,
    user_text: str,
    model: str | None = None,
    base_url: str = "http://localhost:11434/v1",
    timeout: int = 60,
) -> dict[str, Any] | None:
    """Call Ollama /v1/chat/completions and parse the JSON verdict.

    Never raises.  Returns None on any error (connection, timeout, bad JSON).

    Args:
        system_prompt: The judge instruction prompt.
        user_text: The content to evaluate.
        model: Model name. Falls back to ``LLM_JUDGE_MODEL`` env var, then
            ``"gemma4:hermes"``.
        base_url: Ollama base URL.
        timeout: HTTP timeout in seconds.

    Returns:
        Dict with keys ``verdict``, ``score``, ``reason`` or None on failure.
    """
    content = call_ollama_raw(
        system_prompt, user_text, model=model, base_url=base_url, timeout=timeout
    )
    if content is None:
        return None

    return _parse_verdict(content)


def _parse_verdict(text: str) -> dict[str, Any] | None:
    """Extract JSON verdict from LLM output, tolerating markdown fences.

    Args:
        text: Raw LLM response string.

    Returns:
        Parsed dict or None if parsing fails.
    """
    # Strip markdown fences if present
    stripped = text.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", stripped)
    if fence_match:
        stripped = fence_match.group(1).strip()

    # Try direct parse first
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        # Try to find JSON object anywhere in the text
        json_match = re.search(r"\{[\s\S]*\}", stripped)
        if not json_match:
            return None
        try:
            obj = json.loads(json_match.group(0))
        except json.JSONDecodeError:
            return None

    if not isinstance(obj, dict):
        return None

    verdict = obj.get("verdict", "")
    if verdict not in ("useful", "junk"):
        return None

    try:
        score = float(obj.get("score", 0.5))
        score = max(0.0, min(1.0, score))
    except (TypeError, ValueError):
        score = 0.5

    reason = str(obj.get("reason", ""))

    return {"verdict": verdict, "score": score, "reason": reason}


def judge_skill_draft(
    draft_dir: Path,
    model: str | None = None,
    base_url: str = "http://localhost:11434/v1",
    timeout: int = 60,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Evaluate a skill-draft directory and write ``judge.json``.

    Reads SKILL.md (or any .md in the dir) to form the evaluation text.
    Writes ``{draft_dir}/judge.json`` on success.
    Returns None if LLM is unavailable.

    Args:
        draft_dir: Path to the draft directory.
        model: LLM model name (or None to use env/default).
        base_url: Ollama base URL.
        timeout: HTTP timeout in seconds.
        now: Timestamp override for testing.

    Returns:
        The judge dict written to disk, or None if LLM unavailable.
    """
    draft_dir = Path(draft_dir)
    if now is None:
        now = datetime.now(timezone.utc)

    # Build evaluation text
    skill_md = draft_dir / "SKILL.md"
    if skill_md.exists():
        try:
            content = skill_md.read_text(encoding="utf-8")
        except Exception:
            content = f"Skill draft directory: {draft_dir.name}"
    else:
        # Collect any .md file
        md_files = list(draft_dir.glob("*.md"))
        if md_files:
            try:
                content = md_files[0].read_text(encoding="utf-8")
            except Exception:
                content = f"Skill draft directory: {draft_dir.name}"
        else:
            content = f"Skill draft directory: {draft_dir.name}\n(no SKILL.md found)"

    user_text = f"Skill draft name: {draft_dir.name}\n\n{content}"

    result = judge_text(
        system_prompt=_SYSTEM_PROMPT_DRAFT,
        user_text=user_text,
        model=model,
        base_url=base_url,
        timeout=timeout,
    )
    if result is None:
        return None

    judge_record: dict[str, Any] = {
        "verdict": result["verdict"],
        "score": result["score"],
        "reason": result["reason"],
        "judged_at": now.isoformat(),
        "method": "llm",
    }

    try:
        (draft_dir / "judge.json").write_text(
            json.dumps(judge_record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass  # Best-effort write; still return the result

    return judge_record


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def _prune_drafts(
    drafts_dir: Path,
    rejected_dir: Path,
    apply: bool,
) -> tuple[int, int, int]:
    """Count or move junk draft directories.

    Args:
        drafts_dir: Source skill-drafts directory.
        rejected_dir: Destination for junk directories.
        apply: If True, actually move junk; otherwise DRY run.

    Returns:
        Tuple of (total, junk_count, kept_count).
    """
    if not drafts_dir.exists():
        return 0, 0, 0

    entries = list(drafts_dir.iterdir())
    # Only consider directories (not .md files or other files)
    dirs = [e for e in entries if e.is_dir()]
    total = len(dirs)
    junk_dirs = [d for d in dirs if is_tool_ngram(d.name)]
    kept = total - len(junk_dirs)

    if apply and junk_dirs:
        rejected_dir.mkdir(parents=True, exist_ok=True)
        pruned_slugs: list[str] = []
        for d in junk_dirs:
            dest = rejected_dir / d.name
            try:
                if dest.exists():
                    # Already rejected in a prior run — this is a regenerated
                    # duplicate. Delete it outright rather than crashing on a
                    # cross-device/FileExistsError rename (the bug that
                    # silently failed on 473/479 dirs and swallowed the error).
                    shutil.rmtree(d)
                else:
                    d.rename(dest)
                pruned_slugs.append(d.name)
            except Exception as exc:
                print(f"[llm_judge] warning: could not prune {d.name}: {exc}", file=sys.stderr)

        # Best-effort: update habit ledger to mark pruned slugs so
        # write_skill_drafts() does not regenerate them on the next cycle.
        # The slug-to-ledger-key mapping is not 1:1 (slug = joined tokens,
        # ledger key = "proj|t0>t1>...") so most mark_pruned calls will
        # find 0 matching keys — that is expected and safe (no error raised).
        if pruned_slugs:
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                from habit_ledger import mark_pruned as _mark_pruned
                updated = _mark_pruned(pruned_slugs)
                if updated:
                    print(
                        f"[llm_judge] mark_pruned updated {updated} ledger entries",
                        file=sys.stderr,
                    )
            except Exception as exc:
                print(
                    f"[llm_judge] warning: could not update ledger after prune: {exc}",
                    file=sys.stderr,
                )

    return total, len(junk_dirs), kept


def _purge_rejected(rejected_dir: Path, older_than_days: int, apply: bool,
                    now: float | None = None) -> int:
    """Delete rejected-draft directories older than *older_than_days* (mtime).

    The skill-drafts-rejected dir grows unbounded (613 dirs / 700KB at audit
    time) — junk is moved there but never removed. This is the GC backstop.

    Args:
        rejected_dir: The skill-drafts-rejected directory.
        older_than_days: Age threshold in days (mtime-based).
        apply: If False, count only (DRY run).
        now: Injected epoch seconds (for tests); defaults to time.time().

    Returns:
        Number of directories removed (or that would be removed in DRY mode).
    """
    if not rejected_dir.exists():
        return 0
    ref = now if now is not None else time.time()
    cutoff = ref - older_than_days * 86400
    removed = 0
    for entry in rejected_dir.iterdir():
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                if apply:
                    shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def _judge_drafts_cmd(
    drafts_dir: Path,
    max_n: int,
    model: str | None,
    base_url: str,
    timeout: int,
) -> None:
    """LLM-judge up to max_n non-junk drafts that lack judge.json."""
    if not drafts_dir.exists():
        print("[llm_judge] drafts dir not found", file=sys.stderr)
        return

    candidates = [
        d for d in drafts_dir.iterdir()
        if d.is_dir()
        and not is_tool_ngram(d.name)
        and not (d / "judge.json").exists()
    ]

    judged = 0
    unavailable = False

    for draft_dir in candidates[:max_n]:
        result = judge_skill_draft(
            draft_dir=draft_dir,
            model=model,
            base_url=base_url,
            timeout=timeout,
        )
        if result is None:
            unavailable = True
            break
        judged += 1
        print(
            f"[llm_judge] {draft_dir.name}: {result['verdict']} "
            f"(score={result['score']:.2f}) — {result['reason']}"
        )

    if unavailable:
        print("[llm_judge] judge unavailable — LLM not reachable")
    else:
        print(f"[llm_judge] judged {judged} drafts")


_QUEUE_TIMEOUT = 120  # default for queue/draft judging; CLI --timeout overrides


_CONSECUTIVE_FAILURE_LIMIT = 3  # circuit breaker threshold


def _judge_queue_cmd(
    queue_path: Path,
    max_n: int,
    model: str | None,
    base_url: str,
    timeout: int,
    judge_text_fn: Any = None,
) -> None:
    """LLM-judge up to max_n queue items lacking judge_score.

    **Ordering**: highest raw ``score`` first among eligible unscored items.

    **Per-item None handling** (skip + continue, not abort):
    A single failed item increments its ``judge_fail_count`` and is persisted
    immediately; the batch continues with the next item.  This prevents one
    poisoned item (e.g. one that reliably triggers HTTP 500) from blocking all
    subsequent items on every run.

    **Permanent error marking**: when ``judge_fail_count`` reaches 2 the item
    receives ``judge_verdict="error"``, ``judge_score=0.0``, and a standard
    reason string.  It is then excluded from the ``to_judge`` pool (treated the
    same as a scored item) so it never blocks future batches.

    **Circuit breaker**: ``_CONSECUTIVE_FAILURE_LIMIT`` (3) *consecutive*
    failures in one run trigger an early stop — this indicates Ollama is
    genuinely down rather than a single bad item.  The stop message reports
    how many items were successfully judged before the breaker fired.  A
    successful response resets the consecutive counter to 0.

    **Incremental persist**: every item state change (success or fail) is
    written to disk immediately so no work is lost on interruption.

    **Eligible items**: ``judge_score is None`` AND ``judge_fail_count < 2``
    (items already permanently marked ``error`` are skipped).

    Args:
        queue_path: Path to the improvement-queue.json file.
        max_n: Maximum number of eligible items to attempt in this run.
        model: Ollama model name (None → env/default).
        base_url: Ollama base URL.
        timeout: HTTP timeout in seconds.
        judge_text_fn: Optional callable replacing ``judge_text`` for tests.
    """
    _judge_fn = judge_text_fn if judge_text_fn is not None else judge_text

    if not queue_path.exists():
        print("[llm_judge] queue file not found", file=sys.stderr)
        return

    try:
        items: list[dict[str, Any]] = json.loads(
            queue_path.read_text(encoding="utf-8")
        )
        if not isinstance(items, list):
            items = []
    except Exception:
        items = []

    # Eligible: unscored AND not permanently error-marked (fail_count < 2).
    to_judge = [
        (idx, item) for idx, item in enumerate(items)
        if item.get("judge_score") is None
        and int(item.get("judge_fail_count") or 0) < 2
    ]
    to_judge.sort(key=lambda t: float(t[1].get("score") or 0.0), reverse=True)

    judged = 0
    consecutive_failures = 0

    for idx, item in to_judge[:max_n]:
        description = item.get("description", "")
        item_type = item.get("type", "")
        item_id = item.get("id", "")
        user_text = (
            f"Queue item id: {item_id}\n"
            f"Type: {item_type}\n"
            f"Description: {description}"
        )

        result = _judge_fn(
            system_prompt=_SYSTEM_PROMPT_QUEUE,
            user_text=user_text,
            model=model,
            base_url=base_url,
            timeout=timeout,
        )

        if result is None:
            consecutive_failures += 1
            fail_count = int(items[idx].get("judge_fail_count") or 0) + 1
            items[idx]["judge_fail_count"] = fail_count

            if fail_count >= 2:
                # Permanently remove from future batches.
                items[idx]["judge_verdict"] = "error"
                items[idx]["judge_score"] = 0.0
                items[idx]["judge_reason"] = (
                    "no response after 2 attempts (timeout or server error)"
                )
                print(
                    f"[llm_judge] item {item_id!r} permanently marked error"
                    f" (fail_count={fail_count})"
                )

            # Persist the updated fail_count / error verdict immediately.
            _atomic_write_json(queue_path, items)

            if consecutive_failures >= _CONSECUTIVE_FAILURE_LIMIT:
                print(
                    f"[llm_judge] stopped after {_CONSECUTIVE_FAILURE_LIMIT}"
                    f" consecutive failures (ollama down?) — judged {judged}"
                )
                return

            # Single failure — skip this item and continue.
            continue

        # Success path.
        consecutive_failures = 0
        items[idx]["judge_score"] = result["score"]
        items[idx]["judge_reason"] = result["reason"]
        items[idx]["judge_verdict"] = result["verdict"]
        judged += 1
        _atomic_write_json(queue_path, items)

    print(f"[llm_judge] judged {judged} queue items")


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON to *path* atomically via a temp file.

    Args:
        path: Destination path.
        data: JSON-serialisable data.
    """
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception as exc:
        print(f"[llm_judge] warning: atomic write failed: {exc}", file=sys.stderr)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse CLI args and dispatch to the appropriate command."""
    p = argparse.ArgumentParser(
        description="LLM-based semantic filter for skill drafts and improvement queue"
    )
    p.add_argument(
        "--prune-drafts",
        action="store_true",
        help="Count/move junk tool-ngram draft directories (DRY by default)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="With --prune-drafts: actually move junk to rejected dir",
    )
    p.add_argument(
        "--judge-drafts",
        action="store_true",
        help="LLM-judge non-junk drafts without judge.json",
    )
    p.add_argument(
        "--purge-rejected-days",
        type=int,
        default=None,
        metavar="N",
        help="Delete rejected-draft dirs older than N days (needs --apply to act)",
    )
    p.add_argument(
        "--judge-queue",
        action="store_true",
        help="LLM-judge queue items without judge_score",
    )
    p.add_argument(
        "--max",
        "--limit",
        dest="max",
        type=int,
        default=None,
        help="Max items to judge (default 10 for drafts, 20 for queue)",
    )
    p.add_argument(
        "--model",
        default=None,
        help="Ollama model name (overrides LLM_JUDGE_MODEL env var)",
    )
    p.add_argument(
        "--base-url",
        default="http://localhost:11434/v1",
        help="Ollama base URL",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=(
            "HTTP timeout in seconds.  Defaults to 120 for --judge-queue "
            "(reasoning models need headroom), 60 for all other commands."
        ),
    )
    # Path overrides (primarily for testing)
    p.add_argument("--drafts-dir", default=None)
    p.add_argument("--rejected-dir", default=None)
    p.add_argument("--queue-path", default=None)

    args = p.parse_args()

    drafts_dir = Path(args.drafts_dir) if args.drafts_dir else _DRAFTS_DIR
    rejected_dir = Path(args.rejected_dir) if args.rejected_dir else _REJECTED_DIR
    queue_path = Path(args.queue_path) if args.queue_path else _QUEUE_PATH

    if not (args.prune_drafts or args.judge_drafts or args.judge_queue
            or args.purge_rejected_days is not None):
        p.print_help()
        sys.exit(0)

    if args.prune_drafts:
        total, junk, kept = _prune_drafts(drafts_dir, rejected_dir, apply=args.apply)
        mode = "APPLY" if args.apply else "DRY"
        print(f"[llm_judge] prune-drafts [{mode}]  total={total}  junk={junk}  kept={kept}")

    if args.purge_rejected_days is not None:
        n = _purge_rejected(rejected_dir, args.purge_rejected_days, apply=args.apply)
        mode = "APPLY" if args.apply else "DRY"
        print(f"[llm_judge] purge-rejected [{mode}]  removed={n} (older than {args.purge_rejected_days}d)")

    if args.judge_drafts:
        max_n = args.max if args.max is not None else 10
        timeout = args.timeout if args.timeout is not None else 60
        _judge_drafts_cmd(
            drafts_dir=drafts_dir,
            max_n=max_n,
            model=args.model,
            base_url=args.base_url,
            timeout=timeout,
        )

    if args.judge_queue:
        max_n = args.max if args.max is not None else 20
        # Reasoning models (e.g. gemma4:hermes) take 60-120 s/item; use a
        # higher default so a slow response is not misreported as "down".
        timeout = args.timeout if args.timeout is not None else _QUEUE_TIMEOUT
        _judge_queue_cmd(
            queue_path=queue_path,
            max_n=max_n,
            model=args.model,
            base_url=args.base_url,
            timeout=timeout,
        )


if __name__ == "__main__":
    main()
