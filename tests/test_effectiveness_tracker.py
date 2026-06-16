import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def test_record_and_load_history(tmp_path):
    from effectiveness_tracker import record_snapshot, load_history
    h = tmp_path / "history.jsonl"
    record_snapshot([{"id": "a", "type": "habit", "project": "P", "score": 0.5}], h)
    record_snapshot([{"id": "b", "type": "boris_rule", "project": "P", "score": 0.8}], h)
    hist = load_history(h)
    assert len(hist) == 2
    assert hist[0]["items"][0]["id"] == "a"


def test_load_history_skips_corrupt(tmp_path):
    from effectiveness_tracker import load_history
    h = tmp_path / "history.jsonl"
    h.write_text('{"ts":"t","items":[]}\nNOT JSON\n{"ts":"t2","items":[]}\n', encoding="utf-8")
    assert len(load_history(h)) == 2


def test_precision_by_type():
    from effectiveness_tracker import precision_by_type
    history = [
        {"items": [{"id": "h1", "type": "habit"}, {"id": "h2", "type": "habit"}]},
        {"items": [{"id": "h1", "type": "habit"}, {"id": "b1", "type": "boris_rule"}]},
    ]
    # h1 resolved, h2 not, b1 resolved
    resolved = lambda it: it["id"] in ("h1", "b1")
    prec = precision_by_type(history, resolved)
    assert abs(prec["habit"] - 0.5) < 1e-9   # h1 resolved of {h1,h2}
    assert abs(prec["boris_rule"] - 1.0) < 1e-9


def test_suggest_thresholds_raises_on_low_precision():
    from effectiveness_tracker import suggest_thresholds
    t = suggest_thresholds({"habit": 0.1})
    assert t["habit_distinctiveness_min"] > 150.0


def test_suggest_thresholds_high_precision_floored():
    # High precision no longer lowers the bar below the human-tuned base of 150:
    # the floor blocks the self-reinforcing downward drift that false-positive
    # precision used to trigger (audit: threshold-auto-tune-corrupted).
    from effectiveness_tracker import suggest_thresholds, _BASE_DISTINCTIVENESS
    t = suggest_thresholds({"habit": 0.9})
    assert t["habit_distinctiveness_min"] == _BASE_DISTINCTIVENESS


def test_suggest_thresholds_clamps():
    from effectiveness_tracker import suggest_thresholds
    # never below the protective floor (150) nor above 500
    t = suggest_thresholds({"habit": 0.0})
    assert 150.0 <= t["habit_distinctiveness_min"] <= 500.0


def test_is_resolved_habit_skill_draft(tmp_path):
    from effectiveness_tracker import is_resolved
    drafts = tmp_path / "skill-drafts"
    (drafts / "pytest-edit").mkdir(parents=True)
    item = {"id": "habit-proj-pytest-edit", "type": "habit", "project": "proj"}
    assert is_resolved(item, {}, drafts, tmp_path / "boris") is True


def test_is_resolved_graphify_always_false(tmp_path):
    from effectiveness_tracker import is_resolved
    item = {"id": "graphify-x", "type": "graphify", "project": "x"}
    assert is_resolved(item, {}, tmp_path / "s", tmp_path / "b") is False


def test_anticipation_accuracy_perfect_match(tmp_path):
    from effectiveness_tracker import check_anticipation_accuracy
    ant = tmp_path / "anticipations.json"
    hab = tmp_path / "habits.json"
    ant.write_text(json.dumps({
        "Proj": [{"routine": ["Bash:git", "Edit"], "score": 0.9}]
    }), encoding="utf-8")
    hab.write_text(json.dumps([{
        "project": "Proj", "routine": ["Bash:git", "Edit"],
        "distinctiveness": 200.0, "count": 10, "status": "detected"
    }]), encoding="utf-8")
    acc = check_anticipation_accuracy(ant, hab)
    assert acc == 1.0

def test_anticipation_accuracy_no_match(tmp_path):
    from effectiveness_tracker import check_anticipation_accuracy
    ant = tmp_path / "anticipations.json"
    hab = tmp_path / "habits.json"
    ant.write_text(json.dumps({
        "Proj": [{"routine": ["Bash:git", "Edit"], "score": 0.9}]
    }), encoding="utf-8")
    hab.write_text(json.dumps([{
        "project": "Proj", "routine": ["Read", "Glob"],
        "distinctiveness": 200.0, "count": 10, "status": "detected"
    }]), encoding="utf-8")
    acc = check_anticipation_accuracy(ant, hab)
    assert acc == 0.0

