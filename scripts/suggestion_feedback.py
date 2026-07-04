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
    python suggestion_feedback.py review --list
    python suggestion_feedback.py review --accept <short-id>
    python suggestion_feedback.py review --reject <short-id>
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from core_io import load_json_tolerant, atomic_write_json

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

LOGS_DIR = Path.home() / ".claude" / "logs"
DEFAULT = LOGS_DIR / "suggestion-feedback.json"
QUEUE_PATH = LOGS_DIR / "improvement-queue.json"

# Auto-suppress an item after being surfaced this many times without explicit action
IMPLICIT_SUPPRESS_THRESHOLD = 5

# Items below this score are auto-rejected at review time — too low-quality for human review
AUTO_REJECT_SCORE_THRESHOLD = 0.5

ACCEPTED_HABITS_PATH = LOGS_DIR / "accepted-habits.json"
ACCEPTED_BORIS_PATH = LOGS_DIR / "accepted-boris.json"

# Store for human review verdicts written by review --accept/--reject
REVIEW_VERDICTS_PATH = LOGS_DIR / "review-verdicts.json"


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
    data = load_json_tolerant(path, {})
    if isinstance(data, dict):
        return data
    return {}


def save_feedback(data: dict, path: Path = DEFAULT) -> None:
    """Atomically write *data* to *path* (tmp file + os.replace).

    Args:
        data: The full feedback mapping to persist.
        path: Target file path.  Parent directories are created as needed.
    """
    atomic_write_json(path, data)


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
        implicit=False,  # explicit human "no" overrides a prior auto-dismiss —
        # without this, audits/clamps misread human decisions as machine noise
    )
    data[item_id] = entry
    save_feedback(data, path)
    return entry


def _queue_accepted_boris(item_id: str, path: Path = ACCEPTED_BORIS_PATH) -> None:
    """Append item_id to the accepted-boris queue (deduplicates).

    Args:
        item_id: Unique identifier of the boris rule item to queue.
        path: Path to the accepted-boris JSON file.
    """
    path = Path(path)
    existing: list[str] = load_json_tolerant(path, [])
    if not isinstance(existing, list):
        existing = []
    if item_id not in existing:
        existing.append(item_id)
        atomic_write_json(path, existing)


def _queue_accepted_habit(item_id: str, path: Path = ACCEPTED_HABITS_PATH) -> None:
    """Append item_id to the accepted-habits queue (deduplicates).

    Args:
        item_id: Unique identifier of the habit item to queue.
        path: Path to the accepted-habits JSON file.
    """
    path = Path(path)
    existing: list[str] = load_json_tolerant(path, [])
    if not isinstance(existing, list):
        existing = []
    if item_id not in existing:
        existing.append(item_id)
        atomic_write_json(path, existing)


