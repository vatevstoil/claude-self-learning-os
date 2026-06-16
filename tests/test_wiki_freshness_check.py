"""Tests for wiki_freshness_check.py — check_graph max-date + future-clamp + staleness_kind."""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import wiki_freshness_check as wfc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_graph(tmp_path: Path, meta: dict) -> Path:
    """Write a knowledge_graph.json with the given meta block; return the path."""
    graph_dir = tmp_path / "graph"
    graph_dir.mkdir(parents=True, exist_ok=True)
    gf = graph_dir / "knowledge_graph.json"
    gf.write_text(json.dumps({"meta": meta}), encoding="utf-8")
    return gf


def _days_ago_date(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _days_future(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# check_graph — max-date logic (the CasinoScore bug)
# ---------------------------------------------------------------------------


def test_check_graph_takes_max_of_last_updated_and_generated(tmp_path: Path) -> None:
    """When last_updated is older than generated, we must NOT report last_updated's age.

    The old code returned age=20 (last_updated won because it was checked first).
    The fix returns the newest candidate.  The freshly-created file in tmp_path
    has mtime≈today (0d), which beats generated (6d), so the real assertion is:
    age must be strictly less than 20 (old code result) and >= 0.
    """
    _make_graph(tmp_path, {
        "last_updated": _days_ago_date(20),  # old — must NOT win
        "generated": _days_ago_date(6),       # newer meta date
    })
    exists, age, source, _hollow = wfc.check_graph(tmp_path)
    assert exists is True
    # With the fix, max() picks the newest among: last_updated(20d), generated(6d),
    # mtime(~0d because file was just created).  So age <= 6.
    assert age is not None and age <= 6, (
        f"Expected age <= 6 (last_updated=20d must not win), got {age}"
    )
    # Source must NOT be last_updated (the old buggy winner)
    assert source != "last_updated", (
        f"last_updated (20d) must not win over newer dates, but source={source!r}"
    )


def test_check_graph_takes_max_including_mtime(tmp_path: Path) -> None:
    """mtime newer than all meta fields should win."""
    gf = _make_graph(tmp_path, {
        "last_updated": _days_ago_date(15),
        "generated": _days_ago_date(15),
    })
    # Touch file to make mtime = today
    import os, time
    now_ts = time.time()
    os.utime(str(gf), (now_ts, now_ts))

    exists, age, source, _hollow = wfc.check_graph(tmp_path)
    assert exists is True
    assert age == 0, f"Expected 0 days (mtime=today wins), got {age}"
    assert source == "mtime"


def test_check_graph_future_date_clamped_to_today(tmp_path: Path) -> None:
    """Future dates in meta must be clamped to today (age >= 0)."""
    _make_graph(tmp_path, {
        "last_updated": _days_future(30),  # deadline, not last-activity
        "generated": _days_future(5),
    })
    exists, age, source, _hollow = wfc.check_graph(tmp_path)
    assert exists is True
    # After clamping both to today, mtime may be slightly in the past (seconds).
    # The important thing is age >= 0 and no negative age from the future date.
    assert age >= 0, f"Age must be >= 0 after clamp, got {age}"


def test_check_graph_missing(tmp_path: Path) -> None:
    exists, age, source, _hollow = wfc.check_graph(tmp_path)
    assert exists is False
    assert age is None
    assert source == "missing"


def test_check_graph_only_meta_no_mtime_field(tmp_path: Path) -> None:
    """Only last_updated in meta — must still work."""
    _make_graph(tmp_path, {"last_updated": _days_ago_date(3)})
    exists, age, source, _hollow = wfc.check_graph(tmp_path)
    assert exists is True
    # age should be <= 3 (mtime may be today because we just created it)
    assert age <= 3


def test_check_graph_all_future_falls_back_to_mtime(tmp_path: Path) -> None:
    """All meta dates in future → clamp → mtime is the real source."""
    _make_graph(tmp_path, {
        "last_updated": _days_future(10),
        "generated": _days_future(20),
    })
    exists, age, _, _hollow = wfc.check_graph(tmp_path)
    assert exists is True
    # After clamping futures to today, the mtime of a freshly-created file is ~today.
    assert age <= 1


# ---------------------------------------------------------------------------
# staleness_kind — distinguish stale_graph vs dead_log
# ---------------------------------------------------------------------------


def _make_report(
    *,
    status: str = "active",
    graph_exists: bool = True,
    graph_age: int | None = None,
    log_age: int | None = None,
) -> wfc.ProjectReport:
    r = wfc.ProjectReport("TestProj")
    r.status = status
    r.graph_exists = graph_exists
    r.graph_age_days = graph_age
    r.log_age_days = log_age
    return r


def test_staleness_kind_stale_graph(tmp_path: Path) -> None:
    r = _make_report(graph_age=20, log_age=3)
    assert r.staleness_kind(14) == "stale_graph"


def test_staleness_kind_dead_log(tmp_path: Path) -> None:
    r = _make_report(graph_age=3, log_age=30)
    assert r.staleness_kind(14) == "dead_log"


def test_staleness_kind_missing_graph(tmp_path: Path) -> None:
    r = _make_report(graph_exists=False)
    assert r.staleness_kind(14) == "missing_graph"


def test_staleness_kind_none_when_fresh(tmp_path: Path) -> None:
    r = _make_report(graph_age=3, log_age=3)
    assert r.staleness_kind(14) is None


def test_staleness_kind_none_when_dormant(tmp_path: Path) -> None:
    r = _make_report(status="dormant", graph_age=100, log_age=100)
    assert r.staleness_kind(14) is None


def test_staleness_kind_none_when_missing_wiki(tmp_path: Path) -> None:
    r = _make_report(status="missing-wiki")
    assert r.staleness_kind(14) is None


# ---------------------------------------------------------------------------
# to_dict backwards-compatibility
# ---------------------------------------------------------------------------


def test_to_dict_includes_staleness_kind_when_stale(tmp_path: Path) -> None:
    r = _make_report(graph_age=20, log_age=3)
    d = r.to_dict(14)
    assert "staleness_kind" in d
    assert d["staleness_kind"] == "stale_graph"
    # Legacy fields still present
    assert "project" in d
    assert "graph_age_days" in d


def test_to_dict_excludes_staleness_kind_when_fresh(tmp_path: Path) -> None:
    r = _make_report(graph_age=3, log_age=3)
    d = r.to_dict(14)
    # Fresh project: no staleness_kind key (backwards-compatible — don't add noise)
    assert "staleness_kind" not in d


# ---------------------------------------------------------------------------
# hollow-graph detection (2026-06-13: structural-only graphs reported FRESH)
# ---------------------------------------------------------------------------

def test_check_graph_hollow_detection(tmp_path: Path) -> None:
    gdir = tmp_path / "graph"; gdir.mkdir(parents=True, exist_ok=True)
    gf = gdir / "knowledge_graph.json"
    # Enriched graph (has critical_rules) -> NOT hollow.
    gf.write_text(json.dumps({"meta": {"generated": _days_ago_date(1)},
                              "critical_rules": ["r1", "r2"]}), encoding="utf-8")
    *_, hollow = wfc.check_graph(tmp_path)
    assert hollow is False
    # Structural-only / empty critical_rules -> hollow.
    gf.write_text(json.dumps({"meta": {"generated": _days_ago_date(1)},
                              "graph_source": "auto_graphify_structural",
                              "critical_rules": []}), encoding="utf-8")
    *_, hollow = wfc.check_graph(tmp_path)
    assert hollow is True


def test_hollow_graph_flagged_warn_and_kind() -> None:
    r = wfc.ProjectReport("X")
    r.graph_exists = True
    r.graph_age_days = 1  # fresh — would otherwise be OK
    r.graph_hollow = True
    assert r.flag(14) == wfc.STATUS_WARN
    assert r.staleness_kind(14) == "hollow_graph"
