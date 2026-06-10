"""Tests for promote_to_pinecone.py — ascii_slug + pre-promote dedup."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def test_ascii_slug_passthrough_for_clean_namespace():
    from promote_to_pinecone import ascii_slug
    assert ascii_slug("Cinemind") == "Cinemind"
    assert ascii_slug("_claude_meta") == "_claude_meta"
    assert ascii_slug("Facturka.bg") == "Facturka.bg"


def test_ascii_slug_transliterates_cyrillic():
    from promote_to_pinecone import ascii_slug
    # Must be ASCII and non-empty
    out = ascii_slug("Петър Дънов")
    assert out.isascii()
    assert out  # not empty
    assert " " not in out  # spaces slugified


def test_check_duplicates_returns_matches_above_threshold(monkeypatch):
    import promote_to_pinecone as ptp
    import pinecone

    def fake_query(ns, text, topk=3):
        return [
            {"id": "a", "score": 0.95, "metadata": {"text": "near dup"}},
            {"id": "b", "score": 0.50, "metadata": {"text": "unrelated"}},
        ]
    monkeypatch.setattr(pinecone, "query_and_track", fake_query)
    dupes = ptp._check_duplicates("ns", "candidate", threshold=0.88)
    assert len(dupes) == 1
    assert dupes[0]["id"] == "a"


def test_check_duplicates_empty_when_all_below_threshold(monkeypatch):
    import promote_to_pinecone as ptp
    import pinecone

    monkeypatch.setattr(pinecone, "query_and_track",
                        lambda ns, text, topk=3: [{"id": "x", "score": 0.40, "metadata": {}}])
    assert ptp._check_duplicates("ns", "candidate", threshold=0.88) == []


def test_check_duplicates_safe_on_pinecone_failure(monkeypatch):
    import promote_to_pinecone as ptp
    import pinecone

    def boom(*a, **k):
        raise RuntimeError("pinecone down")
    monkeypatch.setattr(pinecone, "query_and_track", boom)
    # Must not raise — returns [] so promotion can proceed offline.
    assert ptp._check_duplicates("ns", "candidate") == []
