#!/usr/bin/env python3
"""suggestion_feedback.py — Inhibitory feedback layer for the self-improvement queue.

Records user decisions (dismiss / accept) per suggestion item and suppresses
re-surfacing dismissed items until their exponentially-increasing cooldown expires.

Feedback store: ~/.claude/logs/suggestion-feedback.json
Shape: {item_id: {status, last_at, suppress_until, dismiss_count}}

Usage:
    python suggestion_feedback.py dismiss <id> [--weeks N]
    python suggestion_feedback.py accept <id>
    python suggestion_feedback.py list
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

LOGS_DIR = Path.home() / ".claude" / "logs"
DEFAULT = LOGS_DIR / "suggestion-feedback.json"

# Auto-suppress an item after being surfaced this many times without explicit action
IMPLICIT_SUPPRESS_THRESHOLD = 5


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def load_feedback(path: Path = DEFAULT) -> dict:
    """Load the feedback store from *path*.

    Returns an empty dict when the file is absent or corrupt — never raises.

    Args:
        path: Path to the JSON feedback file.

    Returns:
        Mapping of item_id -> feedback entry dict.
    """
    path = Path(path)
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_feedback(data: dict, path: Path = DEFAULT) -> None:
    """Atomically write *data* to *path* (tmp file + os.replace).

    Args:
        data: The full feedback mapping to persist.
        path: Target file path.  Parent directories are created as needed.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def dismiss(
    item_id: str,
    weeks: int = 4,
    path: Path = DEFAULT,
    now: datetime | None = None,
) -> dict:
    """Record a dismissal for *item_id* with exponential back-off suppression.

    Each successive dismiss doubles the effective cooldown window:
      dismiss_count 1 -> base weeks (default 4w)
      dismiss_count 2 -> 2x base (8w)
      dismiss_count 3 -> 4x base (16w)
      dismiss_count N -> 2^(N-1) * base

    Args:
        item_id: Unique identifier of the suggestion item.
        weeks: Base cooldown window in weeks for the first dismissal.
        path: Path to the feedback JSON file.
        now: Current timestamp (UTC). Defaults to datetime.now(timezone.utc).
              Inject a fixed value in tests for determinism.

    Returns:
        The updated feedback entry for *item_id*.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    data = load_feedback(path)
    entry = data.get(item_id, {"dismiss_count": 0, "status": "dismissed"})

    dismiss_count = entry.get("dismiss_count", 0) + 1
    effective_weeks = weeks * (2 ** (dismiss_count - 1))
    suppress_until = now + timedelta(weeks=effective_weeks)

    entry.update(
        status="dismissed",
        last_at=now.isoformat(),
        suppress_until=suppress_until.isoformat(),
        dismiss_count=dismiss_count,
    )
    data[item_id] = entry
    save_feedback(data, path)
    return entry


def accept(
    item_id: str,
    path: Path = DEFAULT,
    now: datetime | None = None,
) -> dict:
    """Record acceptance of *item_id*, clearing any suppression.

    Args:
        item_id: Unique identifier of the suggestion item.
        path: Path to the feedback JSON file.
        now: Current timestamp (UTC). Defaults to datetime.now(timezone.utc).

    Returns:
        The updated feedback entry for *item_id*.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    data = load_feedback(path)
    entry = data.get(item_id, {})

    entry.update(
        status="accepted",
        last_at=now.isoformat(),
        suppress_until=None,
    )
    # Preserve dismiss_count if present (history is useful)
    data[item_id] = entry
    save_feedback(data, path)
    return entry


