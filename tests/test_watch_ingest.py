"""Tests for watch_ingest.py pure functions (slugify, build_raw format)."""
import importlib
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
wi = importlib.import_module("watch_ingest")


# ── slugify ──────────────────────────────────────────────────────────────────

def test_slugify_basic_kebab():
    assert wi.slugify("Hello World") == "hello-world.md"


def test_slugify_strips_punctuation():
    out = wi.slugify("I Turned Karpathy's Second Brain!")
    assert out.endswith(".md")
    assert "'" not in out and "!" not in out
    assert "--" not in out  # no double hyphens


def test_slugify_channel_prefix():
    out = wi.slugify("My Video", "AI Impact")
    assert out.startswith("ai-impact-")


def test_slugify_no_double_prefix():
    # if title already starts with channel slug, don't prepend again
    out = wi.slugify("AI Impact Weekly Roundup", "AI Impact")
    assert out.count("ai-impact") == 1


def test_slugify_length_capped():
    out = wi.slugify("word " * 100, "chan")
    assert len(out) <= 93  # 90 cap + ".md"
    assert out.endswith(".md")


def test_slugify_empty_title_fallback():
    assert wi.slugify("", "") == "video.md"


# ── build_raw ────────────────────────────────────────────────────────────────

def test_build_raw_header_contract():
    # kb-ingest reads line 1 = '# RAW: <title>', line 2 = 'Source: <url> -- <author>'
    info = {"title": "Test Vid", "url": "https://youtu.be/abc", "channel": "Chan", "description": "desc"}
    md = wi.build_raw(info, "[00:00] hello")
    lines = md.splitlines()
    assert lines[0] == "# RAW: Test Vid"
    assert lines[1] == "Source: https://youtu.be/abc -- Chan"
    assert "## Transcript" in md
    assert "[00:00] hello" in md


def test_build_raw_author_override():
    info = {"title": "T", "url": "u", "channel": "Auto", "description": ""}
    md = wi.build_raw(info, "x", author_override="Manual")
    assert "-- Manual" in md
    assert "-- Auto" not in md


def test_build_raw_truncates_long_description():
    info = {"title": "T", "url": "u", "channel": "C", "description": "x " * 2000}
    md = wi.build_raw(info, "t")
    assert "…" in md


def test_build_raw_empty_description_placeholder():
    info = {"title": "T", "url": "u", "channel": "C", "description": ""}
    md = wi.build_raw(info, "t")
    assert "(no description)" in md
