#!/usr/bin/env python3
"""habit_ledger.py — Persistent habit status tracker and graduation ladder.

Reads habits.json produced by habit_miner.py (stateless), adds memory across
runs, and advances habit status through the graduation ladder:
    detected -> suggested_skill

Also identifies cross-project patterns (same routine appearing in >= N projects).

Usage:
    python habit_ledger.py          # loads habits.json, updates ledger, prints summary
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LEDGER_PATH: Path = Path.home() / ".claude" / "logs" / "habit-ledger.json"
HABITS_PATH: Path = Path.home() / ".claude" / "logs" / "habits.json"
CROSS_PROJECT_PATH: Path = Path.home() / ".claude" / "logs" / "cross-project-habits.json"
SKILLS_DIR: Path = Path.home() / ".claude" / "skills"

D_THRESHOLD: float = 200.0       # distinctiveness threshold for graduation
REWARD_THRESHOLD: float = 0.6    # minimum reward_ratio for graduation
OBSERVE_THRESHOLD: int = 2       # minimum times_observed (distinct runs) before graduation


# ---------------------------------------------------------------------------
# Helper: unified field access for both dicts and dataclass objects
# ---------------------------------------------------------------------------

def _get(h: Any, field: str, default: Any = None) -> Any:
    """Access a field from either a dict or a dataclass/object instance.

    Args:
        h: Habit dict or HabitCandidate dataclass instance.
        field: Field name to retrieve.
        default: Value to return if field is absent.

    Returns:
        Field value, or default if not present.
    """
    if isinstance(h, dict):
        return h.get(field, default)
    return getattr(h, field, default)


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------

def routine_key(project: str, routine: list | tuple) -> str:
    """Build a stable ledger key from project name and routine token list.

    Format: ``"project|tok1>tok2>tok3"``

    Args:
        project: Project name string.
        routine: Ordered sequence of tool signature tokens.

    Returns:
        Deterministic string key for use in the ledger dict.
    """
    # Defensive: coerce tokens to str and tolerate None routine (never crash)
    tokens = [str(t) for t in (routine or [])]
    return f"{project}|{'>'.join(tokens)}"


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------

def load_ledger(path: Path = LEDGER_PATH) -> dict:
    """Load the habit ledger from disk.

    Returns an empty dict if the file is missing, unreadable, or corrupt —
    never raises.

    Args:
        path: Path to the ledger JSON file.

    Returns:
        Ledger dict mapping routine key -> entry dict.
    """
    path = Path(path)
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def save_ledger(ledger: dict, path: Path = LEDGER_PATH) -> None:
    """Persist the habit ledger to disk using an atomic write.

    Writes to a temp file in the same directory, then calls os.replace()
    so concurrent readers never see a partial write.

    Args:
        ledger: Ledger dict to serialize.
        path: Destination file path (parent directories created if absent).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(ledger, ensure_ascii=False, indent=2)

    # Write to a temp file in the same directory for atomic rename
    fd, tmp_path_str = tempfile.mkstemp(
        dir=str(path.parent), prefix=".habit-ledger-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_path_str, str(path))
    except Exception:
        # Clean up temp file on failure; re-raise so caller knows
        try:
            os.unlink(tmp_path_str)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Skill directory matching
# ---------------------------------------------------------------------------

def _skill_matches_routine(routine: list | tuple, skills_dir: Path) -> bool:
    """Return True if any existing skill directory exactly token-matches a routine part.

    Extracts the post-colon part of ``Bash:xxx``, ``Skill:xxx``, ``Agent:xxx``
    tokens (minimum 3 chars).  Plain tokens without ":" are skipped.

    For each skill directory, its name is split on ``-`` and ``_`` into discrete
    tokens (e.g. ``git-commit-helper`` -> ``{"git", "commit", "helper"}``).  A
    match occurs only when an extracted part **exactly equals** one of those
    tokens (case-insensitive).  This prevents substring false-positives such as
    ``"git"`` wrongly matching ``"digital-foo"`` (tokens: ``{digital, foo}``).

    Args:
        routine: Ordered sequence of tool signature tokens.
        skills_dir: Path to the skills root directory.

    Returns:
        True if a matching skill directory is found.
    """
    import re

    skills_dir = Path(skills_dir)
    if not skills_dir.exists():
        return False

    # Collect per-skill token sets: {dir_name -> frozenset of lowercase word tokens}
    try:
        skill_token_sets: list[frozenset[str]] = [
            frozenset(re.split(r"[-_]+", p.name.lower()))
            for p in skills_dir.iterdir()
            if p.is_dir()
        ]
    except OSError:
        return False

    if not skill_token_sets:
        return False

    # Extract meaningful parts from tokens with a colon prefix
    extracted: list[str] = []
    for token in routine:
        if ":" in token:
            part = token.split(":", 1)[1].lower()
            if len(part) >= 3:
                extracted.append(part)

    if not extracted:
        return False

    # Match only if an extracted part EXACTLY equals one of a skill's tokens
    return any(
        part in token_set
        for part in extracted
        for token_set in skill_token_sets
    )


# ---------------------------------------------------------------------------
# Core ledger update
# ---------------------------------------------------------------------------

def update_ledger(
    habits: list,
    ledger: dict,
    skills_dir: Path = SKILLS_DIR,
) -> dict:
    """Merge a fresh habit list into the ledger and advance graduation status.

    Supports both dict habits (from habits.json) and HabitCandidate dataclass
    objects.  Modifies and returns the ledger (makes a shallow copy of the
    existing entries so the input dict is not mutated).

    Graduation rules (applied in order):
      1. If a matching skill directory exists -> ``"skill_exists"`` (always wins).
      2. ``"detected"`` -> ``"suggested_skill"`` when ALL three conditions met:
         - ``distinctiveness >= D_THRESHOLD``
         - ``reward_ratio >= REWARD_THRESHOLD``
         - ``times_observed >= OBSERVE_THRESHOLD`` (after this run's increment)

    Args:
        habits: List of habit dicts or HabitCandidate dataclass instances from
            the current miner run.
        ledger: Existing ledger dict (may be empty ``{}`` for first run).
        skills_dir: Path to the skills root directory for existence checks.

    Returns:
        Updated ledger dict (new object; input ledger is not mutated).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    updated: dict = dict(ledger)  # shallow copy — entries are dicts, we replace them below

    skills_dir = Path(skills_dir)

    for habit in habits:
        project = _get(habit, "project", "unknown")
        routine = _get(habit, "routine", [])
        dist = float(_get(habit, "distinctiveness", 0.0))
        reward = float(_get(habit, "reward_ratio", 0.0))

        key = routine_key(project, routine)

        existing = updated.get(key)
        if existing is None:
            # Brand-new entry
            entry: dict = {
                "first_seen": now_iso,
                "times_observed": 1,
                "status": "detected",
                "last_distinctiveness": dist,
                "last_reward": reward,
            }
        else:
            # Merge — make a copy so we don't mutate the passed-in ledger's values
            entry = dict(existing)
            entry["times_observed"] = existing.get("times_observed", 1) + 1
            entry["last_distinctiveness"] = dist
            entry["last_reward"] = reward

        # Determine status (skill_exists wins, then graduation, then keep current)
        if _skill_matches_routine(routine, skills_dir):
            entry["status"] = "skill_exists"
        elif (
            entry.get("status") == "detected"
            and dist >= D_THRESHOLD
            and reward >= REWARD_THRESHOLD
            and entry["times_observed"] >= OBSERVE_THRESHOLD
        ):
            entry["status"] = "suggested_skill"
        # else: keep existing status unchanged

        updated[key] = entry

    return updated


# ---------------------------------------------------------------------------
# Cross-project pattern detection
# ---------------------------------------------------------------------------

def cross_project_patterns(habits: list, min_projects: int = 3) -> list[dict]:
    """Identify routines that appear in multiple distinct projects.

    Groups habits by their routine token tuple (ignoring project).  Routines
    present in at least ``min_projects`` distinct projects AND with a mean
    distinctiveness > 50 are returned, sorted by mean distinctiveness
    descending.

    Args:
        habits: List of habit dicts or HabitCandidate objects.
        min_projects: Minimum number of distinct projects required.

    Returns:
        List of pattern dicts, each with keys:
        - ``"routine"``: list of token strings
        - ``"projects"``: list of distinct project names
        - ``"mean_distinctiveness"``: float
    """
    # routine_tuple -> {projects: set, distinctiveness_sum: float, count: int}
    grouped: dict[tuple, dict] = defaultdict(
        lambda: {"projects": set(), "distinctiveness_sum": 0.0, "count": 0}
    )

    for habit in habits:
        routine = _get(habit, "routine", [])
        project = _get(habit, "project", "unknown")
        dist = float(_get(habit, "distinctiveness", 0.0))
        key: tuple = tuple(routine)
        grouped[key]["projects"].add(project)
        grouped[key]["distinctiveness_sum"] += dist
        grouped[key]["count"] += 1

    results: list[dict] = []
    for routine_tuple, data in grouped.items():
        projects = sorted(data["projects"])
        if len(projects) < min_projects:
            continue
        mean_dist = data["distinctiveness_sum"] / data["count"]
        if mean_dist <= 50.0:
            continue
        results.append({
            "routine": list(routine_tuple),
            "projects": projects,
            "mean_distinctiveness": round(mean_dist, 4),
        })

    results.sort(key=lambda x: -x["mean_distinctiveness"])
    return results


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Load habits.json, update the ledger, save both outputs, print summary.

    Side effects:
        - Reads ``~/.claude/logs/habits.json``
        - Writes ``~/.claude/logs/habit-ledger.json`` (atomic)
        - Writes ``~/.claude/logs/cross-project-habits.json`` (atomic)
        - Prints a one-line summary to stderr
    """
    # Load habits produced by habit_miner.py
    if not HABITS_PATH.exists():
        print(
            f"[ledger] habits.json not found at {HABITS_PATH} — run habit_miner.py first",
            file=sys.stderr,
        )
        return

    try:
        raw = json.loads(HABITS_PATH.read_text(encoding="utf-8"))
        habits: list = raw if isinstance(raw, list) else []
    except Exception as exc:
        print(f"[ledger] failed to read habits.json: {exc}", file=sys.stderr)
        return

    # Load existing ledger
    ledger = load_ledger(LEDGER_PATH)

    # Update
    updated = update_ledger(habits, ledger, skills_dir=SKILLS_DIR)

    # Save ledger
    save_ledger(updated, LEDGER_PATH)

    # Cross-project patterns
    patterns = cross_project_patterns(habits, min_projects=3)

    # Save cross-project patterns
    CROSS_PROJECT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(patterns, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(
        dir=str(CROSS_PROJECT_PATH.parent),
        prefix=".cross-project-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, str(CROSS_PROJECT_PATH))
    except Exception as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print(f"[ledger] warning: failed to write cross-project-habits.json: {exc}", file=sys.stderr)

    # Summary
    graduated_count = sum(
        1 for e in updated.values() if e.get("status") == "suggested_skill"
    )
    print(
        f"[ledger] {len(updated)} tracked, {graduated_count} graduated to suggested_skill,"
        f" {len(patterns)} cross-project patterns",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
