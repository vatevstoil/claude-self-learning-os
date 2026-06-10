import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def test_generate_draft_contains_rule_and_evidence():
    from boris_draft import generate_draft
    draft = generate_draft("MyProj", {"count": 6, "examples": ["не прави X", "не така, прави Y"]})
    assert "MyProj" in draft
    assert "не прави X"[:10] in draft
    assert "CLAUDE.md" in draft


def test_write_drafts_threshold(tmp_path):
    from boris_draft import write_drafts
    boris = tmp_path / "boris.json"
    boris.write_text(json.dumps({"projects": {
        "Big": {"count": 6, "examples": ["не"]},
        "Small": {"count": 2, "examples": ["не"]},
    }}), encoding="utf-8")
    out = tmp_path / "drafts"
    written = write_drafts(boris_path=boris, out_dir=out, min_count=4)
    names = [p.name for p in written]
    assert "Big.md" in names
    assert "Small.md" not in names


def test_write_drafts_missing_file_returns_empty(tmp_path):
    from boris_draft import write_drafts
    written = write_drafts(boris_path=tmp_path / "nope.json", out_dir=tmp_path / "d", min_count=4)
    assert written == []


def test_summarize_corrections_nonempty():
    from boris_draft import summarize_corrections
    s = summarize_corrections(["не прави това, използвай другия подход"])
    assert isinstance(s, str) and len(s) > 0


def test_write_drafts_tolerates_corrupt_input(tmp_path):
    from boris_draft import write_drafts
    # null example, non-numeric count, all should be tolerated without crashing
    boris = tmp_path / "boris.json"
    boris.write_text(json.dumps({"projects": {
        "NullEx": {"count": 6, "examples": [None, "не прави X"]},
        "BadCount": {"count": "lots", "examples": ["не"]},
    }}), encoding="utf-8")
    out = tmp_path / "drafts"
    written = write_drafts(boris_path=boris, out_dir=out, min_count=4)
    # NullEx qualifies (count 6); BadCount's count coerces to 0 -> excluded. No crash.
    assert any(p.name == "NullEx.md" for p in written)


def test_write_drafts_non_dict_json_returns_empty(tmp_path):
    from boris_draft import write_drafts
    boris = tmp_path / "boris.json"
    boris.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    written = write_drafts(boris_path=boris, out_dir=tmp_path / "d", min_count=4)
    assert written == []


def test_summarize_corrections_tolerates_null():
    from boris_draft import summarize_corrections
    s = summarize_corrections([None, "не прави това"])
    assert isinstance(s, str) and len(s) > 0


def test_process_accepted_applies_rule(tmp_path):
    from boris_draft import process_accepted_boris, _safe_filename

    # Create a draft file
    drafts_dir = tmp_path / "boris-drafts"
    drafts_dir.mkdir()
    project_key = "X--Projects-TestProj"
    draft_content = """# Boris Draft -- X--Projects-TestProj

## Proposed Rule

```
always run tests before commit
```

## Target File

`X:\\Projects\\TestProj\\CLAUDE.md`
"""
    draft_file = drafts_dir / (_safe_filename(project_key) + ".md")
    draft_file.write_text(draft_content, encoding="utf-8")

    # Create accepted-boris queue
    accepted = tmp_path / "accepted-boris.json"
    accepted.write_text(json.dumps([f"boris_rule-{project_key}"]), encoding="utf-8")

    # Create a fake CLAUDE.md
    claude_dir = tmp_path / "TestProj"
    claude_dir.mkdir()
    claude_md = claude_dir / "CLAUDE.md"
    claude_md.write_text("# Project Rules\n## Learned Rules\n", encoding="utf-8")

    # Monkey-patch path resolution
    import boris_draft as bd
    original_encode = bd._encode_to_path_hint
    bd._encode_to_path_hint = lambda k: str(claude_dir)

    try:
        applied = process_accepted_boris(accepted_path=accepted, drafts_dir=drafts_dir)
        assert len(applied) == 1
        content = claude_md.read_text(encoding="utf-8")
        assert "always run tests before commit" in content
        # Queue should be cleared
        assert json.loads(accepted.read_text()) == []
    finally:
        bd._encode_to_path_hint = original_encode


def test_process_accepted_empty_queue(tmp_path):
    from boris_draft import process_accepted_boris
    accepted = tmp_path / "accepted.json"
    accepted.write_text("[]", encoding="utf-8")
    result = process_accepted_boris(accepted_path=accepted, drafts_dir=tmp_path)
    assert result == []


def test_process_accepted_missing_draft_skips_gracefully(tmp_path):
    from boris_draft import process_accepted_boris
    accepted = tmp_path / "accepted.json"
    accepted.write_text(json.dumps(["boris_rule-nonexistent"]), encoding="utf-8")
    # No draft file exists — should not raise
    result = process_accepted_boris(accepted_path=accepted, drafts_dir=tmp_path)
    assert result == []


# --- _resolve_real_project_dir: fuzzy match for lossy path encoding ----------

def test_resolve_exact_path_returned_asis(tmp_path):
    from boris_draft import _resolve_real_project_dir
    real = tmp_path / "MyProject"
    real.mkdir()
    assert _resolve_real_project_dir(str(real)) == real


def test_resolve_dash_matches_dot_folder(tmp_path):
    # Encoding collapses "." to "-": "Facturka-bg" must resolve to "Facturka.bg"
    from boris_draft import _resolve_real_project_dir
    (tmp_path / "Facturka.bg").mkdir()
    resolved = _resolve_real_project_dir(str(tmp_path / "Facturka-bg"))
    assert resolved == tmp_path / "Facturka.bg"


def test_resolve_dash_matches_space_folder(tmp_path):
    # "StroyOffice-Pro" must resolve to real "StroyOffice Pro"
    from boris_draft import _resolve_real_project_dir
    (tmp_path / "StroyOffice Pro").mkdir()
    resolved = _resolve_real_project_dir(str(tmp_path / "StroyOffice-Pro"))
    assert resolved == tmp_path / "StroyOffice Pro"


def test_resolve_no_match_returns_none(tmp_path):
    from boris_draft import _resolve_real_project_dir
    (tmp_path / "SomethingElse").mkdir()
    assert _resolve_real_project_dir(str(tmp_path / "DoesNotExist")) is None


def test_resolve_ambiguous_returns_none(tmp_path):
    # Two folders normalize to the same form → must refuse (None), not guess.
    from boris_draft import _resolve_real_project_dir
    (tmp_path / "Foo-Bar").mkdir()
    (tmp_path / "Foo.Bar").mkdir()
    # Candidate "Foo Bar" normalizes to same as both → ambiguous
    assert _resolve_real_project_dir(str(tmp_path / "Foo Bar")) is None


def test_resolve_missing_parent_returns_none(tmp_path):
    from boris_draft import _resolve_real_project_dir
    assert _resolve_real_project_dir(str(tmp_path / "no_such_parent" / "Proj")) is None
