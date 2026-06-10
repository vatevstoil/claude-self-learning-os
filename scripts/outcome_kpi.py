"""Outcome KPI — measures RESULTS, not activity.

Tracks three real learning signals:
  1. repeat_corrections  — does the same mistake recur? (learning failed = high rate)
  2. recall_engagement   — is surfaced memory actually used?
  3. apply_funnel        — are improvements applied, not rolled back, queue draining?

CLI:
  python outcome_kpi.py          # compute + persist + short stdout
  python outcome_kpi.py --show   # print last snapshot only, no write
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Default paths (all DI-injectable for tests)
# ---------------------------------------------------------------------------

_LOGS = Path.home() / ".claude" / "logs"
_DEFAULT_STATE = _LOGS / "incidents-state.json"
_DEFAULT_INCIDENTS = _LOGS / "incidents.json"
_DEFAULT_RECALL = _LOGS / "cross-recall-metrics.json"
_DEFAULT_LEDGER = _LOGS / "applied-ledger.jsonl"
_DEFAULT_QUEUE = _LOGS / "improvement-queue.json"
_DEFAULT_HISTORY = _LOGS / "outcome-kpi-history.jsonl"
_DEFAULT_LATEST = _LOGS / "outcome-kpi.json"


# ---------------------------------------------------------------------------
# Tolerant loaders — never raise on missing / corrupt input
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> Any:
    """Load JSON file; return None on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load JSONL file; skip corrupt lines; return [] on missing file."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass
    return rows


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------


