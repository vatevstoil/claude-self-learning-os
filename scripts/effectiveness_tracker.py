#!/usr/bin/env python3
"""effectiveness_tracker.py — Metacognition layer for the self-improvement system.

Tracks which items surfaced by self_improvement_queue actually get resolved
(acted on), computes per-type precision, and writes auto-tuned threshold
suggestions that the queue reads back via _effective_distinctiveness_min().

This module is SELF-CONTAINED: it reads the queue output and auxiliary logs
but never modifies any other script.

Usage:
    python effectiveness_tracker.py

Files consumed (read-only):
    ~/.claude/logs/improvement-queue.json   — current queue snapshot
    ~/.claude/logs/habit-ledger.json        — for habit resolution status
    ~/.claude/logs/skill-drafts/            — dirs = codified skills
    ~/.claude/logs/boris-drafts/            — files = acted-on boris rules
    ~/.claude/logs/review-verdicts.json     — human accept/reject verdicts (exact match)

Files maintained (written):
    ~/.claude/logs/queue-history.jsonl      — append-only snapshot log
    ~/.claude/logs/effectiveness.json       — precision report
    ~/.claude/logs/thresholds.json          — auto-tuned thresholds (read by queue)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Shared semantic filter — a skill-draft dir whose name is a pure tool-ngram
# (e.g. "edit", "write-cat") carries no real skill and must NOT count as
# "resolving" a habit. Tolerant import so this module never hard-fails if
# llm_judge is unavailable (falls back to a length guard below).
try:  # pragma: no cover - llm_judge is always present in practice
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from llm_judge import is_tool_ngram as _is_tool_ngram
except Exception:  # pragma: no cover
    _is_tool_ngram = None


def _draft_dir_is_noise(name: str) -> bool:
    """True if a skill-draft directory name must not count as resolving a habit.

    Pure tool-ngram names (``edit``, ``read``, ``write``, ``write-cat`` ...) are
    substrings of almost every habit id (habit ids encode tool sequences), so
    treating them as resolutions fabricates habit precision. This guard is the
    fix for the self-reinforcing precision-inflation bug: inflated precision
    pushed ``suggest_thresholds`` to LOWER the distinctiveness bar, admitting
    even more noise each cycle.
    """
    if _is_tool_ngram is not None and _is_tool_ngram(name):
        return True
    # Fallback when llm_judge cannot be imported: drop ultra-short single-tool
    # names (edit=4, read=4, write=5) that cause the worst collisions.
    return len(name) < 8


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
LOGS_DIR = Path.home() / ".claude" / "logs"
QUEUE_PATH = LOGS_DIR / "improvement-queue.json"
LEDGER_PATH = LOGS_DIR / "habit-ledger.json"
SKILL_DRAFTS_DIR = LOGS_DIR / "skill-drafts"
BORIS_DRAFTS_DIR = LOGS_DIR / "boris-drafts"
HISTORY_PATH = LOGS_DIR / "queue-history.jsonl"
EFFECTIVENESS_PATH = LOGS_DIR / "effectiveness.json"
THRESHOLDS_PATH = LOGS_DIR / "thresholds.json"
REVIEW_VERDICTS_PATH = LOGS_DIR / "review-verdicts.json"

_DEFAULT_ANTICIPATIONS = Path.home() / ".claude" / "logs" / "anticipations.json"
_DEFAULT_HABITS = Path.home() / ".claude" / "logs" / "habits.json"

# Base value for the habit distinctiveness threshold
_BASE_DISTINCTIVENESS = 150.0


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def record_snapshot(
    queue_items: list[dict[str, Any]],
    history_path: Path,
    now: datetime | None = None,
) -> None:
    """Append one JSON line to the history file for the current run.

    Args:
        queue_items: List of item dicts from the improvement queue.
            Each must have at least ``id``, ``type``, ``project``, ``score``.
        history_path: Path to the JSONL history file (created if absent).
        now: Timestamp override for tests; defaults to UTC now.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    ts = now.isoformat()

    slim_items = [
        {
            "id": item.get("id", ""),
            "type": item.get("type", ""),
            "project": item.get("project", ""),
            "score": item.get("score", 0.0),
        }
        for item in queue_items
    ]
    line = json.dumps({"ts": ts, "items": slim_items}, ensure_ascii=False)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_history(history_path: Path) -> list[dict[str, Any]]:
    """Load all snapshots from the JSONL history file.

    Silently skips lines that are not valid JSON (corruption tolerance).

    Args:
        history_path: Path to the queue-history.jsonl file.

    Returns:
        List of snapshot dicts in chronological order.
    """
    if not history_path.exists():
        return []

    snapshots: list[dict[str, Any]] = []
    with history_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                snapshots.append(json.loads(line))
            except json.JSONDecodeError:
                # Skip corrupt lines — documented tolerance
                continue
    return snapshots


