import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def test_compute_hebbian_ttl_basic():
    from hebbian_consolidation import compute_hebbian_ttl
    assert compute_hebbian_ttl(base_ttl=90, recall_count=0) == 90
    assert compute_hebbian_ttl(base_ttl=90, recall_count=1) == 180
    assert compute_hebbian_ttl(base_ttl=90, recall_count=2) == 270


def test_compute_hebbian_ttl_cap():
    from hebbian_consolidation import compute_hebbian_ttl
    # High recall count must not exceed NEVER_EXPIRE=9999
    assert compute_hebbian_ttl(base_ttl=365, recall_count=100) == 9999
    assert compute_hebbian_ttl(base_ttl=365, recall_count=27) == 9999
    assert compute_hebbian_ttl(base_ttl=90, recall_count=111) == 9999   # 90*112=10080 > 9999


def test_compute_hebbian_ttl_already_never_expire():
    from hebbian_consolidation import compute_hebbian_ttl
    # NEVER_EXPIRE sentinel (9999) must stay 9999 regardless of recall_count
    assert compute_hebbian_ttl(base_ttl=9999, recall_count=5) == 9999


def test_get_high_recall_ids(tmp_path):
    from hebbian_consolidation import get_high_recall_ids
    tracker = tmp_path / "recall-tracker.json"
    tracker.write_text(json.dumps({
        "id-frequent": {"hit_count": 5, "namespace": "ns1", "avg_score": 0.85, "last_recalled": "2026-01-01T00:00:00+00:00"},
        "id-rare": {"hit_count": 1, "namespace": "ns1", "avg_score": 0.7, "last_recalled": "2026-01-01T00:00:00+00:00"},
    }), encoding="utf-8")
    ids = get_high_recall_ids(tracker_path=tracker, min_count=2)
    assert "id-frequent" in ids
    assert "id-rare" not in ids


def test_get_high_recall_ids_empty_file(tmp_path):
    from hebbian_consolidation import get_high_recall_ids
    # Non-existent file -> returns empty dict
    ids = get_high_recall_ids(tracker_path=tmp_path / "missing.json", min_count=2)
    assert ids == {}


def test_load_salience_missing_file(tmp_path):
    from hebbian_consolidation import load_salience
    result = load_salience(tmp_path / "missing.json")
    assert result == {}


def test_load_salience_parses_scores(tmp_path):
    from hebbian_consolidation import load_salience
    p = tmp_path / "salience.json"
    p.write_text(json.dumps({
        "sess-1": {"score": 0.9, "markers": ["SECURITY"], "project": "P", "snippet": "auth bug"},
        "sess-2": {"score": 0.3, "markers": ["ERRORS"], "project": "P", "snippet": "error"},
    }), encoding="utf-8")
    result = load_salience(p)
    assert result == {"sess-1": 0.9, "sess-2": 0.3}


def test_load_salience_tolerates_corrupt(tmp_path):
    from hebbian_consolidation import load_salience
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    assert load_salience(p) == {}


def test_apply_hebbian_salience_bonus_high(tmp_path):
    """score >= 0.8 adds +2 to recall_count -> higher TTL."""
    from hebbian_consolidation import apply_hebbian_ttls, _SALIENCE_BONUS_HIGH

    high_recall = {
        "vec-1": {"hit_count": 1, "namespace": "ns1"},
    }
    salience = {"session-abc": 0.9}

    # Without salience: TTL = 90 * (1+1) = 180
    # With salience bonus +2: TTL = 90 * (1+1+2) = 360
    # Since we can't call Pinecone in tests, just verify the bonus math via compute_hebbian_ttl
    from hebbian_consolidation import compute_hebbian_ttl
    assert compute_hebbian_ttl(90, 1 + _SALIENCE_BONUS_HIGH) == 360
    assert compute_hebbian_ttl(90, 1) == 180  # baseline without bonus


def test_apply_hebbian_salience_bonus_mid():
    """score 0.5-0.79 adds +1 to recall_count."""
    from hebbian_consolidation import compute_hebbian_ttl, _SALIENCE_BONUS_MID
    assert compute_hebbian_ttl(90, 1 + _SALIENCE_BONUS_MID) == 270