def compute_repeat_rate(
    state: dict[str, Any],
    window_days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute correction repeat-rate from incidents-state clusters.

    A correction (example) is a *repeat* when:
      - its ``seen`` timestamp falls within the last ``window_days``, AND
      - it belongs to a cluster whose ``first_seen`` is BEFORE the window
        (i.e., the problem was already known — it recurred).

    Args:
        state: Parsed incidents-state.json dict (``{"clusters": [...]}``).
        window_days: Look-back window in days.
        now: Reference timestamp; defaults to UTC now.

    Returns:
        ``{"total": int, "repeats": int, "rate": float}``
    """
    if now is None:
        now = datetime.now(timezone.utc)

    window_start = now - timedelta(days=window_days)

    corrections_total = 0
    repeats = 0

    clusters = state.get("clusters", []) if isinstance(state, dict) else []
    for cluster in clusters:
        # Parse cluster first_seen
        first_seen_raw = cluster.get("first_seen", "")
        try:
            first_seen = datetime.fromisoformat(first_seen_raw)
            if first_seen.tzinfo is None:
                first_seen = first_seen.replace(tzinfo=timezone.utc)
        except Exception:
            first_seen = None

        for example in cluster.get("examples", []):
            seen_raw = example.get("seen", "")
            try:
                seen = datetime.fromisoformat(seen_raw)
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
            except Exception:
                continue  # skip unparseable timestamps

            # Only count examples within the window
            if seen < window_start:
                continue

            corrections_total += 1

            # It's a repeat if the cluster existed BEFORE the window
            if first_seen is not None and first_seen < window_start:
                repeats += 1

    rate = repeats / corrections_total if corrections_total > 0 else 0.0

    return {"total": corrections_total, "repeats": repeats, "rate": round(rate, 4)}


def _compute_recall_engagement(recall_path: Path) -> dict[str, Any]:
    """Extract recall engagement from cross-recall-metrics.json.

    Returns nulls when the file is missing or corrupt.
    """
    data = _load_json(recall_path)
    if not isinstance(data, dict):
        return {"surfaced": None, "engaged": None, "rate": None}
    return {
        "surfaced": data.get("surfaced"),
        "engaged": data.get("engaged"),
        "rate": data.get("engagement_rate"),
    }


def _compute_apply_funnel(
    ledger_path: Path,
    queue_path: Path,
    incidents_path: Path,
    now: datetime,
) -> dict[str, Any]:
    """Compute apply-funnel metrics.

    - applied_30d   : ledger entries whose ts is within 30 days and not rolled back
    - rolled_back_30d : ledger entries within 30 days that ARE rolled back
    - queue_depth   : number of items in improvement-queue.json
    - open_incidents: number of open incidents in incidents.json
    """
    cutoff_30d = now - timedelta(days=30)

    applied_30d = 0
    rolled_back_30d = 0
    for entry in _load_jsonl(ledger_path):
        ts_raw = entry.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if ts < cutoff_30d:
            continue
        if entry.get("rolled_back", False):
            rolled_back_30d += 1
        else:
            applied_30d += 1

    # queue_depth: improvement-queue.json is a JSON array
    queue_data = _load_json(queue_path)
    if isinstance(queue_data, list):
        queue_depth = len(queue_data)
    elif isinstance(queue_data, dict):
        # Some versions store items under a key
        queue_depth = len(queue_data.get("items", queue_data.get("queue", [])))
    else:
        queue_depth = 0

    # open_incidents
    incidents_data = _load_json(incidents_path)
    if isinstance(incidents_data, dict):
        open_incidents = len(incidents_data.get("open", []))
    else:
        open_incidents = 0

    return {
        "applied_30d": applied_30d,
        "rolled_back_30d": rolled_back_30d,
        "queue_depth": queue_depth,
        "open_incidents": open_incidents,
    }


def _compute_trend(current_rate: float, history_path: Path,
                   now: datetime | None = None) -> str:
    """Compare current repeat_rate with the last snapshot from a PREVIOUS day.

    Same-day rows are skipped: persist_kpi replaces today's row on re-runs, so
    comparing against it would mean comparing the snapshot with itself and the
    trend would lock to "flat" on any second run within a day.

    Returns one of: "improving", "degrading", "flat", "insufficient_data".
    """
    rows = _load_jsonl(history_path)
    if not rows:
        return "insufficient_data"

    today = _today_str(now) if now is not None else None
    prev = None
    for row in reversed(rows):
        if today is not None and str(row.get("generated", ""))[:10] == today:
            continue
        prev = row
        break
    if prev is None:
        return "insufficient_data"
    try:
        prev_rate = float(prev["repeat_corrections"]["rate"])
    except Exception:
        return "insufficient_data"

    delta = current_rate - prev_rate
    if abs(delta) < 0.01:
        return "flat"
    return "improving" if delta < 0 else "degrading"


# ---------------------------------------------------------------------------
# Main compute
# ---------------------------------------------------------------------------


def compute_kpi(
    now: datetime | None = None,
    state_path: Path = _DEFAULT_STATE,
    incidents_path: Path = _DEFAULT_INCIDENTS,
    recall_path: Path = _DEFAULT_RECALL,
    ledger_path: Path = _DEFAULT_LEDGER,
    queue_path: Path = _DEFAULT_QUEUE,
    history_path: Path = _DEFAULT_HISTORY,
) -> dict[str, Any]:
    """Compute the full outcome KPI snapshot.

    Args:
        now: Reference timestamp; defaults to UTC now.
        state_path: Path to incidents-state.json.
        incidents_path: Path to incidents.json.
        recall_path: Path to cross-recall-metrics.json.
        ledger_path: Path to applied-ledger.jsonl.
        queue_path: Path to improvement-queue.json.
        history_path: Path to outcome-kpi-history.jsonl (for trend).

    Returns:
        KPI dict with keys: generated, window_days, repeat_corrections,
        recall_engagement, apply_funnel, trend.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    window_days = 7

    # 1. Repeat corrections
    state_data = _load_json(state_path)
    if not isinstance(state_data, dict):
        state_data = {}
    repeat_corrections = compute_repeat_rate(state_data, window_days=window_days, now=now)

    # 2. Recall engagement
    recall_engagement = _compute_recall_engagement(recall_path)

    # 3. Apply funnel
    apply_funnel = _compute_apply_funnel(
        ledger_path=ledger_path,
        queue_path=queue_path,
        incidents_path=incidents_path,
        now=now,
    )

    # 4. Trend (reads history before we append; same-day rows skipped)
    trend = _compute_trend(repeat_corrections["rate"], history_path, now=now)

    return {
        "generated": now.isoformat(),
        "window_days": window_days,
        "repeat_corrections": repeat_corrections,
        "recall_engagement": recall_engagement,
        "apply_funnel": apply_funnel,
        "trend": trend,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _today_str(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def persist_kpi(
    snapshot: dict[str, Any],
    now: datetime,
    history_path: Path = _DEFAULT_HISTORY,
    latest_path: Path = _DEFAULT_LATEST,
) -> None:
    """Append snapshot to history (dedup same-day) and write latest atomically."""
    history_path.parent.mkdir(parents=True, exist_ok=True)

    today = _today_str(now)
    rows = _load_jsonl(history_path)

    # Dedup: if last row is from today, replace it
    if rows:
        last_ts_raw = rows[-1].get("generated", "")
        try:
            last_dt = datetime.fromisoformat(last_ts_raw)
            last_day = last_dt.strftime("%Y-%m-%d")
        except Exception:
            last_day = ""

        if last_day == today:
            rows[-1] = snapshot
            # Rewrite entire history file
            tmp = history_path.with_suffix(".tmp")
            try:
                tmp.write_text(
                    "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp, history_path)
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
        else:
            # Append new row
            with history_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    else:
        # First entry
        with history_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(snapshot, ensure_ascii=False) + "\n")

    # Write latest atomically
    tmp_latest = latest_path.with_suffix(".tmp")
    try:
        tmp_latest.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_latest, latest_path)
    except Exception:
        try:
            tmp_latest.unlink(missing_ok=True)
        except Exception:
            pass


def load_latest(latest_path: Path = _DEFAULT_LATEST) -> dict[str, Any] | None:
    """Load the last persisted KPI snapshot. Returns None if unavailable."""
    data = _load_json(latest_path)
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_snapshot(snapshot: dict[str, Any]) -> None:
    rc = snapshot.get("repeat_corrections", {})
    re_ = snapshot.get("recall_engagement", {})
    af = snapshot.get("apply_funnel", {})
    print(
        f"[outcome_kpi] {snapshot.get('generated', '?')} | "
        f"repeat_rate={rc.get('rate', '?')} "
        f"({rc.get('repeats', '?')}/{rc.get('total', '?')}) | "
        f"recall_eng={re_.get('rate', '?')} "
        f"({re_.get('engaged', '?')}/{re_.get('surfaced', '?')}) | "
        f"applied_30d={af.get('applied_30d', '?')} "
        f"rb={af.get('rolled_back_30d', '?')} "
        f"q={af.get('queue_depth', '?')} "
        f"open={af.get('open_incidents', '?')} | "
        f"trend={snapshot.get('trend', '?')}"
    )


def main() -> None:  # noqa: D401
    """Entry point."""
    show_only = "--show" in sys.argv

    if show_only:
        snap = load_latest()
        if snap:
            _print_snapshot(snap)
        else:
            print("[outcome_kpi] No snapshot found. Run without --show first.")
        return

    now = datetime.now(timezone.utc)
    snapshot = compute_kpi(now=now)
    persist_kpi(snapshot, now=now)
    _print_snapshot(snapshot)


if __name__ == "__main__":
    main()