def accept(
    item_id: str,
    item_type: str | None = None,
    path: Path = DEFAULT,
    accepted_habits_path: Path = ACCEPTED_HABITS_PATH,
    accepted_boris_path: Path = ACCEPTED_BORIS_PATH,
    now: datetime | None = None,
) -> dict:
    """Record acceptance of *item_id*, clearing any suppression.

    When item_type == "habit", also queues the item for skill scaffolding
    by appending item_id to accepted-habits.json.
    When item_type == "boris_rule", queues the item for CLAUDE.md injection
    by appending item_id to accepted-boris.json.

    Args:
        item_id: Unique identifier of the suggestion item.
        item_type: Optional type string (e.g. "habit", "boris_rule"). When
            "habit", triggers queuing for skill scaffold generation.  When
            "boris_rule", triggers queuing for CLAUDE.md rule injection.
        path: Path to the feedback JSON file.
        accepted_habits_path: Path to the accepted-habits queue file.
        accepted_boris_path: Path to the accepted-boris queue file.
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
        item_type=item_type,
    )
    data[item_id] = entry
    save_feedback(data, path)

    # Queue habit items for skill scaffold generation
    if item_type == "habit":
        _queue_accepted_habit(item_id, accepted_habits_path)

    # Queue boris_rule items for CLAUDE.md injection
    if item_type == "boris_rule":
        _queue_accepted_boris(item_id, accepted_boris_path)

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
    auto-dismissed with a 1-week exponential back-off — deliberately shorter
    than a manual dismissal's 4-week base (marked ``implicit=True`` so it can
    be distinguished in audits).

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
        # Auto-suppress with a SHORTER base than a manual dismiss: implicit
        # means "not acted on in N surfaces", NOT "human said no". The 4-week
        # base was locking fresh real signal out of the improvement queue for
        # a month (2026-07-02 audit: live count=4 boris candidate suppressed
        # to 07-30). 1 week still stops surfacing-flap; repeated ignores
        # still back off exponentially (1 -> 2 -> 4 weeks).
        dismiss_count = entry.get("dismiss_count", 0) + 1
        effective_weeks = 1 * (2 ** (dismiss_count - 1))
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
# Amnesty helper
# ---------------------------------------------------------------------------

def clear_implicit_suppressions(
    path: Path = DEFAULT,
    now: datetime | None = None,
) -> list[str]:
    """Remove implicit suppression markers caused by the missing UI handle.

    Items that were auto-suppressed because they were surfaced 5 times without
    any user action (``implicit=True``) carry an invalid signal — the user
    never had a real accept/dismiss handle.  This one-shot helper resets them
    to ``surfaced`` status so they re-enter the queue on the next build.

    Only touches entries where ``implicit is True`` AND status is ``dismissed``.
    Explicit dismissals (``implicit`` absent or False) are never touched.

    Args:
        path: Path to the feedback JSON file.
        now: Unused; kept for DI-compatible signature.

    Returns:
        List of item IDs that were cleared.
    """
    data = load_feedback(path)
    cleared: list[str] = []
    for item_id, entry in data.items():
        if entry.get("implicit") is True and entry.get("status") == "dismissed":
            entry["status"] = "surfaced"
            entry["suppress_until"] = None
            entry.pop("implicit", None)
            cleared.append(item_id)
    if cleared:
        save_feedback(data, path)
    return cleared


# ---------------------------------------------------------------------------
# Review: frictionless human-review CLI (breaks bootstrap deadlock)
# ---------------------------------------------------------------------------

def _load_queue(queue_path: Path = QUEUE_PATH) -> list[dict[str, Any]]:
    """Load improvement-queue.json safely.

    Args:
        queue_path: Path to the queue JSON file.

    Returns:
        List of item dicts, empty on any error.
    """
    data = load_json_tolerant(queue_path, [])
    return data if isinstance(data, list) else []


def _load_verdicts(path: Path = REVIEW_VERDICTS_PATH) -> dict[str, Any]:
    """Load review verdicts store.

    Args:
        path: Path to the review-verdicts JSON file.

    Returns:
        Mapping of item_id -> verdict entry dict.
    """
    data = load_json_tolerant(path, {})
    return data if isinstance(data, dict) else {}


def _save_verdicts(data: dict, path: Path = REVIEW_VERDICTS_PATH) -> None:
    """Atomically write verdicts to *path*.

    Args:
        data: The full verdicts mapping.
        path: Target file path.
    """
    atomic_write_json(path, data)


def _short_id(item_id: str) -> str:
    """Derive a short (8-char) id from a full queue item id for CLI brevity.

    Uses last 8 non-hyphen chars to maximise discriminating power across similar ids.

    Args:
        item_id: Full item id string.

    Returns:
        Short id string (8 chars, alphanumeric from the tail of the id).
    """
    import hashlib
    return hashlib.md5(item_id.encode()).hexdigest()[:8]


def _pending_items(
    queue_path: Path = QUEUE_PATH,
    feedback_path: Path = DEFAULT,
    verdicts_path: Path = REVIEW_VERDICTS_PATH,
) -> list[dict[str, Any]]:
    """Return pending-review items sorted by score descending, skipping reviewed.

    An item qualifies for review when:
    - Its queue status is NOT ``auto_apply`` (those run automatically), AND
    - It has NOT been previously accepted/rejected via ``review --accept/--reject``.

    Items already marked accepted/dismissed in the feedback store are also excluded.

    Args:
        queue_path: Path to improvement-queue.json.
        feedback_path: Path to suggestion-feedback.json.
        verdicts_path: Path to review-verdicts.json.

    Returns:
        List of raw queue item dicts, sorted by score descending.
    """
    items = _load_queue(queue_path)
    feedback = load_feedback(feedback_path)
    verdicts = _load_verdicts(verdicts_path)

    result = []
    for it in items:
        iid = it.get("id", "")
        # Skip auto_apply items — they don't need human review
        if it.get("status") == "auto_apply":
            continue
        # Skip if already given a verdict via review CLI
        if iid in verdicts:
            continue
        # Skip if explicitly accepted or dismissed via old accept/dismiss CLI
        fb = feedback.get(iid, {})
        if fb.get("status") in ("accepted", "dismissed"):
            continue
        result.append(it)

    result.sort(key=lambda x: float(x.get("score", 0)), reverse=True)
    return result


def review_list(
    queue_path: Path = QUEUE_PATH,
    feedback_path: Path = DEFAULT,
    verdicts_path: Path = REVIEW_VERDICTS_PATH,
) -> None:
    """Print top-3 pending items with accept/reject commands to stdout.

    Args:
        queue_path: Path to improvement-queue.json.
        feedback_path: Path to suggestion-feedback.json.
        verdicts_path: Path to review-verdicts.json.
    """
    items = _pending_items(queue_path, feedback_path, verdicts_path)

    # Auto-reject items below score threshold — no human review needed for low-quality items
    low_score = [it for it in items if float(it.get("score", 0)) < AUTO_REJECT_SCORE_THRESHOLD]
    if low_score:
        for it in low_score:
            item_id = it.get("id", "")
            dismiss(item_id, weeks=4, path=feedback_path)
            _patch_queue_item_status(item_id, "dismissed", queue_path)
        items = [it for it in items if float(it.get("score", 0)) >= AUTO_REJECT_SCORE_THRESHOLD]
        print(f"[auto-reject] {len(low_score)} item(s) with score < {AUTO_REJECT_SCORE_THRESHOLD} dismissed silently.\n")

    top = items[:3]

    if not top:
        print("No pending items for review.")
        return

    print(f"Pending review — top {len(top)} item(s):\n")
    for it in top:
        iid = it.get("id", "")
        sid = _short_id(iid)
        itype = it.get("type", "?")
        score = it.get("score", 0.0)
        verdict = it.get("judge_verdict", "") or "unscored"
        desc = it.get("description", "")
        preview = (desc[:90] + "...") if len(desc) > 90 else desc
        print(f"  [{sid}]  type={itype}  score={score:.3f}  judge={verdict}")
        print(f"         {preview}")
        print()

    print("Commands:")
    for it in top:
        sid = _short_id(it.get("id", ""))
        print(f"  python suggestion_feedback.py review --accept {sid}")
        print(f"  python suggestion_feedback.py review --reject {sid}")
    print()
    print("(Or use the short-id from above with either flag.)")


def review_accept(
    short_id: str,
    queue_path: Path = QUEUE_PATH,
    feedback_path: Path = DEFAULT,
    verdicts_path: Path = REVIEW_VERDICTS_PATH,
    accepted_habits_path: Path = ACCEPTED_HABITS_PATH,
    accepted_boris_path: Path = ACCEPTED_BORIS_PATH,
    now: datetime | None = None,
) -> None:
    """Accept a queue item by its short id.

    Effects:
    1. Writes ``"accepted"`` verdict to review-verdicts.json (idempotent).
       This is the canonical precision-stream entry — ``effectiveness_tracker.is_resolved()``
       does an exact lookup here, so there is zero risk of substring overcounting.
    2. Calls ``accept()`` so the item is marked accepted in suggestion-feedback.json
       and appended to accepted-boris.json / accepted-habits.json per type.
    3. Updates the queue item status to ``"accepted"`` in improvement-queue.json via
       a direct JSON patch (no rebuild, preserves all judge fields).

    Args:
        short_id: 8-char short id from ``review --list``.
        queue_path: Path to improvement-queue.json.
        feedback_path: Path to suggestion-feedback.json.
        verdicts_path: Path to review-verdicts.json (precision-stream store).
        accepted_habits_path: Path to accepted-habits.json.
        accepted_boris_path: Path to accepted-boris.json.
        now: Timestamp override for tests.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    items = _load_queue(queue_path)
    verdicts = _load_verdicts(verdicts_path)

    # Resolve short_id -> full item
    matched = next(
        (it for it in items if _short_id(it.get("id", "")) == short_id), None
    )
    if matched is None:
        print(f"Unknown id '{short_id}' — nothing to do.")
        return

    item_id = matched.get("id", "")
    item_type = matched.get("type")

    # Idempotency check
    if item_id in verdicts and verdicts[item_id].get("verdict") == "accepted":
        print(f"Already accepted '{short_id}' ({item_id}) — no-op.")
        return

    # 1. Record verdict (canonical precision-stream entry — exact lookup, no file sentinels)
    verdicts[item_id] = {
        "verdict": "accepted",
        "short_id": short_id,
        "ts": now.isoformat(),
        "type": item_type,
    }
    _save_verdicts(verdicts, verdicts_path)

    # 2. Mark accepted in suggestion-feedback.json (also writes accepted-boris/habits)
    accept(
        item_id,
        item_type=item_type,
        path=feedback_path,
        accepted_habits_path=accepted_habits_path,
        accepted_boris_path=accepted_boris_path,
        now=now,
    )

    # 3. Patch queue status without a full rebuild (preserves judge fields)
    _patch_queue_item_status(item_id, "accepted", queue_path)

    print(
        f"Accepted '{short_id}' ({item_id})  type={item_type}\n"
        f"  -> verdict recorded, suggestion-feedback updated,\n"
        f"     queue status=accepted, accepted-{'boris' if item_type == 'boris_rule' else 'habits'}.json updated."
    )


