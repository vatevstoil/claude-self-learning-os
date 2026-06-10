"""Tests for obsidian_premium_viz.py pure helpers (palette + query building)."""
import importlib
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
viz = importlib.import_module("obsidian_premium_viz")


# ── assign_colors ──────────────────────────────────────────────────────────────

def test_semantic_colors_are_stable():
    c = viz.assign_colors(["concepts", "summaries", "_system", "sources"])
    assert c["concepts"] == 0x9B7DE0
    assert c["summaries"] == 0x2FB6A8
    assert c["_system"] == 0xE8765A
    assert c["sources"] == 0x4F9BE0


def test_semantic_synonyms_share_color():
    c = viz.assign_colors(["summaries", "knowledge", "abstracts"])
    assert c["summaries"] == c["knowledge"] == c["abstracts"] == 0x2FB6A8


def test_unknown_folders_get_distinct_colors():
    c = viz.assign_colors(["alpha", "beta", "gamma", "delta"])
    assert len(set(c.values())) == 4  # all distinct


def test_fallback_does_not_collide_with_semantic():
    # 'concepts' takes violet; unknown folders must avoid violet until palette exhausts
    c = viz.assign_colors(["concepts", "x1", "x2"])
    assert c["x1"] != c["concepts"]
    assert c["x2"] != c["concepts"]
    assert c["x1"] != c["x2"]


def test_more_folders_than_palette_does_not_crash():
    names = [f"f{i}" for i in range(40)]
    c = viz.assign_colors(names)
    assert len(c) == 40
    assert all(isinstance(v, int) for v in c.values())


def test_case_insensitive_semantic():
    c = viz.assign_colors(["Concepts", "SUMMARIES"])
    assert c["Concepts"] == 0x9B7DE0
    assert c["SUMMARIES"] == 0x2FB6A8


# ── query building ──────────────────────────────────────────────────────────────

def test_query_quotes_names_with_spaces():
    assert viz._q("Lens Effects") == 'path:"Lens Effects"'
    assert viz._q("concepts", "wiki/") == "path:wiki/concepts"
    assert viz._q("a b", "wiki/") == 'path:"wiki/a b"'


def test_hub_query_constant():
    assert "file:index" in viz.HUB_QUERY
    assert viz.HUB_RGB == 0xFFF0C2
