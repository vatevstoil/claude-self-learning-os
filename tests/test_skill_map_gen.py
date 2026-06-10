"""Tests for skill_map_gen.py (frontmatter parse, condense, render)."""
import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
smg = importlib.import_module("skill_map_gen")


# ── frontmatter ──────────────────────────────────────────────────────────────

def test_parse_simple_frontmatter():
    fm = smg.parse_frontmatter("---\nname: fix-flow\ndescription: Do a thing\n---\n# body")
    assert fm["name"] == "fix-flow"
    assert fm["description"] == "Do a thing"


def test_parse_quoted_description():
    fm = smg.parse_frontmatter('---\nname: x\ndescription: "Quoted desc here"\n---\n')
    assert fm["description"] == "Quoted desc here"


def test_parse_no_frontmatter():
    assert smg.parse_frontmatter("# just a title\nbody") == {}


def test_parse_folded_continuation():
    fm = smg.parse_frontmatter("---\ndescription: line one\n  line two\n---\n")
    assert fm["description"] == "line one line two"


# ── condense ─────────────────────────────────────────────────────────────────

def test_condense_short_passthrough():
    assert smg.condense("Short text") == "Short text"


def test_condense_trims_at_word_boundary():
    long = "word " * 100
    out = smg.condense(long, limit=40)
    assert len(out) <= 42
    assert out.endswith("…")
    assert "wor…" not in out  # cut at a space, not mid-word


def test_condense_extracts_not_for():
    out = smg.condense("Use when X happens. NOT for unknown root cause", limit=200)
    assert "⛔" in out
    assert "unknown root cause" in out
    assert "NOT for" not in out  # the marker phrase itself is stripped


# ── render ───────────────────────────────────────────────────────────────────

def test_build_map_sections_and_counts():
    now = datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)
    md = smg.build_map(now, [("fix-flow", "fix stuff")], [("/handoff", "hand off")])
    assert "## Skills (1)" in md
    assert "## Slash commands (1)" in md
    assert "**fix-flow** — fix stuff" in md
    assert "**/handoff** — hand off" in md