def test_anticipation_accuracy_empty_inputs(tmp_path):
    from effectiveness_tracker import check_anticipation_accuracy
    acc = check_anticipation_accuracy(tmp_path / "nope.json", tmp_path / "nope2.json")
    assert acc == 0.0


# ---------------------------------------------------------------------------
# is_resolved — verdicts_path exact lookup (H1 fix)
# ---------------------------------------------------------------------------

def test_is_resolved_accepted_verdict_returns_true(tmp_path):
    """Exact verdict 'accepted' in review-verdicts.json -> is_resolved True."""
    from effectiveness_tracker import is_resolved

    vd = tmp_path / "review-verdicts.json"
    vd.write_text(
        json.dumps({"boris-proj-alpha": {"verdict": "accepted", "type": "boris_rule"}}),
        encoding="utf-8",
    )
    item = {"id": "boris-proj-alpha", "type": "boris_rule", "project": "proj-alpha"}
    empty = tmp_path / "empty"
    empty.mkdir()
    assert is_resolved(item, {}, empty, empty, verdicts_path=vd) is True


def test_is_resolved_rejected_verdict_returns_false(tmp_path):
    """Exact verdict 'rejected' -> is_resolved False (not resolved, negative sample)."""
    from effectiveness_tracker import is_resolved

    vd = tmp_path / "review-verdicts.json"
    vd.write_text(
        json.dumps({"boris-proj-alpha": {"verdict": "rejected", "type": "boris_rule"}}),
        encoding="utf-8",
    )
    item = {"id": "boris-proj-alpha", "type": "boris_rule", "project": "proj-alpha"}
    empty = tmp_path / "empty"
    empty.mkdir()
    assert is_resolved(item, {}, empty, empty, verdicts_path=vd) is False


def test_is_resolved_no_prefix_collision(tmp_path):
    """Accepting 'boris-proj' must NOT resolve 'boris-proj-v2' (exact key match)."""
    from effectiveness_tracker import is_resolved

    vd = tmp_path / "review-verdicts.json"
    vd.write_text(
        json.dumps({"boris-proj": {"verdict": "accepted", "type": "boris_rule"}}),
        encoding="utf-8",
    )
    item_base = {"id": "boris-proj", "type": "boris_rule", "project": "proj"}
    item_v2 = {"id": "boris-proj-v2", "type": "boris_rule", "project": "proj-v2"}
    empty = tmp_path / "empty"
    empty.mkdir()

    assert is_resolved(item_base, {}, empty, empty, verdicts_path=vd) is True
    assert is_resolved(item_v2, {}, empty, empty, verdicts_path=vd) is False


def test_is_resolved_missing_verdicts_file_falls_through_to_heuristic(tmp_path):
    """When review-verdicts.json is absent, legacy file heuristic still works."""
    from effectiveness_tracker import is_resolved

    drafts = tmp_path / "skill-drafts"
    (drafts / "pytest-edit").mkdir(parents=True)
    item = {"id": "habit-proj-pytest-edit", "type": "habit", "project": "proj"}
    missing_vd = tmp_path / "nonexistent-verdicts.json"

    # No verdict file, but skill-draft dir present -> True via legacy heuristic
    assert is_resolved(item, {}, drafts, tmp_path / "boris",
                       verdicts_path=missing_vd) is True


def test_is_resolved_accepted_verdict_takes_priority_over_empty_drafts(tmp_path):
    """Verdict store is checked first; legacy heuristic is not needed when verdict present."""
    from effectiveness_tracker import is_resolved

    vd = tmp_path / "review-verdicts.json"
    vd.write_text(
        json.dumps({"boris-special": {"verdict": "accepted"}}),
        encoding="utf-8",
    )
    item = {"id": "boris-special", "type": "boris_rule", "project": "special"}
    # Empty dirs — legacy heuristic would return False
    empty = tmp_path / "empty"
    empty.mkdir()
    assert is_resolved(item, {}, empty, empty, verdicts_path=vd) is True


def test_is_resolved_corrupt_verdicts_file_falls_through(tmp_path):
    """Corrupt review-verdicts.json is tolerated; falls through to legacy heuristic."""
    from effectiveness_tracker import is_resolved

    vd = tmp_path / "review-verdicts.json"
    vd.write_text("{not valid json{{", encoding="utf-8")

    drafts = tmp_path / "skill-drafts"
    (drafts / "pytest-edit").mkdir(parents=True)
    item = {"id": "habit-proj-pytest-edit", "type": "habit", "project": "proj"}

    # Corrupt verdicts -> fallback to heuristic -> True (dir exists)
    assert is_resolved(item, {}, drafts, tmp_path / "boris", verdicts_path=vd) is True
