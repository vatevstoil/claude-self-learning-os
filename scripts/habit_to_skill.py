"""habit_to_skill.py — Habit graduation ladder: turn suggested_skill entries into SKILL.md drafts.

Reads habit-ledger.json (for entries with status == "suggested_skill") and
habits.json (for evidence metadata), then writes SKILL.md draft files to
~/.claude/logs/skill-drafts/{slug}/SKILL.md.

NEVER writes to ~/.claude/skills/ — drafts only, pending human review.
Mirrors the boris_draft.py pattern: suggest, never auto-apply.

Usage:
    python habit_to_skill.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

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

        dest_dir = out_dir / slug
        dest_file = dest_dir / "SKILL.md"

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

    for item_id in item_ids:
        item_id_lower = item_id.lower()

        # Find matching ledger key: item_id contains the ledger key's slug or habit_id
        matched_habit: dict[str, Any] | None = None
        for ledger_key, entry in ledger.items():
            if not isinstance(entry, dict):
                continue
            # Match by ledger key substrings within item_id
            ledger_key_lower = ledger_key.lower()
            habit_id_field = str(entry.get("habit_id", "")).lower()
            slug_field = str(entry.get("slug", "")).lower()
            if (ledger_key_lower in item_id_lower
                    or (habit_id_field and habit_id_field in item_id_lower)
                    or (slug_field and slug_field in item_id_lower)):
                matched_habit = habits_index.get(ledger_key)
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
    args = parser.parse_args()

    if args.process_accepted:
        written = process_accepted_habits()
        print(
            f"[habit_to_skill] processed accepted habits, skill drafts written: {len(written)}",
            file=sys.stderr,
        )
        for path in written:
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
