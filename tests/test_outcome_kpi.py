"""Tests for outcome_kpi.py — Outcome KPI: measures results, not activity."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
WINDOW = 7  # default window_days


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(clusters: list[dict]) -> dict:
    return {"clusters": clusters}


def _cluster(
    cluster_id: str,
    first_seen: datetime,
    examples: list[datetime],
) -> dict:
    """Build a minimal cluster dict."""
    return {
        "id": cluster_id,
        "project": "test-proj",
        "representative": "test",
        "first_seen": first_seen.isoformat(),
        "last_seen": examples[-1].isoformat() if examples else first_seen.isoformat(),
        "examples": [{"text": f"ex-{i}", "seen": dt.isoformat()} for i, dt in enumerate(examples)],
        "status": "open",
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# compute_repeat_rate — within window
# ---------------------------------------------------------------------------


def test_repeat_rate_no_clusters():
    """Empty state -> 0 corrections, 0 repeats, rate 0.0."""
    from outcome_kpi import compute_repeat_rate

    result = compute_repeat_rate({}, now=NOW)
    assert result == {"total": 0, "repeats": 0, "rate": 0.0}


def test_repeat_rate_zero_corrections_in_window():
    """Cluster examples all before window -> 0 corrections."""
    from outcome_kpi import compute_repeat_rate

    old_ts = NOW - timedelta(days=10)
    state = _make_state([_cluster("c1", first_seen=old_ts, examples=[old_ts])])
    result = compute_repeat_rate(state, window_days=WINDOW, now=NOW)
    assert result["total"] == 0
    assert result["repeats"] == 0
    assert result["rate"] == 0.0


def test_repeat_rate_new_cluster_in_window_not_a_repeat():
    """Cluster first_seen is within the window -> correction is NOT a repeat."""
    from outcome_kpi import compute_repeat_rate

    # first_seen = 2 days ago (within window), example also 2 days ago
    recent = NOW - timedelta(days=2)
    state = _make_state([_cluster("c1", first_seen=recent, examples=[recent])])
    result = compute_repeat_rate(state, window_days=WINDOW, now=NOW)
    assert result["total"] == 1
    assert result["repeats"] == 0
    assert result["rate"] == 0.0


def test_repeat_rate_old_cluster_example_in_window_is_repeat():
    """Cluster first_seen is before window, new example within window -> repeat."""
    from outcome_kpi import compute_repeat_rate

    old = NOW - timedelta(days=14)
    recent = NOW - timedelta(days=1)
    state = _make_state([_cluster("c1", first_seen=old, examples=[recent])])
    result = compute_repeat_rate(state, window_days=WINDOW, now=NOW)
    assert result["total"] == 1
    assert result["repeats"] == 1
    assert result["rate"] == 1.0


def test_repeat_rate_mixed_clusters():
    """Two clusters: one repeat, one new."""
    from outcome_kpi import compute_repeat_rate

    old = NOW - timedelta(days=20)
    recent_ex = NOW - timedelta(days=2)
    new_cluster_start = NOW - timedelta(days=3)

    state = _make_state([
        _cluster("old-c", first_seen=old, examples=[recent_ex]),     # repeat
        _cluster("new-c", first_seen=new_cluster_start, examples=[recent_ex]),  # new
    ])
    result = compute_repeat_rate(state, window_days=WINDOW, now=NOW)
    assert result["total"] == 2
    assert result["repeats"] == 1
    assert result["rate"] == pytest.approx(0.5, abs=0.001)


def test_repeat_rate_example_exactly_at_window_boundary_excluded():
    """Example at exactly window_start (= NOW - window_days) is excluded (< not <=)."""
    from outcome_kpi import compute_repeat_rate

    old = NOW - timedelta(days=20)
    at_boundary = NOW - timedelta(days=WINDOW)
    state = _make_state([_cluster("c1", first_seen=old, examples=[at_boundary])])
    result = compute_repeat_rate(state, window_days=WINDOW, now=NOW)
    # at_boundary == window_start, which is NOT >= window_start + 1ns,
    # our condition is `seen < window_start` so at boundary: seen == window_start -> NOT excluded
    # Let's verify: window_start = NOW - 7d; seen == window_start -> seen >= window_start -> included
    assert result["total"] == 1
    assert result["repeats"] == 1


def test_repeat_rate_multiple_examples_same_cluster():
    """Multiple examples in window, all from old cluster -> all repeats."""
    from outcome_kpi import compute_repeat_rate

    old = NOW - timedelta(days=30)
    examples = [NOW - timedelta(days=i) for i in range(1, 5)]  # 4 recent
    state = _make_state([_cluster("c1", first_seen=old, examples=examples)])
    result = compute_repeat_rate(state, window_days=WINDOW, now=NOW)
    assert result["total"] == 4
    assert result["repeats"] == 4
    assert result["rate"] == 1.0


def test_repeat_rate_invalid_seen_skipped():
    """Examples with invalid seen timestamps are skipped gracefully."""
    from outcome_kpi import compute_repeat_rate

    old = NOW - timedelta(days=20)
    state = _make_state([{
        "id": "c1",
        "project": "p",
        "first_seen": old.isoformat(),
        "examples": [
            {"text": "a", "seen": "not-a-date"},
            {"text": "b", "seen": (NOW - timedelta(days=1)).isoformat()},
        ],
    }])
    result = compute_repeat_rate(state, window_days=WINDOW, now=NOW)
    assert result["total"] == 1
    assert result["repeats"] == 1


def test_repeat_rate_invalid_first_seen_not_counted_as_repeat():
    """Cluster with invalid first_seen: can't prove it's old, so not a repeat."""
    from outcome_kpi import compute_repeat_rate

    recent_ex = NOW - timedelta(days=1)
    state = _make_state([{
        "id": "c1",
        "project": "p",
        "first_seen": "bad-date",
        "examples": [{"text": "a", "seen": recent_ex.isoformat()}],
    }])
    result = compute_repeat_rate(state, window_days=WINDOW, now=NOW)
    assert result["total"] == 1
    assert result["repeats"] == 0


# ---------------------------------------------------------------------------
# compute_kpi — file-level DI tests
# ---------------------------------------------------------------------------


def test_compute_kpi_all_files_missing(tmp_path):
    """All inputs missing -> sensible defaults, no exception."""
    from outcome_kpi import compute_kpi

    result = compute_kpi(
        now=NOW,
        state_path=tmp_path / "state.json",
        incidents_path=tmp_path / "incidents.json",
        recall_path=tmp_path / "recall.json",
        ledger_path=tmp_path / "ledger.jsonl",
        queue_path=tmp_path / "queue.json",
        history_path=tmp_path / "history.jsonl",
    )
    assert result["window_days"] == 7
    assert result["repeat_corrections"] == {"total": 0, "repeats": 0, "rate": 0.0}
    assert result["recall_engagement"] == {"surfaced": None, "engaged": None, "rate": None}
    assert result["apply_funnel"]["applied_30d"] == 0
    assert result["apply_funnel"]["rolled_back_30d"] == 0
    assert result["apply_funnel"]["queue_depth"] == 0
    assert result["apply_funnel"]["open_incidents"] == 0
    assert result["trend"] == "insufficient_data"


def test_compute_kpi_recall_engagement_from_file(tmp_path):
    """Recall metrics are read correctly from cross-recall-metrics.json."""
    from outcome_kpi import compute_kpi

    recall = {"surfaced": 100, "engaged": 10, "engagement_rate": 0.1}
    _write_json(tmp_path / "recall.json", recall)

    result = compute_kpi(
        now=NOW,
        state_path=tmp_path / "state.json",
        incidents_path=tmp_path / "incidents.json",
        recall_path=tmp_path / "recall.json",
        ledger_path=tmp_path / "ledger.jsonl",
        queue_path=tmp_path / "queue.json",
        history_path=tmp_path / "history.jsonl",
    )
    eng = result["recall_engagement"]
    assert eng["surfaced"] == 100
    assert eng["engaged"] == 10
    assert eng["rate"] == pytest.approx(0.1)


def test_compute_kpi_apply_funnel_from_ledger(tmp_path):
    """Ledger entries in 30d window are counted correctly."""
    from outcome_kpi import compute_kpi

    recent = (NOW - timedelta(days=5)).isoformat()
    old = (NOW - timedelta(days=40)).isoformat()
    ledger = [
        {"ts": recent, "item_id": "a", "item_type": "boris_rule", "tier": 1, "rolled_back": False},
        {"ts": recent, "item_id": "b", "item_type": "boris_rule", "tier": 1, "rolled_back": True},
        {"ts": old, "item_id": "c", "item_type": "habit", "tier": 2, "rolled_back": False},
    ]
    _write_jsonl(tmp_path / "ledger.jsonl", ledger)

    result = compute_kpi(
        now=NOW,
        state_path=tmp_path / "state.json",
        incidents_path=tmp_path / "incidents.json",
        recall_path=tmp_path / "recall.json",
        ledger_path=tmp_path / "ledger.jsonl",
        queue_path=tmp_path / "queue.json",
        history_path=tmp_path / "history.jsonl",
    )
    af = result["apply_funnel"]
    assert af["applied_30d"] == 1      # only recent non-rolled-back
    assert af["rolled_back_30d"] == 1  # only recent rolled-back
    # old entry outside 30d window is excluded


def test_compute_kpi_queue_depth(tmp_path):
    """queue_depth is length of improvement-queue.json array."""
    from outcome_kpi import compute_kpi

    queue = [{"id": f"item-{i}"} for i in range(5)]
    _write_json(tmp_path / "queue.json", queue)

    result = compute_kpi(
        now=NOW,
        state_path=tmp_path / "state.json",
        incidents_path=tmp_path / "incidents.json",
        recall_path=tmp_path / "recall.json",
        ledger_path=tmp_path / "ledger.jsonl",
        queue_path=tmp_path / "queue.json",
        history_path=tmp_path / "history.jsonl",
    )
    assert result["apply_funnel"]["queue_depth"] == 5


def test_compute_kpi_open_incidents(tmp_path):
    """open_incidents from incidents.json."""
    from outcome_kpi import compute_kpi

    incidents = {"generated": NOW.isoformat(), "open": [{"id": "x"}, {"id": "y"}], "resolved_recent": []}
    _write_json(tmp_path / "incidents.json", incidents)

    result = compute_kpi(
        now=NOW,
        state_path=tmp_path / "state.json",
        incidents_path=tmp_path / "incidents.json",
        recall_path=tmp_path / "recall.json",
        ledger_path=tmp_path / "ledger.jsonl",
        queue_path=tmp_path / "queue.json",
        history_path=tmp_path / "history.jsonl",
    )
    assert result["apply_funnel"]["open_incidents"] == 2


# ---------------------------------------------------------------------------
# Trend logic
# ---------------------------------------------------------------------------


def test_trend_insufficient_data_no_history(tmp_path):
    """No history file -> insufficient_data."""
    from outcome_kpi import compute_kpi

    result = compute_kpi(
        now=NOW,
        state_path=tmp_path / "state.json",
        incidents_path=tmp_path / "incidents.json",
        recall_path=tmp_path / "recall.json",
        ledger_path=tmp_path / "ledger.jsonl",
        queue_path=tmp_path / "queue.json",
        history_path=tmp_path / "history.jsonl",
    )
    assert result["trend"] == "insufficient_data"


def test_trend_improving(tmp_path):
    """Current rate < previous rate -> improving."""
    from outcome_kpi import compute_kpi, _compute_trend

    prev_snap = {"generated": (NOW - timedelta(days=1)).isoformat(), "repeat_corrections": {"rate": 0.8}}
    _write_jsonl(tmp_path / "history.jsonl", [prev_snap])

    trend = _compute_trend(0.4, tmp_path / "history.jsonl")
    assert trend == "improving"


def test_trend_degrading(tmp_path):
    """Current rate > previous rate -> degrading."""
    from outcome_kpi import _compute_trend

    prev_snap = {"generated": (NOW - timedelta(days=1)).isoformat(), "repeat_corrections": {"rate": 0.2}}
    _write_jsonl(tmp_path / "history.jsonl", [prev_snap])

    trend = _compute_trend(0.6, tmp_path / "history.jsonl")
    assert trend == "degrading"


def test_trend_flat(tmp_path):
    """Rate within 0.01 delta -> flat."""
    from outcome_kpi import _compute_trend

    prev_snap = {"generated": (NOW - timedelta(days=1)).isoformat(), "repeat_corrections": {"rate": 0.5}}
    _write_jsonl(tmp_path / "history.jsonl", [prev_snap])

    trend = _compute_trend(0.505, tmp_path / "history.jsonl")
    assert trend == "flat"


# ---------------------------------------------------------------------------
# Persistence — same-day dedup
# ---------------------------------------------------------------------------


def test_persist_first_entry(tmp_path):
    """First persist creates a single-line history file."""
    from outcome_kpi import persist_kpi, _load_jsonl

    snap = {"generated": NOW.isoformat(), "repeat_corrections": {"rate": 0.3}}
    hist = tmp_path / "history.jsonl"
    latest = tmp_path / "latest.json"

    persist_kpi(snap, now=NOW, history_path=hist, latest_path=latest)

    rows = _load_jsonl(hist)
    assert len(rows) == 1
    assert rows[0]["repeat_corrections"]["rate"] == pytest.approx(0.3)
    assert latest.exists()


def test_persist_same_day_dedup(tmp_path):
    """Two persists on the same day -> only one history entry (replaced)."""
    from outcome_kpi import persist_kpi, _load_jsonl

    hist = tmp_path / "history.jsonl"
    latest = tmp_path / "latest.json"

    snap1 = {"generated": NOW.replace(hour=9).isoformat(), "repeat_corrections": {"rate": 0.5}}
    snap2 = {"generated": NOW.replace(hour=15).isoformat(), "repeat_corrections": {"rate": 0.3}}

    persist_kpi(snap1, now=NOW.replace(hour=9), history_path=hist, latest_path=latest)
    persist_kpi(snap2, now=NOW.replace(hour=15), history_path=hist, latest_path=latest)

    rows = _load_jsonl(hist)
    assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
    assert rows[0]["repeat_corrections"]["rate"] == pytest.approx(0.3)


def test_persist_different_days_append(tmp_path):
    """Persists on different days -> two separate history entries."""
    from outcome_kpi import persist_kpi, _load_jsonl

    hist = tmp_path / "history.jsonl"
    latest = tmp_path / "latest.json"

    day1 = NOW - timedelta(days=1)
    day2 = NOW

    snap1 = {"generated": day1.isoformat(), "repeat_corrections": {"rate": 0.7}}
    snap2 = {"generated": day2.isoformat(), "repeat_corrections": {"rate": 0.4}}

    persist_kpi(snap1, now=day1, history_path=hist, latest_path=latest)
    persist_kpi(snap2, now=day2, history_path=hist, latest_path=latest)

    rows = _load_jsonl(hist)
    assert len(rows) == 2


def test_persist_latest_is_atomic(tmp_path):
    """Latest file is correct after persist."""
    from outcome_kpi import persist_kpi, load_latest

    snap = {"generated": NOW.isoformat(), "repeat_corrections": {"rate": 0.25}, "trend": "flat"}
    hist = tmp_path / "history.jsonl"
    latest = tmp_path / "latest.json"

    persist_kpi(snap, now=NOW, history_path=hist, latest_path=latest)

    loaded = load_latest(latest)
    assert loaded is not None
    assert loaded["trend"] == "flat"


def test_load_latest_missing_returns_none(tmp_path):
    """load_latest on missing file returns None."""
    from outcome_kpi import load_latest

    assert load_latest(tmp_path / "nope.json") is None


# ---------------------------------------------------------------------------
# Corrupt / edge inputs
# ---------------------------------------------------------------------------


def test_compute_kpi_corrupt_recall(tmp_path):
    """Corrupt recall file -> nulls, no exception."""
    from outcome_kpi import compute_kpi

    (tmp_path / "recall.json").write_text("{broken json{{", encoding="utf-8")
    result = compute_kpi(
        now=NOW,
        state_path=tmp_path / "state.json",
        incidents_path=tmp_path / "incidents.json",
        recall_path=tmp_path / "recall.json",
        ledger_path=tmp_path / "ledger.jsonl",
        queue_path=tmp_path / "queue.json",
        history_path=tmp_path / "history.jsonl",
    )
    assert result["recall_engagement"]["surfaced"] is None


def test_compute_kpi_corrupt_ledger_line(tmp_path):
    """JSONL with a corrupt line -> that line is skipped, rest counted."""
    from outcome_kpi import compute_kpi

    recent = (NOW - timedelta(days=2)).isoformat()
    ledger_path = tmp_path / "ledger.jsonl"
    ledger_path.write_text(
        '{"ts": "' + recent + '", "item_id": "ok", "rolled_back": false}\n'
        "not-json-at-all\n",
        encoding="utf-8",
    )

    result = compute_kpi(
        now=NOW,
        state_path=tmp_path / "state.json",
        incidents_path=tmp_path / "incidents.json",
        recall_path=tmp_path / "recall.json",
        ledger_path=ledger_path,
        queue_path=tmp_path / "queue.json",
        history_path=tmp_path / "history.jsonl",
    )
    assert result["apply_funnel"]["applied_30d"] == 1
