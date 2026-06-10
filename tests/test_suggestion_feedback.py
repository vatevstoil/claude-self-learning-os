"""Tests for suggestion_feedback.py — inhibitory feedback layer."""
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))

NOW = datetime(2026, 5, 25, tzinfo=timezone.utc)


def test_dismiss_sets_future_suppression(tmp_path):
    from suggestion_feedback import dismiss, is_suppressed

    p = tmp_path / "fb.json"
    dismiss("item-1", weeks=4, path=p, now=NOW)

    # Right now: suppressed
    assert is_suppressed("item-1", path=p, now=NOW) is True
    # 5 weeks later: cooldown expired (base window was 4 weeks)
    assert is_suppressed("item-1", path=p, now=NOW + timedelta(weeks=5)) is False


def test_dismiss_doubles_window(tmp_path):
    from suggestion_feedback import dismiss

    p = tmp_path / "fb.json"
    dismiss("i", weeks=4, path=p, now=NOW)            # 1st -> 4 weeks
    entry = dismiss("i", weeks=4, path=p, now=NOW)    # 2nd -> 8 weeks

    su = datetime.fromisoformat(entry["suppress_until"])
    # suppress_until must be at least 8 weeks from NOW (allow 1-day tolerance for rounding)
    assert su >= NOW + timedelta(weeks=8) - timedelta(days=1)
    assert entry["dismiss_count"] == 2


def test_dismiss_triples_on_third(tmp_path):
    """Third dismiss -> 16 weeks (2^2 * 4)."""
    from suggestion_feedback import dismiss

    p = tmp_path / "fb.json"
    dismiss("j", weeks=4, path=p, now=NOW)
    dismiss("j", weeks=4, path=p, now=NOW)
    entry = dismiss("j", weeks=4, path=p, now=NOW)  # 3rd -> 16 weeks

    su = datetime.fromisoformat(entry["suppress_until"])
    assert su >= NOW + timedelta(weeks=16) - timedelta(days=1)
    assert entry["dismiss_count"] == 3


def test_accept_clears_suppression(tmp_path):
    from suggestion_feedback import dismiss, accept, is_suppressed

    p = tmp_path / "fb.json"
    dismiss("x", weeks=4, path=p, now=NOW)
    assert is_suppressed("x", path=p, now=NOW) is True

    accept("x", path=p, now=NOW)
    assert is_suppressed("x", path=p, now=NOW) is False


def test_accept_sets_status_accepted(tmp_path):
    from suggestion_feedback import accept, load_feedback

    p = tmp_path / "fb.json"
    entry = accept("y", path=p, now=NOW)

    assert entry["status"] == "accepted"
    assert entry["suppress_until"] is None
    assert entry["last_at"] == NOW.isoformat()

    loaded = load_feedback(p)
    assert loaded["y"]["status"] == "accepted"


def test_unknown_id_not_suppressed(tmp_path):
    from suggestion_feedback import is_suppressed

    # File doesn't exist yet
    assert is_suppressed("never-seen", path=tmp_path / "fb.json") is False


def test_unknown_id_not_suppressed_empty_file(tmp_path):
    from suggestion_feedback import is_suppressed, save_feedback

    p = tmp_path / "fb.json"
    save_feedback({}, p)
    assert is_suppressed("ghost", path=p, now=NOW) is False


def test_save_load_roundtrip(tmp_path):
    from suggestion_feedback import dismiss, load_feedback

    p = tmp_path / "fb.json"
    dismiss("a", path=p, now=NOW)

    data = load_feedback(p)
    assert "a" in data
    assert data["a"]["status"] == "dismissed"
    assert data["a"]["dismiss_count"] == 1


def test_load_feedback_tolerates_corrupt_file(tmp_path):
    from suggestion_feedback import load_feedback

    p = tmp_path / "bad.json"
    p.write_text("{not valid json{{", encoding="utf-8")
    # Must not raise
    result = load_feedback(p)
    assert result == {}


def test_load_feedback_tolerates_missing_file(tmp_path):
    from suggestion_feedback import load_feedback

    result = load_feedback(tmp_path / "nonexistent.json")
    assert result == {}


def test_is_suppressed_expired_window(tmp_path):
    from suggestion_feedback import dismiss, is_suppressed

    p = tmp_path / "fb.json"
    dismiss("z", weeks=4, path=p, now=NOW)

    # Exactly at suppress_until: not suppressed (boundary: now == su means NOT now < su)
    future = NOW + timedelta(weeks=4)
    assert is_suppressed("z", path=p, now=future) is False


def test_multiple_items_independent(tmp_path):
    from suggestion_feedback import dismiss, is_suppressed

    p = tmp_path / "fb.json"
    dismiss("alpha", weeks=4, path=p, now=NOW)
    dismiss("beta", weeks=4, path=p, now=NOW)

    assert is_suppressed("alpha", path=p, now=NOW) is True
    assert is_suppressed("beta", path=p, now=NOW) is True
    # alpha expires but beta still suppressed (same window, so both expire together)
    assert is_suppressed("alpha", path=p, now=NOW + timedelta(weeks=5)) is False
    assert is_suppressed("beta", path=p, now=NOW + timedelta(weeks=5)) is False


