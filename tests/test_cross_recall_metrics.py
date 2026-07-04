import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def _setup(tmp_path, monkeypatch, surfaced_rows, tracker, used_rows=None):
    import cross_recall_metrics as crm
    sp = tmp_path / "surfaced.jsonl"
    tp = tmp_path / "tracker.json"
    up = tmp_path / "used.jsonl"
    sp.write_text("\n".join(json.dumps(r) for r in surfaced_rows), encoding="utf-8")
    tp.write_text(json.dumps(tracker), encoding="utf-8")
    if used_rows:
        up.write_text("\n".join(json.dumps(r) for r in used_rows), encoding="utf-8")
    monkeypatch.setattr(crm, "SURFACED", sp)
    monkeypatch.setattr(crm, "TRACKER", tp)
    monkeypatch.setattr(crm, "USED", up)
    return crm


def test_rerecall_detected(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(days=1)).isoformat(timespec="seconds")
    surfaced = [{"ts": ts, "project": "P", "enriched": True,
                 "surfaced": [{"ns": "_shared", "id": "v1", "score": 0.9, "hc0": 2}]}]
    tracker = {"v1": {"hit_count": 4}}  # rose 2 -> 4 = re-recalled
    crm = _setup(tmp_path, monkeypatch, surfaced, tracker)
    m = crm.compute(days=30, now=now)
    assert m["surfaced"] == 1
    assert m["rerecalled"] == 1
    assert m["engaged"] == 1
    assert m["engagement_rate"] == 1.0


def test_no_rerecall_when_hitcount_unchanged(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(days=1)).isoformat(timespec="seconds")
    surfaced = [{"ts": ts, "project": "P", "enriched": False,
                 "surfaced": [{"ns": "_shared", "id": "v1", "score": 0.85, "hc0": 5}]}]
    tracker = {"v1": {"hit_count": 5}}  # unchanged
    crm = _setup(tmp_path, monkeypatch, surfaced, tracker)
    m = crm.compute(days=30, now=now)
    assert m["rerecalled"] == 0
    assert m["engaged"] == 0
    assert m["engagement_rate"] == 0.0


def test_explicit_used_counts_as_engaged(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(days=1)).isoformat(timespec="seconds")
    surfaced = [{"ts": ts, "project": "P", "enriched": True,
                 "surfaced": [{"ns": "_claude_meta", "id": "v9", "score": 0.88, "hc0": 0}]}]
    used = [{"ts": ts, "id": "v9"}]
    crm = _setup(tmp_path, monkeypatch, surfaced, {}, used)  # no re-recall, only explicit
    m = crm.compute(days=30, now=now)
    assert m["explicit_used"] == 1
    assert m["rerecalled"] == 0
    assert m["engaged"] == 1


def test_window_excludes_old_events(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=40)).isoformat(timespec="seconds")
    surfaced = [{"ts": old, "project": "P", "enriched": True,
                 "surfaced": [{"ns": "_shared", "id": "v1", "score": 0.9, "hc0": 0}]}]
    crm = _setup(tmp_path, monkeypatch, surfaced, {"v1": {"hit_count": 3}})
    m = crm.compute(days=30, now=now)
    assert m["events"] == 0
    assert m["surfaced"] == 0


def test_silent_event_tracked(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(days=1)).isoformat(timespec="seconds")
    surfaced = [{"ts": ts, "project": "P", "enriched": False, "surfaced": []}]
    crm = _setup(tmp_path, monkeypatch, surfaced, {})
    m = crm.compute(days=30, now=now)
    assert m["events"] == 1
    assert m["silent_events"] == 1
    assert m["surfaced"] == 0
    assert m["engagement_rate"] == 0.0


