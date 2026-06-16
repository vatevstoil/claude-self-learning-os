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


# ---------------------------------------------------------------------------
# review --list / --accept / --reject tests
# ---------------------------------------------------------------------------

def _make_queue(tmp_path: "Path", items: list) -> "Path":
    """Write a fake improvement-queue.json and return its path."""
    p = tmp_path / "improvement-queue.json"
    p.write_text(json.dumps(items), encoding="utf-8")
    return p


def _sample_queue_items():
    """Return 4 fake queue items covering boris_rule and habit types."""
    return [
        {
            "id": "boris-proj-alpha",
            "type": "boris_rule",
            "description": "6 corrections in proj-alpha — e.g. 'something went wrong here'",
            "project": "proj-alpha",
            "confidence": 0.95,
            "value": 0.9,
            "score": 0.855,
            "status": "queued",
            "source": "fake",
            "judge_score": 0.9,
            "judge_reason": "useful correction",
            "judge_verdict": "useful",
        },
        {
            "id": "habit-proj-beta-read-edit",
            "type": "habit",
            "description": "Recurring workflow in proj-beta: Read -> Edit (dist=300, 10x, reward=0.80)",
            "project": "proj-beta",
            "confidence": 0.75,
            "value": 0.7,
            "score": 0.525,
            "status": "queued",
            "source": "fake",
            "judge_score": None,
            "judge_reason": "",
            "judge_verdict": "",
        },
        {
            "id": "boris-proj-gamma",
            "type": "boris_rule",
            "description": "3 corrections in proj-gamma",
            "project": "proj-gamma",
            "confidence": 0.74,
            "value": 0.9,
            "score": 0.666,
            "status": "queued",
            "source": "fake",
            "judge_score": 0.7,
            "judge_reason": "mildly useful",
            "judge_verdict": "useful",
        },
        {
            "id": "boris-proj-auto",
            "type": "boris_rule",
            "description": "auto-apply item",
            "project": "proj-auto",
            "confidence": 0.95,
            "value": 0.9,
            "score": 0.855,
            "status": "auto_apply",   # should be skipped in --list
            "source": "fake",
            "judge_score": None,
            "judge_reason": "",
            "judge_verdict": "",
        },
    ]


def test_review_list_excludes_auto_apply(tmp_path, capsys):
    from suggestion_feedback import review_list, _short_id

    q = _make_queue(tmp_path, _sample_queue_items())
    fb = tmp_path / "fb.json"
    vd = tmp_path / "verdicts.json"

    review_list(queue_path=q, feedback_path=fb, verdicts_path=vd)
    out = capsys.readouterr().out

    # auto_apply item must not appear
    assert "auto_apply" not in out
    assert "proj-auto" not in out
    # Manual items should appear (top 3 by score, auto-apply excluded)
    assert "boris-proj-alpha" in out or _short_id("boris-proj-alpha") in out


def test_review_list_shows_copy_paste_commands(tmp_path, capsys):
    from suggestion_feedback import review_list

    q = _make_queue(tmp_path, _sample_queue_items())
    fb = tmp_path / "fb.json"
    vd = tmp_path / "verdicts.json"

    review_list(queue_path=q, feedback_path=fb, verdicts_path=vd)
    out = capsys.readouterr().out

    assert "review --accept" in out
    assert "review --reject" in out


def test_review_list_no_pending_when_all_reviewed(tmp_path, capsys):
    from suggestion_feedback import review_list, _short_id
    import json

    items = _sample_queue_items()
    # Mark all non-auto_apply items as already-verdicted
    verdicts = {}
    for it in items:
        if it["status"] != "auto_apply":
            verdicts[it["id"]] = {"verdict": "accepted", "short_id": _short_id(it["id"])}

    q = _make_queue(tmp_path, items)
    fb = tmp_path / "fb.json"
    vd = tmp_path / "verdicts.json"
    vd.write_text(json.dumps(verdicts), encoding="utf-8")

    review_list(queue_path=q, feedback_path=fb, verdicts_path=vd)
    out = capsys.readouterr().out
    assert "No pending" in out


