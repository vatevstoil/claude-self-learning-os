"""Tests for wikilink_checker.py pure helpers."""
import importlib, sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
wc = importlib.import_module("wikilink_checker")


def test_extract_basic():
    assert wc.extract_links("see [[Note A]] and [[Note B]]") == ["Note A", "Note B"]


def test_extract_strips_alias_and_heading():
    assert wc.extract_links("[[Target|Alias]]") == ["Target"]
    assert wc.extract_links("[[Target#Section]]") == ["Target"]
    assert wc.extract_links("[[folder/Target#Sec|Alias]]") == ["folder/Target"]


def test_extract_none():
    assert wc.extract_links("no links here") == []


def test_resolves_bare_name_to_md():
    stems = {"portfolio", "index"}
    assert wc.resolves("portfolio", stems, set()) is True
    assert wc.resolves("missing", stems, set()) is False


def test_resolves_by_basename_of_path():
    stems = {"Target"}
    assert wc.resolves("folder/Target", stems, set()) is True


def test_resolves_image_by_filename():
    assert wc.resolves("diagram.png", set(), {"diagram.png"}) is True
    assert wc.resolves("ghost.png", set(), {"diagram.png"}) is False


def test_extension_does_not_match_md_stems():
    # a .png link must NOT resolve just because a same-stem .md exists
    assert wc.resolves("foo.png", {"foo"}, set()) is False


def test_dot_in_name_is_not_an_extension():
    # "3.0" / "Fakturka.bg" contain dots but are NOT file extensions
    assert wc.resolves("software-3.0-strategy", {"software-3.0-strategy"}, set()) is True
    assert wc.resolves("Fakturka.bg", {"Fakturka.bg"}, set()) is True


def test_explicit_md_resolves_by_stem():
    assert wc.resolves("portfolio.md", {"portfolio"}, set()) is True


def test_table_escaped_pipe_resolves():
    # [[roles/director\|Director]] inside a table -> target captured as "roles/director\"
    assert wc.resolves("roles/director\\", {"director"}, set()) is True
    assert wc.extract_links("[[roles/director\\|Director]]") == ["roles/director"]


def test_case_insensitive_like_obsidian():
    # Obsidian resolves links case-insensitively
    assert wc.resolves("Image-To-Video-Pipeline", {"image-to-video-pipeline"}, set()) is True
    assert wc.resolves("PORTFOLIO.png", set(), {"portfolio.png"}) is True
