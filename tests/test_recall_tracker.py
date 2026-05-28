import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def test_log_hit_creates_file(tmp_path):
    from recall_tracker import log_hit
    p = tmp_path / "recall-tracker.json"
    log_hit("ns1", "id-abc", 0.85, tracker_path=p)
    data = json.loads(p.read_text())
    assert "id-abc" in data
    assert data["id-abc"]["hit_count"] == 1
    assert data["id-abc"]["namespace"] == "ns1"
    assert data["id-abc"]["avg_score"] == 0.85


def test_log_hit_increments_and_averages(tmp_path):
    from recall_tracker import log_hit
    p = tmp_path / "recall-tracker.json"
    log_hit("ns1", "id-abc", 0.80, tracker_path=p)
    log_hit("ns1", "id-abc", 0.90, tracker_path=p)
    data = json.loads(p.read_text())
    assert data["id-abc"]["hit_count"] == 2
    assert abs(data["id-abc"]["avg_score"] - 0.85) < 0.001


def test_log_hits_batch(tmp_path):
    from recall_tracker import log_hits
    p = tmp_path / "recall-tracker.json"
    matches = [{"id": "vec-a", "score": 0.9}, {"id": "vec-b", "score": 0.7}]
    log_hits("ns-test", matches, tracker_path=p)
    data = json.loads(p.read_text())
    assert "vec-a" in data and "vec-b" in data
    assert data["vec-a"]["hit_count"] == 1
    assert data["vec-b"]["namespace"] == "ns-test"


def test_log_hit_tolerates_missing_id(tmp_path):
    from recall_tracker import log_hits
    p = tmp_path / "recall-tracker.json"
    # id missing -> should silently skip
    log_hits("ns1", [{"score": 0.9}], tracker_path=p)
    data = json.loads(p.read_text()) if p.exists() else {}
    assert data == {}
