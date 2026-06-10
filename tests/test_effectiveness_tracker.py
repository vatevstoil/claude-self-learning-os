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


def test_suggest_thresholds_lowers_on_high_precision():
    from effectiveness_tracker import suggest_thresholds
    t = suggest_thresholds({"habit": 0.9})
    assert t["habit_distinctiveness_min"] < 150.0


def test_suggest_thresholds_clamps():
    from effectiveness_tracker import suggest_thresholds
    # extreme repeated low precision shouldn't exceed 500
    t = suggest_thresholds({"habit": 0.0})
    assert 50.0 <= t["habit_distinctiveness_min"] <= 500.0


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