def test_review_accept_updates_feedback_and_verdict(tmp_path):
    from suggestion_feedback import review_accept, _short_id, load_feedback, _load_verdicts

    items = _sample_queue_items()
    q = _make_queue(tmp_path, items)
    fb = tmp_path / "fb.json"
    vd = tmp_path / "verdicts.json"
    ab = tmp_path / "accepted-boris.json"
    ah = tmp_path / "accepted-habits.json"

    target_id = "boris-proj-alpha"
    sid = _short_id(target_id)

    review_accept(
        sid,
        queue_path=q,
        feedback_path=fb,
        verdicts_path=vd,
        accepted_habits_path=ah,
        accepted_boris_path=ab,
        now=NOW,
    )

    # (a) verdict recorded
    v = _load_verdicts(vd)
    assert v[target_id]["verdict"] == "accepted"

    # (b) accepted-boris.json updated
    boris_list = json.loads(ab.read_text(encoding="utf-8"))
    assert target_id in boris_list

    # (c) suggestion-feedback updated
    fb_data = load_feedback(fb)
    assert fb_data[target_id]["status"] == "accepted"

    # (d) queue status patched
    q_data = json.loads(q.read_text(encoding="utf-8"))
    item = next(it for it in q_data if it["id"] == target_id)
    assert item["status"] == "accepted"
    # judge fields must survive (carry_judge invariant)
    assert item["judge_score"] == 0.9
    assert item["judge_verdict"] == "useful"

    # (e) NO sentinel files created in boris-drafts (verdict store is the channel)
    bd = tmp_path / "boris-drafts"
    assert not bd.exists() or list(bd.iterdir()) == []


def test_review_accept_feeds_effectiveness_is_resolved_exact(tmp_path):
    """After accept, is_resolved() returns True for the EXACT item id (via verdicts).

    Core H1 assertion: precision feeds through the exact verdict store,
    not through substring-matching file sentinels.
    """
    from suggestion_feedback import review_accept, _short_id
    from effectiveness_tracker import is_resolved

    items = _sample_queue_items()
    q = _make_queue(tmp_path, items)
    vd = tmp_path / "verdicts.json"

    target_id = "boris-proj-alpha"
    item_dict = next(it for it in items if it["id"] == target_id)

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    # Before accept: is_resolved must be False (no verdicts, no files)
    assert is_resolved(item_dict, {}, empty_dir, empty_dir,
                       verdicts_path=vd) is False

    sid = _short_id(target_id)
    review_accept(
        sid,
        queue_path=q,
        feedback_path=tmp_path / "fb.json",
        verdicts_path=vd,
        accepted_habits_path=tmp_path / "ah.json",
        accepted_boris_path=tmp_path / "ab.json",
        now=NOW,
    )

    # After accept: is_resolved True for the exact id
    assert is_resolved(item_dict, {}, empty_dir, empty_dir,
                       verdicts_path=vd) is True