def review_reject(
    short_id: str,
    queue_path: Path = QUEUE_PATH,
    feedback_path: Path = DEFAULT,
    verdicts_path: Path = REVIEW_VERDICTS_PATH,
    now: datetime | None = None,
) -> None:
    """Reject a queue item by its short id.

    Effects:
    1. Writes verdict to review-verdicts.json (idempotent).
    2. Calls ``dismiss()`` so the item is suppressed in suggestion-feedback.json.
    3. Patches queue item status to ``"dismissed"``.

    Note: a reject records a NEGATIVE precision sample implicitly — the item
    remains unresolved (no sentinel), so precision_by_type will count it as
    unresolved once it appears in a queue snapshot.

    Args:
        short_id: 8-char short id from ``review --list``.
        queue_path: Path to improvement-queue.json.
        feedback_path: Path to suggestion-feedback.json.
        verdicts_path: Path to review-verdicts.json.
        now: Timestamp override for tests.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    items = _load_queue(queue_path)
    verdicts = _load_verdicts(verdicts_path)

    matched = next(
        (it for it in items if _short_id(it.get("id", "")) == short_id), None
    )
    if matched is None:
        print(f"Unknown id '{short_id}' — nothing to do.")
        return

    item_id = matched.get("id", "")
    item_type = matched.get("type")

    # Idempotency check
    if item_id in verdicts and verdicts[item_id].get("verdict") == "rejected":
        print(f"Already rejected '{short_id}' ({item_id}) — no-op.")
        return

    # 1. Record verdict
    verdicts[item_id] = {
        "verdict": "rejected",
        "short_id": short_id,
        "ts": now.isoformat(),
        "type": item_type,
    }
    _save_verdicts(verdicts, verdicts_path)

    # 2. Dismiss (suppress) in suggestion-feedback.json
    dismiss(item_id, weeks=4, path=feedback_path, now=now)

    # 3. Patch queue status
    _patch_queue_item_status(item_id, "dismissed", queue_path)

    print(
        f"Rejected '{short_id}' ({item_id})  type={item_type}\n"
        f"  -> suggestion-feedback dismissed (4-week suppression), queue status=dismissed."
    )


def _patch_queue_item_status(
    item_id: str,
    new_status: str,
    queue_path: Path,
) -> None:
    """Patch a single item's status in the saved queue JSON without a full rebuild.

    This is a targeted JSON mutation — it carries forward ALL existing fields
    (including judge_score, judge_reason, judge_verdict) and only changes ``status``.
    It does NOT call build_queue or save_queue from self_improvement_queue.py,
    which would require rebuilding from all sources.  The carry_judge_path regen
    invariant is preserved because the file is read-and-patched in place: the next
    full rebuild via self_improvement_queue.py will still find judge fields in this
    file and carry them forward.

    Args:
        item_id: Full item id to update.
        new_status: Target status string (``"accepted"`` | ``"dismissed"``).
        queue_path: Path to improvement-queue.json.
    """
    data = load_json_tolerant(queue_path, [])
    if not isinstance(data, list):
        return
    changed = False
    for item in data:
        if item.get("id") == item_id:
            item["status"] = new_status
            changed = True
            break
    if changed:
        atomic_write_json(queue_path, data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Manage suggestion feedback (dismiss / accept / list / review)"
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
    a_parser.add_argument("--type", default=None, dest="item_type",
                          help="Item type (habit, boris_rule, etc.) — triggers side effects")

    sub.add_parser("list", help="List all feedback entries")
    sub.add_parser("amnesty", help="Clear implicit suppressions (UI had no handle)")

    rev_parser = sub.add_parser(
        "review",
        help="Frictionless human review: --list | --accept <short-id> | --reject <short-id>",
    )
    rev_group = rev_parser.add_mutually_exclusive_group(required=True)
    rev_group.add_argument(
        "--list", action="store_true",
        help="Show top-3 pending items with accept/reject commands",
    )
    rev_group.add_argument(
        "--accept", metavar="SHORT_ID",
        help="Accept item by 8-char short id (feeds precision stream)",
    )
    rev_group.add_argument(
        "--reject", metavar="SHORT_ID",
        help="Reject item by 8-char short id (suppresses for 4 weeks)",
    )

    args = parser.parse_args()

    if args.cmd == "dismiss":
        entry = dismiss(args.id, weeks=args.weeks)
        print(f"Dismissed '{args.id}' — suppressed until {entry['suppress_until']} "
              f"(dismiss #{entry['dismiss_count']})")

    elif args.cmd == "accept":
        # Auto-detect item_type from item_id prefix when not explicitly given
        item_type = args.item_type
        if item_type is None:
            if args.id.startswith("habit-"):
                item_type = "habit"
            elif args.id.startswith("boris_rule-") or args.id.startswith("boris-"):
                item_type = "boris_rule"
        entry = accept(args.id, item_type=item_type)
        print(f"Accepted '{args.id}' (type={item_type}) — suppression cleared (last_at {entry['last_at']})")

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

    elif args.cmd == "amnesty":
        cleared = clear_implicit_suppressions()
        if cleared:
            print(f"Cleared {len(cleared)} implicit suppression(s):")
            for item_id in cleared:
                print(f"  {item_id}")
        else:
            print("No implicit suppressions found.")

    elif args.cmd == "review":
        if args.list:
            review_list()
        elif args.accept:
            review_accept(args.accept)
        elif args.reject:
            review_reject(args.reject)


if __name__ == "__main__":
    main()
