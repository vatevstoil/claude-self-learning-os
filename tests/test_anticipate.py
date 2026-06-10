"""Tests for anticipate.py — TDD first pass."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def _h(project: str, routine: list, dist: float) -> dict:
    return {
        "project": project,
        "routine": list(routine),
        "count": 10,
        "distinctiveness": dist,
        "reward_ratio": 0.9,
        "session_count": 4,
    }


def test_build_index_groups_by_first():
    from anticipate import build_index

    idx = build_index([_h("P", ["Bash:grep", "Read"], 300), _h("P", ["Edit", "Bash:python"], 200)])
    assert ("P", "Bash:grep") in idx["by_first"]
    assert "P" in idx["by_project"]


def test_predict_next_without_recent_returns_top():
    from anticipate import build_index, predict_next

    idx = build_index([_h("P", ["Bash:grep", "Read"], 300), _h("P", ["Edit", "Bash:python"], 100)])
    preds = predict_next("P", None, idx, top=2)
    assert len(preds) >= 1
    # highest distinctiveness first
    assert preds[0]["routine"] == ["Bash:grep", "Read"]
    assert 0.0 <= preds[0]["confidence"] <= 1.0


def test_predict_next_with_recent_prefix():
    from anticipate import build_index, predict_next

    idx = build_index([_h("P", ["Bash:grep", "Read", "Edit"], 300)])
    preds = predict_next("P", ["Bash:grep"], idx, top=3)
    # should predict Read follows Bash:grep
    assert any(p.get("next") == "Read" for p in preds)


def test_predict_unknown_project_empty():
    from anticipate import build_index, predict_next

    idx = build_index([_h("P", ["Bash:grep", "Read"], 300)])
    assert predict_next("Other", None, idx) == []


def test_write_anticipations(tmp_path):
    from anticipate import write_anticipations

    habits = tmp_path / "habits.json"
    habits.write_text(json.dumps([_h("P", ["Bash:grep", "Read"], 300)]), encoding="utf-8")
    out = tmp_path / "anticipations.json"
    result = write_anticipations(habits, out)
    assert "P" in result
    assert out.exists()