def test_review_accept_no_prefix_collision(tmp_path):
    """Accepting boris-proj must NOT resolve boris-proj-v2 (no substring overcounting).

    This is the H1 regression test: the old sentinel approach would have
    caused boris-proj.md stem 'boris-proj' to match 'boris-proj-v2' as a
    substring.  The verdict store uses exact keys, so collision is impossible.
    """
    from suggestion_feedback import review_accept, _short_id
    from effectiveness_tracker import is_resolved

    # Two items sharing a prefix: accept only the first
    base_item = {
        "id": "boris-proj",
        "type": "boris_rule",
        "description": "base item",
        "project": "proj",
        "confidence": 0.9, "value": 0.9, "score": 0.81,
        "status": "queued", "source": "fake",
        "judge_score": None, "judge_reason": "", "judge_verdict": "",
    }
    v2_item = {
        "id": "boris-proj-v2",
        "type": "boris_rule",
        "description": "v2 item",
        "project": "proj-v2",
        "confidence": 0.9, "value": 0.9, "score": 0.81,
        "status": "queued", "source": "fake",
        "judge_score": None, "judge_reason": "", "judge_verdict": "",
    }

    q = _make_queue(tmp_path, [base_item, v2_item])
    vd = tmp_path / "verdicts.json"
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    # Accept only the base item
    review_accept(
        _short_id("boris-proj"),
        queue_path=q,
        feedback_path=tmp_path / "fb.json",
        verdicts_path=vd,
        accepted_habits_path=tmp_path / "ah.json",
        accepted_boris_path=tmp_path / "ab.json",
        now=NOW,
    )

    # base item: resolved
    assert is_resolved(base_item, {}, empty_dir, empty_dir,
                       verdicts_path=vd) is True
    # v2 item: NOT resolved — exact match prevents prefix collision
    assert is_resolved(v2_item, {}, empty_dir, empty_dir,
                       verdicts_path=vd) is False


def test_review_reject_is_not_resolved(tmp_path):
    """Rejected item must NOT be counted as resolved in precision_by_type."""
    from suggestion_feedback import review_reject, _short_id
    from effectiveness_tracker import is_resolved

    items = _sample_queue_items()
    q = _make_queue(tmp_path, items)
    vd = tmp_path / "verdicts.json"
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    target_id = "boris-proj-alpha"
    item_dict = next(it for it in items if it["id"] == target_id)

    review_reject(
        _short_id(target_id),
        queue_path=q,
        feedback_path=tmp_path / "fb.json",
        verdicts_path=vd,
        now=NOW,
    )

    # Rejected verdict must NOT count as resolved
    assert is_resolved(item_dict, {}, empty_dir, empty_dir,
                       verdicts_path=vd) is False


def test_review_accept_precision_by_type_moves_for_accepted_only(tmp_path):
    """End-to-end: precision_by_type rises for accepted item, not for rejected."""
    from suggestion_feedback import review_accept, review_reject, _short_id
    from effectiveness_tracker import (
        is_resolved, precision_by_type, record_snapshot, load_history,
    )

    items = _sample_queue_items()
    q = _make_queue(tmp_path, items)
    vd = tmp_path / "verdicts.json"
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    hist = tmp_path / "history.jsonl"

    # Record snapshot with two boris items
    boris_items = [it for it in items if it["type"] == "boris_rule"
                   and it["status"] != "auto_apply"]
    record_snapshot(boris_items, hist)
    history = load_history(hist)

    # Accept first, reject second
    accepted_id = boris_items[0]["id"]
    rejected_id = boris_items[1]["id"]

    review_accept(
        _short_id(accepted_id),
        queue_path=q,
        feedback_path=tmp_path / "fb.json",
        verdicts_path=vd,
        accepted_habits_path=tmp_path / "ah.json",
        accepted_boris_path=tmp_path / "ab.json",
        now=NOW,
    )
    review_reject(
        _short_id(rejected_id),
        queue_path=q,
        feedback_path=tmp_path / "fb.json",
        verdicts_path=vd,
        now=NOW,
    )

    def resolved_fn(item):
        return is_resolved(item, {}, empty_dir, empty_dir, verdicts_path=vd)

    prec = precision_by_type(history, resolved_fn)
    # 1 accepted of 2 total boris items = 0.5 precision
    assert abs(prec.get("boris_rule", -1) - 0.5) < 1e-9