def test_per_project_breakdown(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(days=1)).isoformat(timespec="seconds")
    surfaced = [
        {"ts": ts, "project": "A", "enriched": True,
         "surfaced": [{"ns": "_shared", "id": "a1", "score": 0.9, "hc0": 0}]},
        {"ts": ts, "project": "B", "enriched": True,
         "surfaced": [{"ns": "_shared", "id": "b1", "score": 0.9, "hc0": 0}]},
    ]
    tracker = {"a1": {"hit_count": 2}, "b1": {"hit_count": 0}}  # only A engaged
    crm = _setup(tmp_path, monkeypatch, surfaced, tracker)
    m = crm.compute(days=30, now=now)
    assert m["per_project"]["A"]["engaged"] == 1
    assert m["per_project"]["B"]["engaged"] == 0


def test_mojibake_project_name_cleaned(tmp_path, monkeypatch):
    """Mojibake project names must be repaired to clean Cyrillic keys."""
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(days=1)).isoformat(timespec="seconds")
    # Construct the mojibake string programmatically (UTF-8 of {{PRIVATE_NS}} read as cp1251)
    broken_name = "".join(chr(c) for c in [
        0x420, 0x452, 0x421, 0x403, 0x420, 0x451, 0x421, 0x403,
        0x421, 0x201a, 0x420, 0xb5, 0x420, 0x405, 0x421, 0x201a,
        0x5f,
        0x420, 0x40e, 0x421, 0x201a, 0x420, 0x455, 0x420, 0x451, 0x420, 0xbb,
    ])
    surfaced = [{"ts": ts, "project": broken_name, "enriched": True,
                 "surfaced": [{"ns": "_shared", "id": "v1", "score": 0.9, "hc0": 0}]}]
    crm = _setup(tmp_path, monkeypatch, surfaced, {})
    m = crm.compute(days=30, now=now)
    # The broken key must NOT appear in per_project
    assert broken_name not in m["per_project"]
    # A clean Cyrillic key must appear (core Cyrillic U+0410-U+044F)
    assert any(
        any(0x0410 <= ord(c) <= 0x044F for c in k)
        for k in m["per_project"]
    )


def test_clean_project_name_not_altered(tmp_path, monkeypatch):
    """Normal ASCII project names must not be touched by the guard."""
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(days=1)).isoformat(timespec="seconds")
    surfaced = [{"ts": ts, "project": "Facturka.bg", "enriched": True,
                 "surfaced": [{"ns": "_shared", "id": "v2", "score": 0.88, "hc0": 0}]}]
    crm = _setup(tmp_path, monkeypatch, surfaced, {})
    m = crm.compute(days=30, now=now)
    assert "Facturka.bg" in m["per_project"]


# ---------------------------------------------------------------------------
# NEW TESTS — unique_engaged dedup (Bug fix 2026-06-10)
# ---------------------------------------------------------------------------

def test_unique_engaged_no_double_count(tmp_path, monkeypatch):
    """Same vector surfaced in two separate session events must count as 1 engaged,
    not 2 (double-count bug fix)."""
    now = datetime.now(timezone.utc)
    ts1 = (now - timedelta(days=2)).isoformat(timespec="seconds")
    ts2 = (now - timedelta(days=1)).isoformat(timespec="seconds")
    # 'v1' surfaced in two different sessions, hc0=1 both times
    surfaced = [
        {"ts": ts1, "project": "P", "enriched": True,
         "surfaced": [{"ns": "_shared", "id": "v1", "score": 0.9, "hc0": 1}]},
        {"ts": ts2, "project": "P", "enriched": True,
         "surfaced": [{"ns": "_shared", "id": "v1", "score": 0.88, "hc0": 1}]},
    ]
    # v1 was recalled again (hc_now=3 > hc0=1 in both events)
    tracker = {"v1": {"hit_count": 3}}
    crm = _setup(tmp_path, monkeypatch, surfaced, tracker)
    m = crm.compute(days=30, now=now)
    # surfaced count is 2 (two occurrences), but unique engaged is 1
    assert m["surfaced"] == 2
    assert m["rerecalled"] == 1, "same id in two events must count as 1 re-recall"
    assert m["engaged"] == 1, "double-count eliminated"
    assert m["unique_engaged"] == 1