def test_dismiss_preserves_history_on_accept(tmp_path):
    """dismiss_count should survive an accept (history preservation)."""
    from suggestion_feedback import dismiss, accept, load_feedback

    p = tmp_path / "fb.json"
    dismiss("h", weeks=4, path=p, now=NOW)
    dismiss("h", weeks=4, path=p, now=NOW)
    accept("h", path=p, now=NOW)

    data = load_feedback(p)
    assert data["h"].get("dismiss_count", 0) == 2
    assert data["h"]["status"] == "accepted"


# ---------------------------------------------------------------------------
# record_surfaced tests
# ---------------------------------------------------------------------------

def test_record_surfaced_increments_count(tmp_path):
    from suggestion_feedback import record_surfaced, load_feedback

    p = tmp_path / "fb.json"
    record_surfaced("s1", path=p, threshold=5, now=NOW)
    record_surfaced("s1", path=p, threshold=5, now=NOW)

    data = load_feedback(p)
    assert data["s1"]["surfaced_count"] == 2
    assert data["s1"]["status"] == "surfaced"


def test_record_surfaced_auto_dismisses_at_threshold(tmp_path):
    from suggestion_feedback import record_surfaced, is_suppressed

    p = tmp_path / "fb.json"
    for _ in range(5):
        record_surfaced("s2", path=p, threshold=5, now=NOW)

    assert is_suppressed("s2", path=p, now=NOW) is True


def test_record_surfaced_marks_implicit(tmp_path):
    from suggestion_feedback import record_surfaced, load_feedback

    p = tmp_path / "fb.json"
    for _ in range(5):
        record_surfaced("s3", path=p, threshold=5, now=NOW)

    data = load_feedback(p)
    assert data["s3"].get("implicit") is True
    assert data["s3"]["status"] == "dismissed"


def test_record_surfaced_does_not_override_explicit_dismiss(tmp_path):
    from suggestion_feedback import dismiss, record_surfaced, load_feedback

    p = tmp_path / "fb.json"
    dismiss("s4", weeks=4, path=p, now=NOW)  # explicit dismiss
    # Surfacing should not change status or suppress_until
    original = load_feedback(p)["s4"]["suppress_until"]
    record_surfaced("s4", path=p, threshold=1, now=NOW)  # threshold=1 would auto-dismiss

    data = load_feedback(p)
    assert data["s4"]["status"] == "dismissed"
    assert data["s4"]["suppress_until"] == original  # unchanged


def test_record_surfaced_below_threshold_not_suppressed(tmp_path):
    from suggestion_feedback import record_surfaced, is_suppressed

    p = tmp_path / "fb.json"
    for _ in range(4):  # 4 < threshold of 5
        record_surfaced("s5", path=p, threshold=5, now=NOW)

    assert is_suppressed("s5", path=p, now=NOW) is False


def test_accept_queues_habit(tmp_path):
    from suggestion_feedback import accept, ACCEPTED_HABITS_PATH
    import json
    fb = tmp_path / "fb.json"
    ah = tmp_path / "accepted.json"
    accept("habit-Proj-bash:git", item_type="habit", path=fb, accepted_habits_path=ah)
    queue = json.loads(ah.read_text())
    assert "habit-Proj-bash:git" in queue

def test_accept_no_queue_for_non_habit(tmp_path):
    from suggestion_feedback import accept
    import json
    fb = tmp_path / "fb.json"
    ah = tmp_path / "accepted.json"
    accept("boris-rule-X", item_type="boris_rule", path=fb, accepted_habits_path=ah)
    assert not ah.exists()

def test_accept_deduplicates_queue(tmp_path):
    from suggestion_feedback import accept
    import json
    fb = tmp_path / "fb.json"
    ah = tmp_path / "accepted.json"
    for _ in range(3):
        accept("habit-P-bash:git", item_type="habit", path=fb, accepted_habits_path=ah)
    queue = json.loads(ah.read_text())
    assert queue.count("habit-P-bash:git") == 1


def test_accept_queues_boris(tmp_path):
    from suggestion_feedback import accept
    import json
    fb = tmp_path / "fb.json"
    ab = tmp_path / "accepted-boris.json"
    accept("boris_rule-J--Proj-X", item_type="boris_rule", path=fb, accepted_boris_path=ab)
    queue = json.loads(ab.read_text())
    assert "boris_rule-J--Proj-X" in queue


def test_accept_no_boris_queue_for_habit(tmp_path):
    from suggestion_feedback import accept
    fb = tmp_path / "fb.json"
    ab = tmp_path / "accepted-boris.json"
    accept("habit-Proj-bash:git", item_type="habit", path=fb, accepted_boris_path=ab)
    assert not ab.exists()
