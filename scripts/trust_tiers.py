"""trust_tiers.py — Graduated trust for auto-applying self-improvement drafts.

Tier model:
    Tier 0  = draft, awaits human review (default / fail-closed)
    Tier 1  = auto-apply + notification entry + rollback support
    Tier 2  = silent auto-apply (ledger entry still written, rollback supported)

Tier is computed from effectiveness.json (precision + samples) and can be
overridden per item-type via trust-overrides.json.

All public functions are pure / dependency-injected (path= / now= kwargs)
for full testability without mocking.

CLI:
    python trust_tiers.py status
    python trust_tiers.py rollback [--type boris_rule]
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_DEFAULT_EFFECTIVENESS = Path.home() / ".claude" / "logs" / "effectiveness.json"
_DEFAULT_OVERRIDES = Path.home() / ".claude" / "trust-overrides.json"
_DEFAULT_LEDGER = Path.home() / ".claude" / "logs" / "applied-ledger.jsonl"

# Tier thresholds — intentionally conservative
_TIER1_PRECISION = 0.80
_TIER1_SAMPLES = 10
_TIER2_PRECISION = 0.92
_TIER2_SAMPLES = 25


# ---------------------------------------------------------------------------
# Loaders (tolerant — never raise)
# ---------------------------------------------------------------------------


def _load_effectiveness(path: Path) -> dict[str, Any]:
    """Load effectiveness.json safely; returns empty dict on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_overrides(path: Path) -> dict[str, Any]:
    """Load trust-overrides.json safely; returns empty dict on any error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def get_tier(
    item_type: str,
    effectiveness_path: Path = _DEFAULT_EFFECTIVENESS,
    overrides_path: Path = _DEFAULT_OVERRIDES,
) -> int:
    """Return the trust tier (0, 1, or 2) for *item_type*.

    Policy (fail-closed):
    - Override file wins absolutely (both up and down).
    - Tier 2 if precision >= 0.92 AND samples >= 25.
    - Tier 1 if precision >= 0.80 AND samples >= 10.
    - Tier 0 otherwise (missing data -> 0).

    Args:
        item_type: The item category string, e.g. ``"boris_rule"`` or ``"habit"``.
        effectiveness_path: Path to effectiveness.json.
        overrides_path: Path to trust-overrides.json.

    Returns:
        Integer tier 0, 1, or 2.
    """
    # Manual overrides win unconditionally
    overrides = _load_overrides(overrides_path)
    if item_type in overrides:
        try:
            val = int(overrides[item_type])
            return max(0, min(2, val))
        except (TypeError, ValueError):
            pass  # malformed override -> fall through to computed

    eff = _load_effectiveness(effectiveness_path)
    precision_map: dict[str, float] = eff.get("precision_by_type") or {}
    samples_map: dict[str, int] = eff.get("samples_by_type") or {}

    precision = precision_map.get(item_type)
    samples = samples_map.get(item_type)

    # Missing data -> fail-closed
    if precision is None or samples is None:
        return 0

    try:
        precision = float(precision)
        samples = int(samples)
    except (TypeError, ValueError):
        return 0

    if precision >= _TIER2_PRECISION and samples >= _TIER2_SAMPLES:
        return 2
    if precision >= _TIER1_PRECISION and samples >= _TIER1_SAMPLES:
        return 1
    return 0


def tier_reason(
    item_type: str,
    effectiveness_path: Path = _DEFAULT_EFFECTIVENESS,
    overrides_path: Path = _DEFAULT_OVERRIDES,
) -> str:
    """Return a human-readable explanation for the computed tier.

    Args:
        item_type: The item category string.
        effectiveness_path: Path to effectiveness.json.
        overrides_path: Path to trust-overrides.json.

    Returns:
        A short explanation string.
    """
    overrides = _load_overrides(overrides_path)
    if item_type in overrides:
        try:
            val = int(overrides[item_type])
            clamped = max(0, min(2, val))
            return f"override={clamped} (from trust-overrides.json)"
        except (TypeError, ValueError):
            return "override malformed — computed tier used"

    eff = _load_effectiveness(effectiveness_path)
    precision_map: dict[str, float] = eff.get("precision_by_type") or {}
    samples_map: dict[str, int] = eff.get("samples_by_type") or {}

    precision = precision_map.get(item_type)
    samples = samples_map.get(item_type)

    if precision is None:
        return f"no precision data for '{item_type}' -> tier 0"
    if samples is None:
        return f"no samples data for '{item_type}' -> tier 0"

    try:
        precision = float(precision)
        samples = int(samples)
    except (TypeError, ValueError):
        return "malformed data -> tier 0"

    tier = get_tier(item_type, effectiveness_path, overrides_path)
    return (
        f"precision={precision:.3f} samples={samples} -> tier {tier} "
        f"(need >={_TIER1_PRECISION}/{_TIER1_SAMPLES} for T1, "
        f">={_TIER2_PRECISION}/{_TIER2_SAMPLES} for T2)"
    )


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def record_application(
    entry: dict[str, Any],
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> None:
    """Append *entry* to the applied-ledger.jsonl file.

    ledger_path=None resolves the module default AT CALL TIME (not at def
    time), so the suite-wide conftest guard can monkeypatch _DEFAULT_LEDGER —
    the 2026-07-02 audit found 59/62 production ledger rows were pytest
    fixtures appended through this default, skewing trust-tier precision.

    The entry must contain at minimum:
        - ``item_id``   : unique identifier of the applied item
        - ``item_type`` : e.g. ``"boris_rule"``
        - ``tier``      : int tier used for this application
        - ``target_file``: path string to the file that was modified
        - ``rollback_marker``: the item_id string used inside HTML/YAML markers

    A ``ts`` (ISO-8601 UTC) field is added automatically if absent.

    Args:
        entry: Dict with the fields above (extra keys are preserved).
        ledger_path: Path to the JSONL ledger file (created if absent).
        now: Timestamp override for tests; defaults to UTC now.
    """
    if ledger_path is None:
        ledger_path = _DEFAULT_LEDGER
    if now is None:
        now = datetime.now(timezone.utc)
    record = dict(entry)
    record.setdefault("ts", now.isoformat())
    record.setdefault("rolled_back", False)

    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_ledger(ledger_path: Path = _DEFAULT_LEDGER) -> list[dict[str, Any]]:
    """Load all entries from the applied-ledger.jsonl file.

    Silently skips corrupt lines.

    Args:
        ledger_path: Path to the JSONL ledger file.

    Returns:
        List of entry dicts in chronological order.
    """
    if not ledger_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with ledger_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return entries


def is_in_ledger(item_id: str, ledger_path: Path = _DEFAULT_LEDGER) -> bool:
    """Return True if *item_id* has a non-rolled-back entry in the ledger.

    Args:
        item_id: The item identifier to check.
        ledger_path: Path to the JSONL ledger file.

    Returns:
        True if an active (not rolled back) entry exists for the item_id.
    """
    for entry in load_ledger(ledger_path):
        if entry.get("item_id") == item_id and not entry.get("rolled_back", False):
            return True
    return False


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

# Marker pattern written by boris_draft.py --auto-apply and habit_to_skill.py --auto-apply:
# <!-- auto-applied:{item_id} tier:{tier} {ts} -->
# ... inserted text ...
# <!-- /auto-applied:{item_id} -->
_MARKER_OPEN_PAT = re.compile(
    r"<!-- auto-applied:([^\s>]+)\s+tier:\d+\s+[^>]+ -->\n?", re.MULTILINE
)
_MARKER_CLOSE_PAT_TPL = "<!-- /auto-applied:{item_id} -->"


def _remove_marked_block(content: str, item_id: str) -> str:
    """Remove the auto-applied block for *item_id* from *content*.

    The block spans from ``<!-- auto-applied:{item_id} ... -->`` to
    ``<!-- /auto-applied:{item_id} -->`` inclusive, including the trailing
    newline after the closing marker if present.

    Args:
        content: Full text of the target file.
        item_id: The item_id embedded in the markers.

    Returns:
        Content with the block removed, or original content if not found.
    """
    close_marker = _MARKER_CLOSE_PAT_TPL.format(item_id=item_id)
    # Escape for regex
    escaped_id = re.escape(item_id)
    pattern = re.compile(
        r"<!-- auto-applied:" + escaped_id + r"\s+tier:\d+\s+[^>]+ -->\n?" +
        r".*?" +
        re.escape(close_marker) + r"\n?",
        re.DOTALL,
    )
    return pattern.sub("", content)


def rollback_last(
    item_type: str | None = None,
    ledger_path: Path = _DEFAULT_LEDGER,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Rollback the last non-rolled-back application.

    Finds the last entry in the ledger (optionally filtered by *item_type*),
    removes the marked block from target_file, and marks the ledger entry
    as rolled_back by rewriting the JSONL file.

    Args:
        item_type: If given, only consider entries of this type.
        ledger_path: Path to the JSONL ledger file.
        now: Timestamp override for tests; defaults to UTC now.

    Returns:
        The rolled-back ledger entry dict, or None if nothing to roll back.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    entries = load_ledger(ledger_path)
    if not entries:
        return None

    # Find the last active entry matching the filter
    target_entry: dict[str, Any] | None = None
    target_index: int = -1
    for i, entry in enumerate(entries):
        if entry.get("rolled_back", False):
            continue
        if item_type is not None and entry.get("item_type") != item_type:
            continue
        target_entry = entry
        target_index = i

    if target_entry is None:
        return None

    # Remove the marked block from the target file
    target_file = Path(target_entry.get("target_file", ""))
    item_id = target_entry.get("rollback_marker") or target_entry.get("item_id", "")

    if target_file.exists() and item_id:
        try:
            original = target_file.read_text(encoding="utf-8")
            patched = _remove_marked_block(original, item_id)
            if patched != original:
                target_file.write_text(patched, encoding="utf-8")
        except Exception as exc:
            print(f"[trust_tiers] rollback I/O error on {target_file}: {exc}", file=sys.stderr)

    # Mark entry as rolled_back in ledger
    entries[target_index] = dict(target_entry, rolled_back=True, rolled_back_at=now.isoformat())

    # Rewrite ledger atomically
    import os
    import tempfile

    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(ledger_path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, str(ledger_path))
    except Exception as exc:
        print(f"[trust_tiers] rollback ledger rewrite error: {exc}", file=sys.stderr)

    return target_entry


# ---------------------------------------------------------------------------
# Pending-review helper (used by boris_draft and habit_to_skill)
# ---------------------------------------------------------------------------

_DEFAULT_PENDING_REVIEW = Path.home() / ".claude" / "logs" / "auto-applied-pending-review.json"


def append_pending_review(
    item: dict[str, Any],
    pending_path: Path = _DEFAULT_PENDING_REVIEW,
) -> None:
    """Append *item* to the pending-review list (Tier 1 notifications).

    File format: JSON array.  Creates file if absent.  Tolerant loader.

    Args:
        item: Dict with keys ``item_id``, ``type``, ``summary``, ``target_file``, ``ts``.
        pending_path: Path to auto-applied-pending-review.json.
    """
    try:
        existing: list[dict[str, Any]] = json.loads(
            pending_path.read_text(encoding="utf-8")
        )
        if not isinstance(existing, list):
            existing = []
    except Exception:
        existing = []

    existing.append(item)
    try:
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        print(f"[trust_tiers] could not write pending-review: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_KNOWN_TYPES = ["boris_rule", "habit", "graphify"]


def _cmd_status(
    effectiveness_path: Path = _DEFAULT_EFFECTIVENESS,
    overrides_path: Path = _DEFAULT_OVERRIDES,
) -> None:
    """Print tier status for all known item types."""
    types_to_check = list(_KNOWN_TYPES)

    # Also include any types that appear in effectiveness.json or overrides
    eff = _load_effectiveness(effectiveness_path)
    for t in (eff.get("precision_by_type") or {}).keys():
        if t not in types_to_check:
            types_to_check.append(t)
    overrides = _load_overrides(overrides_path)
    for t in overrides.keys():
        if t not in types_to_check:
            types_to_check.append(t)

    print("Trust Tier Status")
    print("=" * 60)
    for t in types_to_check:
        tier = get_tier(t, effectiveness_path, overrides_path)
        reason = tier_reason(t, effectiveness_path, overrides_path)
        print(f"  {t:<20} tier={tier}  {reason}")


def _cmd_rollback(item_type: str | None, ledger_path: Path = _DEFAULT_LEDGER) -> None:
    """Rollback the last application entry."""
    entry = rollback_last(item_type=item_type, ledger_path=ledger_path)
    if entry is None:
        print(
            "[trust_tiers] nothing to rollback"
            + (f" for type={item_type}" if item_type else ""),
            file=sys.stderr,
        )
    else:
        print(
            f"[trust_tiers] rolled back: {entry.get('item_id')} "
            f"(type={entry.get('item_type')}) from {entry.get('target_file')}",
            file=sys.stderr,
        )


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Trust tier management for auto-applied drafts.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show tier status for all item types")

    rollback_p = sub.add_parser("rollback", help="Rollback the last auto-applied entry")
    rollback_p.add_argument("--type", dest="item_type", default=None, help="Filter by item type")

    args = parser.parse_args()

    if args.command == "status":
        _cmd_status()
    elif args.command == "rollback":
        _cmd_rollback(item_type=args.item_type)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