def test_unique_engaged_field_present(tmp_path, monkeypatch):
    """unique_engaged must always be present in output (backwards-compat new field)."""
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(days=1)).isoformat(timespec="seconds")
    surfaced = [{"ts": ts, "project": "P", "enriched": True,
                 "surfaced": [{"ns": "_shared", "id": "v1", "score": 0.9, "hc0": 0}]}]
    crm = _setup(tmp_path, monkeypatch, surfaced, {"v1": {"hit_count": 2}})
    m = crm.compute(days=30, now=now)
    assert "unique_engaged" in m


def test_unique_engaged_two_different_ids_count_separately(tmp_path, monkeypatch):
    """Two different vectors both re-recalled must count as 2 unique_engaged."""
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(days=1)).isoformat(timespec="seconds")
    surfaced = [{"ts": ts, "project": "P", "enriched": True,
                 "surfaced": [
                     {"ns": "_shared", "id": "va", "score": 0.9, "hc0": 0},
                     {"ns": "_shared", "id": "vb", "score": 0.85, "hc0": 0},
                 ]}]
    tracker = {"va": {"hit_count": 2}, "vb": {"hit_count": 1}}
    crm = _setup(tmp_path, monkeypatch, surfaced, tracker)
    m = crm.compute(days=30, now=now)
    assert m["rerecalled"] == 2
    assert m["unique_engaged"] == 2


def test_engagement_rate_corrected_with_dedup(tmp_path, monkeypatch):
    """Engagement rate must use unique engaged / total surfaced (not raw event count)."""
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(days=1)).isoformat(timespec="seconds")
    # v1 surfaced 6 times (1 per event), re-recalled each time => old code: 6 engaged
    surfaced = [
        {"ts": ts, "project": "P", "enriched": True,
         "surfaced": [{"ns": "_shared", "id": "v1", "score": 0.9, "hc0": 1}]}
        for _ in range(6)
    ]
    tracker = {"v1": {"hit_count": 5}}  # hc_now(5) > hc0(1) — re-recalled
    crm = _setup(tmp_path, monkeypatch, surfaced, tracker)
    m = crm.compute(days=30, now=now)
    # surfaced=6, unique engaged=1 → rate = 1/6 ≈ 0.167, NOT 1.0
    assert m["surfaced"] == 6
    assert m["engaged"] == 1
    assert m["engagement_rate"] == round(1 / 6, 3)


# ---------------------------------------------------------------------------
# NEW TESTS — type_filter for cross_project_search (Bug fix 2026-06-10)
# ---------------------------------------------------------------------------

def test_filter_meaningful_excludes_raw_passage():
    """filter_meaningful() must exclude raw_passage and typeless vectors."""
    import sys
    sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))
    from cross_project_search import filter_meaningful

    hits = [
        {"id": "a", "metadata": {"type": "promoted", "text": "lesson"}},
        {"id": "b", "metadata": {"type": "raw_passage", "text": "noise"}},
        {"id": "c", "metadata": {"type": "learning", "text": "lesson"}},
        {"id": "d", "metadata": {}, "score": 0.9},                         # typeless
        {"id": "e", "metadata": {"type": "antipattern", "text": "pattern"}},
    ]
    result = filter_meaningful(hits)
    ids = [h["id"] for h in result]
    assert "a" in ids
    assert "c" in ids
    assert "e" in ids
    assert "b" not in ids, "raw_passage must be excluded"
    assert "d" not in ids, "typeless vector must be excluded"


def test_filter_meaningful_all_meaningful_types():
    """All six meaningful types must pass through filter_meaningful()."""
    import sys
    sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))
    from cross_project_search import filter_meaningful, MEANINGFUL_TYPES

    hits = [
        {"id": t, "metadata": {"type": t}} for t in MEANINGFUL_TYPES
    ]
    result = filter_meaningful(hits)
    assert len(result) == len(MEANINGFUL_TYPES)