def test_review_reject_suppresses_item(tmp_path):
    from suggestion_feedback import review_reject, _short_id, is_suppressed, _load_verdicts

    items = _sample_queue_items()
    q = _make_queue(tmp_path, items)
    fb = tmp_path / "fb.json"
    vd = tmp_path / "verdicts.json"

    target_id = "boris-proj-alpha"
    sid = _short_id(target_id)

    review_reject(
        sid,
        queue_path=q,
        feedback_path=fb,
        verdicts_path=vd,
        now=NOW,
    )

    # Verdict recorded
    v = _load_verdicts(vd)
    assert v[target_id]["verdict"] == "rejected"

    # Item is suppressed (negative precision sample path)
    assert is_suppressed(target_id, path=fb, now=NOW) is True

    # Queue status patched
    q_data = json.loads(q.read_text(encoding="utf-8"))
    item = next(it for it in q_data if it["id"] == target_id)
    assert item["status"] == "dismissed"
    # judge fields must survive
    assert item["judge_score"] == 0.9
    assert item["judge_verdict"] == "useful"


def test_review_accept_idempotent(tmp_path, capsys):
    """Re-accepting the same short-id is a no-op with a friendly message."""
    from suggestion_feedback import review_accept, _short_id

    items = _sample_queue_items()
    q = _make_queue(tmp_path, items)
    fb = tmp_path / "fb.json"
    vd = tmp_path / "verdicts.json"
    ab = tmp_path / "accepted-boris.json"
    ah = tmp_path / "accepted-habits.json"

    target_id = "boris-proj-alpha"
    sid = _short_id(target_id)

    review_accept(sid, queue_path=q, feedback_path=fb, verdicts_path=vd,
                  accepted_habits_path=ah, accepted_boris_path=ab, now=NOW)
    review_accept(sid, queue_path=q, feedback_path=fb, verdicts_path=vd,
                  accepted_habits_path=ah, accepted_boris_path=ab, now=NOW)

    out = capsys.readouterr().out
    assert "Already accepted" in out or "no-op" in out

    # accepted-boris.json must not have duplicate entries
    boris_list = json.loads(ab.read_text(encoding="utf-8"))
    assert boris_list.count(target_id) == 1


def test_review_reject_idempotent(tmp_path, capsys):
    """Re-rejecting the same short-id is a no-op with a friendly message."""
    from suggestion_feedback import review_reject, _short_id

    items = _sample_queue_items()
    q = _make_queue(tmp_path, items)
    fb = tmp_path / "fb.json"
    vd = tmp_path / "verdicts.json"

    target_id = "boris-proj-alpha"
    sid = _short_id(target_id)

    review_reject(sid, queue_path=q, feedback_path=fb, verdicts_path=vd, now=NOW)
    review_reject(sid, queue_path=q, feedback_path=fb, verdicts_path=vd, now=NOW)

    out = capsys.readouterr().out
    assert "Already rejected" in out or "no-op" in out


def test_review_unknown_id_accept_exits_clean(tmp_path, capsys):
    """Unknown short-id on accept -> friendly message, exit 0 (no exception)."""
    from suggestion_feedback import review_accept

    q = _make_queue(tmp_path, _sample_queue_items())
    fb = tmp_path / "fb.json"
    vd = tmp_path / "verdicts.json"
    ab = tmp_path / "accepted-boris.json"
    ah = tmp_path / "accepted-habits.json"

    review_accept(
        "deadbeef",  # does not match anything
        queue_path=q, feedback_path=fb, verdicts_path=vd,
        accepted_habits_path=ah, accepted_boris_path=ab, now=NOW,
    )
    out = capsys.readouterr().out
    assert "Unknown" in out or "nothing" in out.lower()


def test_review_unknown_id_reject_exits_clean(tmp_path, capsys):
    """Unknown short-id on reject -> friendly message, exit 0 (no exception)."""
    from suggestion_feedback import review_reject

    q = _make_queue(tmp_path, _sample_queue_items())
    fb = tmp_path / "fb.json"
    vd = tmp_path / "verdicts.json"

    review_reject(
        "deadbeef",
        queue_path=q, feedback_path=fb, verdicts_path=vd, now=NOW,
    )
    out = capsys.readouterr().out
    assert "Unknown" in out or "nothing" in out.lower()
