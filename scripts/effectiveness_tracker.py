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


def is_resolved(
    item: dict[str, Any],
    ledger: dict[str, Any],
    skill_drafts_dir: Path,
    boris_drafts_dir: Path,
) -> bool:
    """Determine whether a queue item has been acted on (resolved).

    Resolution heuristics (documented, intentionally approximate):

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

    if item_type == "habit":
        # Check skill-draft directories: resolved if any dir name is a substring of item id
        if skill_drafts_dir.exists():
            for entry in skill_drafts_dir.iterdir():
                if entry.is_dir() and (entry.name.lower() in item_id
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


def suggest_thresholds(precision: dict[str, float]) -> dict[str, float]:
    """Produce auto-tuned threshold suggestions based on observed precision.

    Logic for ``habit_distinctiveness_min``:
    - Base value: 150.0
    - If habit precision < 0.3: multiply by 1.3 (raise bar — too much noise)
    - If habit precision > 0.7: multiply by 0.8 (lower bar — too strict)
    - Otherwise: keep base
    - Clamped to [50.0, 500.0]

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

    # Clamp to valid range
    threshold = max(50.0, min(500.0, threshold))

    return {"habit_distinctiveness_min": threshold}


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

        # 5. Build resolution function bound to real paths
        def resolved_fn(item: dict[str, Any]) -> bool:
            return is_resolved(item, ledger, SKILL_DRAFTS_DIR, BORIS_DRAFTS_DIR)

        # 6. Compute precision
        precision = precision_by_type(history, resolved_fn)

        # 7. Suggest thresholds
        thresholds = suggest_thresholds(precision)

        # 8. Write effectiveness.json
        effectiveness = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "precision_by_type": precision,
            "total_snapshots": len(history),
        }
        EFFECTIVENESS_PATH.write_text(
            json.dumps(effectiveness, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # 9. Write thresholds.json
        THRESHOLDS_PATH.write_text(
            json.dumps(thresholds, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # 10. Print summary to stderr
        print(
            f"[effectiveness_tracker] snapshots={len(history)} "
            f"queue_items={len(queue_items)} "
            f"precision={precision} "
            f"thresholds={thresholds}",
            file=sys.stderr,
        )

    except Exception as exc:  # noqa: BLE001
        print(f"[effectiveness_tracker] ERROR: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