# ---------------------------------------------------------------------------
# NEW TESTS — project_threshold_bump (per-project suppression decay)
# ---------------------------------------------------------------------------

def test_project_threshold_bump_applies_when_chronic_zero_engagement():
    """surfaced >= min_surfaced AND engaged == 0 -> bump returned."""
    import cross_recall_metrics as crm
    metrics = {"per_project": {"higgsfield.ai": {"surfaced": 90, "engaged": 0}}}
    assert crm.project_threshold_bump(metrics, "higgsfield.ai") == 0.08


def test_project_threshold_bump_zero_when_engaged():
    """Any engagement (even 1) must suppress the bump entirely."""
    import cross_recall_metrics as crm
    metrics = {"per_project": {"Move": {"surfaced": 12, "engaged": 6}}}
    assert crm.project_threshold_bump(metrics, "Move") == 0.0


def test_project_threshold_bump_zero_below_min_surfaced():
    """Below min_surfaced -> no bump, even with 0 engagement (too little data)."""
    import cross_recall_metrics as crm
    metrics = {"per_project": {"Autoagency": {"surfaced": 4, "engaged": 0}}}
    assert crm.project_threshold_bump(metrics, "Autoagency") == 0.0


def test_project_threshold_bump_zero_missing_project():
    """Project absent from per_project -> 0.0, never raises."""
    import cross_recall_metrics as crm
    metrics = {"per_project": {"Facturka.bg": {"surfaced": 30, "engaged": 5}}}
    assert crm.project_threshold_bump(metrics, "NeverSeenProject") == 0.0


def test_project_threshold_bump_zero_missing_keys_tolerant():
    """Missing 'per_project' key, empty metrics dict, or None must not raise."""
    import cross_recall_metrics as crm
    assert crm.project_threshold_bump({}, "X") == 0.0
    assert crm.project_threshold_bump({"per_project": {}}, "X") == 0.0
    assert crm.project_threshold_bump({"per_project": {"X": {}}}, "X") == 0.0


def test_project_threshold_bump_custom_thresholds():
    """min_surfaced/bump overrides are honored."""
    import cross_recall_metrics as crm
    metrics = {"per_project": {"P": {"surfaced": 10, "engaged": 0}}}
    assert crm.project_threshold_bump(metrics, "P", min_surfaced=10, bump=0.05) == 0.05
    assert crm.project_threshold_bump(metrics, "P", min_surfaced=11) == 0.0


def test_silent_top_p50_from_near_miss_events(tmp_path, monkeypatch):
    """Silent events with top_score feed the near-miss median; legacy rows skip."""
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(days=1)).isoformat(timespec="seconds")
    rows = [
        {"ts": ts, "project": "P", "enriched": False, "surfaced": [], "top_score": 0.58},
        {"ts": ts, "project": "P", "enriched": False, "surfaced": [], "top_score": 0.62},
        {"ts": ts, "project": "P", "enriched": False, "surfaced": []},  # legacy row
    ]
    crm = _setup(tmp_path, monkeypatch, rows, {})
    m = crm.compute(days=30, now=now)
    assert m["silent_events"] == 3
    assert m["silent_top_p50"] == 0.62  # median of [0.58, 0.62] -> upper mid


def test_silent_top_p50_zero_when_no_scored_silent_events(tmp_path, monkeypatch):
    """Only legacy silent rows (no top_score) -> 0.0, and surfaced events don't count."""
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(days=1)).isoformat(timespec="seconds")
    rows = [
        {"ts": ts, "project": "P", "enriched": False, "surfaced": []},
        {"ts": ts, "project": "P", "enriched": False, "top_score": 0.9,
         "surfaced": [{"ns": "_shared", "id": "v1", "score": 0.9, "hc0": 0}]},
    ]
    crm = _setup(tmp_path, monkeypatch, rows, {})
    m = crm.compute(days=30, now=now)
    assert m["silent_top_p50"] == 0.0
