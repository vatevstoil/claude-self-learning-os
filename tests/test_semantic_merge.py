"""Tests for semantic_merge.py pure functions (no network required)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def test_cosine_identical():
    from semantic_merge import cosine

    assert abs(cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9


def test_cosine_orthogonal():
    from semantic_merge import cosine

    assert abs(cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9


def test_cosine_zero_vector():
    from semantic_merge import cosine

    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_find_duplicates_threshold():
    from semantic_merge import find_duplicates

    vecs = [
        {"id": "a", "values": [1.0, 0.0, 0.0]},
        {"id": "b", "values": [0.99, 0.01, 0.0]},
        {"id": "c", "values": [0.0, 1.0, 0.0]},
    ]
    pairs = find_duplicates(vecs, threshold=0.95)
    ids = {frozenset(p) for p in pairs}
    assert frozenset(("a", "b")) in ids
    assert not any("c" in p for p in pairs)


def test_pick_survivor_by_recall():
    from semantic_merge import pick_survivor

    recall = {"a": {"hit_count": 5}, "b": {"hit_count": 1}}
    survivor = pick_survivor("a", "b", recall, {"date": "2026-01-01"}, {"date": "2026-05-01"})
    assert survivor == "a"


def test_pick_survivor_tiebreak_date():
    from semantic_merge import pick_survivor

    recall = {}  # no recall data -> tiebreak by date
    survivor = pick_survivor("a", "b", recall, {"date": "2026-01-01"}, {"date": "2026-05-01"})
    assert survivor == "b"  # newer date wins
