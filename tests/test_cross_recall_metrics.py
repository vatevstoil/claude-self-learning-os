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