def record_surfaced(
    item_id: str,
    path: Path = DEFAULT,
    threshold: int = IMPLICIT_SUPPRESS_THRESHOLD,
    now: datetime | None = None,
) -> dict:
    """Increment surfaced_count for *item_id*. Auto-dismiss after *threshold* ignores.

    Called by session_start_brief each time an item is shown to the user.
    After *threshold* surfaces without explicit accept/dismiss, the item is
    auto-dismissed with the same exponential back-off as a manual dismissal
    (marked ``implicit=True`` so it can be distinguished in audits).

    Already-dismissed or accepted items are not affected.

    Args:
        item_id: Unique identifier of the suggestion item.
        path: Path to the feedback JSON file.
        threshold: Number of surfaces before auto-dismiss (default 5).
        now: Current timestamp (UTC). Defaults to datetime.now(timezone.utc).

    Returns:
        The updated feedback entry for *item_id*.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    data = load_feedback(path)
    entry = data.get(item_id, {})

    # Don't interfere with explicit dismiss/accept decisions
    if entry.get("status") in ("dismissed", "accepted"):
        return entry

    surfaced_count = entry.get("surfaced_count", 0) + 1
    entry["surfaced_count"] = surfaced_count
    entry.setdefault("status", "surfaced")
    entry["last_surfaced"] = now.isoformat()

    if surfaced_count >= threshold:
        # Auto-suppress using same exponential window as manual dismiss
        dismiss_count = entry.get("dismiss_count", 0) + 1
        effective_weeks = 4 * (2 ** (dismiss_count - 1))
        suppress_until = now + timedelta(weeks=effective_weeks)
        entry.update(
            status="dismissed",
            last_at=now.isoformat(),
            suppress_until=suppress_until.isoformat(),
            dismiss_count=dismiss_count,
            implicit=True,
        )

    data[item_id] = entry
    save_feedback(data, path)
    return entry


def is_suppressed(
    item_id: str,
    path: Path = DEFAULT,
    now: datetime | None = None,
) -> bool:
    """Return True if *item_id* is currently within its suppression window.

    An item is suppressed when:
    - It has an entry in the feedback store,
    - Its status is "dismissed", AND
    - now < suppress_until.

    Unknown items return False (never suppressed by default).

    Args:
        item_id: Unique identifier of the suggestion item.
        path: Path to the feedback JSON file.
        now: Current timestamp (UTC). Defaults to datetime.now(timezone.utc).

    Returns:
        True if the item should be hidden from the queue.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    data = load_feedback(path)
    entry = data.get(item_id)
    if entry is None:
        return False
    if entry.get("status") != "dismissed":
        return False
    raw = entry.get("suppress_until")
    if raw is None:
        return False
    try:
        suppress_until = datetime.fromisoformat(raw)
        # Ensure timezone-aware comparison
        if suppress_until.tzinfo is None:
            suppress_until = suppress_until.replace(tzinfo=timezone.utc)
        return now < suppress_until
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Manage suggestion feedback (dismiss / accept / list)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    d_parser = sub.add_parser("dismiss", help="Dismiss a suggestion item")
    d_parser.add_argument("id", help="Item ID to dismiss")
    d_parser.add_argument(
        "--weeks", type=int, default=4,
        help="Base suppression window in weeks (doubles each re-dismiss)"
    )

    a_parser = sub.add_parser("accept", help="Accept a suggestion item")
    a_parser.add_argument("id", help="Item ID to accept")

    sub.add_parser("list", help="List all feedback entries")

    args = parser.parse_args()

    if args.cmd == "dismiss":
        entry = dismiss(args.id, weeks=args.weeks)
        print(f"Dismissed '{args.id}' — suppressed until {entry['suppress_until']} "
              f"(dismiss #{entry['dismiss_count']})")

    elif args.cmd == "accept":
        entry = accept(args.id)
        print(f"Accepted '{args.id}' — suppression cleared (last_at {entry['last_at']})")

    elif args.cmd == "list":
        data = load_feedback()
        if not data:
            print("(no feedback entries)")
            return
        now = datetime.now(timezone.utc)
        for item_id, entry in sorted(data.items()):
            status = entry.get("status", "?")
            suppressed = is_suppressed(item_id, now=now)
            sup_label = f"  [suppressed until {entry.get('suppress_until')}]" if suppressed else ""
            print(f"{item_id}: {status}  (count={entry.get('dismiss_count', 0)}, "
                  f"last={entry.get('last_at', '?')}){sup_label}")


if __name__ == "__main__":
    main()