def _load_verdicts_tolerant(verdicts_path: Path) -> dict[str, Any]:
    """Load review-verdicts.json safely; returns empty dict on any error.

    Args:
        verdicts_path: Path to the review-verdicts JSON file.

    Returns:
        Mapping of full item_id -> verdict entry dict.
    """
    try:
        data = json.loads(verdicts_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def is_resolved(
    item: dict[str, Any],
    ledger: dict[str, Any],
    skill_drafts_dir: Path,
    boris_drafts_dir: Path,
    verdicts_path: Path = REVIEW_VERDICTS_PATH,
) -> bool:
    """Determine whether a queue item has been acted on (resolved).

    Resolution priority (checked in order):

    1. **Human verdict (exact, canonical)**: if ``review-verdicts.json`` contains
       a ``"accepted"`` entry for the item's full id, return ``True`` immediately.
       This is the precise channel for human accept votes and is collision-free.
       A ``"rejected"`` verdict returns ``False`` (explicitly not resolved).

    2. **Legacy file heuristics** (for real drafts created by other tools):

       * ``habit``: Resolved if ANY directory inside ``skill_drafts_dir`` has a
         name that is a substring of the item's ``id``.  The id format is
         ``habit-{proj}-{tok1}-{tok2}-...`` (lowercased), and skill-draft dirs
         are named after the routine tokens (e.g. ``pytest-edit``).
         Also resolved if the ledger entry for the item has
         ``status == "skill_exists"``.

       * ``boris_rule``: Resolved if ANY file in ``boris_drafts_dir`` has a stem
         (filename without extension) that appears as a substring within the
         item's ``project`` or ``id``.  A boris-draft file being created is the
         proxy for "acted on".

    * ``graphify``: Always ``False`` — cannot be reliably detected.

    * Any other type: ``False``.

    Args:
        item: Queue item dict with at least ``id``, ``type``, ``project``.
        ledger: Contents of habit-ledger.json (mapping key -> status dict).
        skill_drafts_dir: Directory containing skill-draft subdirectories.
        boris_drafts_dir: Directory containing boris-draft markdown files.
        verdicts_path: Path to review-verdicts.json for exact human-verdict lookup.
            Defaults to the real log path; inject a tmp path in tests.

    Returns:
        True if the item appears to have been resolved; False otherwise.
    """
    import re as _re
    item_type = item.get("type", "")
    item_id = item.get("id", "").lower()
    item_project = item.get("project", "").lower()
    # Normalize away tool prefixes (e.g. "bash:grep" -> "grep") so the queue id
    # (which keeps "bash:") matches skill-draft slugs (which strip the prefix).
    item_id_norm = _re.sub(r"[a-z]+:", "", item_id)

    if item_type == "graphify":
        return False

    # --- Priority 1: exact human verdict (collision-free) ---
    verdicts = _load_verdicts_tolerant(verdicts_path)
    # Verdicts keys are stored in original case; item_id above is lowercased,
    # so we check both the lowercased key and the original item id.
    raw_item_id = item.get("id", "")
    verdict_entry = verdicts.get(raw_item_id) or verdicts.get(item_id)
    if verdict_entry is not None:
        v = verdict_entry.get("verdict", "")
        if v == "accepted":
            return True
        if v == "rejected":
            return False
        # Unknown verdict value — fall through to legacy heuristics

    # --- Priority 2: legacy file heuristics (real drafts from other tools) ---

    if item_type == "habit":
        # Check skill-draft directories: resolved if any dir name is a substring of item id
        if skill_drafts_dir.exists():
            for entry in skill_drafts_dir.iterdir():
                if not entry.is_dir():
                    continue
                # Tool-ngram dir names collide with nearly every habit id —
                # never let them fabricate a resolution (precision inflation).
                if _draft_dir_is_noise(entry.name):
                    continue
                if (entry.name.lower() in item_id
                        or entry.name.lower() in item_id_norm):
                    return True

        # Check ledger for explicit skill_exists status
        for ledger_key, ledger_val in ledger.items():
            if isinstance(ledger_val, dict):
                status = ledger_val.get("status", "")
                if status == "skill_exists":
                    # Match ledger key against item id heuristically
                    key_lower = ledger_key.lower()
                    if key_lower in item_id or item_id in key_lower:
                        return True
        return False

    if item_type == "boris_rule":
        if boris_drafts_dir.exists():
            for entry in boris_drafts_dir.iterdir():
                if entry.is_file():
                    stem_lower = entry.stem.lower()
                    if stem_lower in item_project or stem_lower in item_id:
                        return True
        return False

    return False


def precision_by_type(
    history: list[dict[str, Any]],
    resolved_fn: Callable[[dict[str, Any]], bool],
) -> dict[str, float]:
    """Compute per-type resolution precision across all history snapshots.

    For each item type, collects all UNIQUE item ids seen across every snapshot,
    then computes the fraction that are now resolved.

    Args:
        history: List of snapshot dicts (each with an ``items`` list).
        resolved_fn: Callable ``(item_dict) -> bool`` that checks resolution.

    Returns:
        Dict mapping type string to precision float in [0.0, 1.0].
        Types with zero unique items are omitted.
    """
    # Collect unique items by (type, id) — keep the last seen dict for resolution check
    unique_items: dict[tuple[str, str], dict[str, Any]] = {}
    for snapshot in history:
        for item in snapshot.get("items", []):
            key = (item.get("type", ""), item.get("id", ""))
            unique_items[key] = item

    # Group by type
    by_type: dict[str, list[dict[str, Any]]] = {}
    for (item_type, _), item in unique_items.items():
        by_type.setdefault(item_type, []).append(item)

    result: dict[str, float] = {}
    for item_type, items in by_type.items():
        if not items:
            continue
        resolved_count = sum(1 for it in items if resolved_fn(it))
        result[item_type] = resolved_count / len(items)

    return result


def samples_by_type(history: list[dict[str, Any]]) -> dict[str, int]:
    """Count unique evaluated items per type across all history snapshots.

    This is the denominator used by :func:`precision_by_type` — it tells
    trust_tiers.py how many samples back each precision estimate.

    Args:
        history: List of snapshot dicts (each with an ``items`` list).

    Returns:
        Dict mapping type string to the count of unique item ids of that type.
        Types with zero items are omitted.
    """
    unique: dict[tuple[str, str], str] = {}
    for snapshot in history:
        for item in snapshot.get("items", []):
            t = item.get("type", "")
            i = item.get("id", "")
            unique[(t, i)] = t

    counts: dict[str, int] = {}
    for (item_type, _) in unique:
        counts[item_type] = counts.get(item_type, 0) + 1
    return counts


def suggest_thresholds(precision: dict[str, float]) -> dict[str, float]:
    """Produce auto-tuned threshold suggestions based on observed precision.

    Logic for ``habit_distinctiveness_min``:
    - Base value: 150.0
    - If habit precision < 0.3: multiply by 1.3 (raise bar — too much noise)
    - If habit precision > 0.7: multiply by 0.8 (lower bar — too strict)
    - Otherwise: keep base
    - Clamped to [150.0, 500.0] — never below base, to block the downward drift
      a false-positive precision used to cause (audit: threshold-auto-tune-corrupted)

    Args:
        precision: Dict from ``precision_by_type``, e.g. ``{"habit": 0.25}``.

    Returns:
        Dict with key ``habit_distinctiveness_min`` (float).
    """
    habit_prec = precision.get("habit")
    threshold = _BASE_DISTINCTIVENESS

    if habit_prec is not None:
        if habit_prec < 0.3:
            threshold = _BASE_DISTINCTIVENESS * 1.3
        elif habit_prec > 0.7:
            threshold = _BASE_DISTINCTIVENESS * 0.8

    # Clamp: never go below the base (prevents false-positive precision from
    # lowering the bar and creating a self-reinforcing corruption loop).
    threshold = max(_BASE_DISTINCTIVENESS, min(500.0, threshold))

    return {"habit_distinctiveness_min": threshold}


def check_anticipation_accuracy(
    anticipations_path: Path = _DEFAULT_ANTICIPATIONS,
    habits_path: Path = _DEFAULT_HABITS,
) -> float:
    """Measure what fraction of anticipations matched actual top habits.

    For each project in anticipations.json, takes the top prediction (first
    entry) and checks whether any high-distinctiveness habit in habits.json
    for that project has a routine overlapping >=50% with the prediction tokens.

    Args:
        anticipations_path: Path to anticipations.json
            (shape: ``{project: [{routine: [str, ...], score: float}, ...]}``)
        habits_path: Path to habits.json
            (shape: ``[{project, routine, distinctiveness, count, ...}]``)

    Returns:
        Accuracy in [0.0, 1.0]. Returns 0.0 when no data is available.
    """
    try:
        ant = json.loads(anticipations_path.read_text(encoding="utf-8"))
    except Exception:
        return 0.0
    try:
        habits_raw = json.loads(habits_path.read_text(encoding="utf-8"))
    except Exception:
        return 0.0

    # Build index: project -> list of frozenset of routine tokens for high-distinctiveness habits
    habit_index: dict[str, list[frozenset]] = {}
    for h in habits_raw:
        proj = h.get("project", "")
        routine = h.get("routine", [])
        if proj and routine and h.get("distinctiveness", 0) > 50:
            habit_index.setdefault(proj, []).append(frozenset(routine))

    matches = 0
    total = 0
    for project, preds in ant.items():
        if not preds:
            continue
        total += 1
        top_routine = frozenset(preds[0].get("routine", []))
        if not top_routine:
            continue
        actual_routines = habit_index.get(project, [])
        for actual in actual_routines:
            overlap = len(top_routine & actual) / len(top_routine)
            if overlap >= 0.5:
                matches += 1
                break

    return round(matches / total, 3) if total else 0.0


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _load_ledger(ledger_path: Path) -> dict[str, Any]:
    """Load habit-ledger.json safely, returning empty dict on any error."""
    try:
        return json.loads(ledger_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_queue(queue_path: Path) -> list[dict[str, Any]]:
    """Load improvement-queue.json safely, returning empty list on any error."""
    try:
        data = json.loads(queue_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def main() -> None:
    """Run effectiveness tracker: snapshot, analyse, write reports.

    Never raises — all errors are caught and reported to stderr.
    """
    try:
        # 1. Load current queue
        queue_items = _load_queue(QUEUE_PATH)

        # 2. Record snapshot
        record_snapshot(queue_items, HISTORY_PATH)

        # 3. Load full history
        history = load_history(HISTORY_PATH)

        # 4. Load auxiliary data for resolution checks
        ledger = _load_ledger(LEDGER_PATH)

        # 5. Build resolution function bound to real paths (verdicts_path uses default)
        def resolved_fn(item: dict[str, Any]) -> bool:
            return is_resolved(item, ledger, SKILL_DRAFTS_DIR, BORIS_DRAFTS_DIR,
                               verdicts_path=REVIEW_VERDICTS_PATH)

        # 6. Compute precision and sample counts
        precision = precision_by_type(history, resolved_fn)
        s_by_type = samples_by_type(history)

        # 7. Suggest thresholds
        thresholds = suggest_thresholds(precision)

        # 8. Compute anticipation accuracy
        ant_accuracy = check_anticipation_accuracy()
        thresholds["anticipation_accuracy"] = ant_accuracy

        # 9. Write effectiveness.json  (samples_by_type added; old keys preserved)
        effectiveness = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "precision_by_type": precision,
            "samples_by_type": s_by_type,
            "total_snapshots": len(history),
            "anticipation_accuracy": ant_accuracy,
        }
        EFFECTIVENESS_PATH.write_text(
            json.dumps(effectiveness, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # 10. Write thresholds.json
        THRESHOLDS_PATH.write_text(
            json.dumps(thresholds, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # 11. Print summary to stderr
        print(
            f"[effectiveness_tracker] snapshots={len(history)} "
            f"queue_items={len(queue_items)} "
            f"precision={precision} "
            f"thresholds={thresholds} "
            f"anticipation_accuracy={ant_accuracy}",
            file=sys.stderr,
        )

    except Exception as exc:  # noqa: BLE001
        print(f"[effectiveness_tracker] ERROR: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
