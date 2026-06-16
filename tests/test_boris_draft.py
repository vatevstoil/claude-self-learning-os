import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch):
    """Safety net: NO boris_draft test may hit the real Ollama endpoint.

    synthesize_rule_with_llm auto-imports ``llm_judge.judge_text`` whenever no
    judge_text_fn is injected. On a machine with Ollama actually running, that
    became a real, slow LLM call — the full suite hung for >4 minutes because of
    it. We patch the transport to a fast deterministic stub. Tests that inject
    their own judge_text_fn are unaffected (they never import llm_judge), and a
    test that needs to simulate an unavailable LLM re-patches it to return None.
    """
    import llm_judge
    monkeypatch.setattr(
        llm_judge, "judge_text",
        lambda *a, **k: {"verdict": "junk", "score": 0.9,
                         "reason": "stubbed test rule (no live LLM)"},
        raising=False,
    )


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


# ===========================================================================
# NEW TESTS — correction detector, LLM synthesis, dedup
# ===========================================================================


class TestIsCorrection:
    """Тестове за новия детектор на корекции."""

    def test_pure_question_mark_not_correction(self):
        from boris_draft import is_correction
        assert is_correction("може ли да анализираш компанията Realty Income?") is False

    def test_question_starter_without_negation_not_correction(self):
        from boris_draft import is_correction
        assert is_correction("може ли да ми покажеш как работи модулът") is False

    def test_question_starter_with_negation_is_correction(self):
        """'може ли' + 'не работи' → реална жалба, не въпрос."""
        from boris_draft import is_correction
        assert is_correction("може ли клавишната комбинация да не работи") is True

    def test_system_reminder_excluded(self):
        from boris_draft import is_correction
        assert is_correction("<system-reminder>Some system content</system-reminder>") is False

    def test_stop_hook_feedback_excluded(self):
        from boris_draft import is_correction
        msg = "Stop hook feedback:\n⚠ Wiki/Reed: log.md не е обновено 21д"
        assert is_correction(msg) is False

    def test_stop_hook_feedback_case_insensitive(self):
        from boris_draft import is_correction
        assert is_correction("STOP HOOK FEEDBACK: something") is False

    def test_real_keyboard_complaint_is_correction(self):
        from boris_draft import is_correction
        assert is_correction("клавишната комбинация за четене отново не работи") is True

    def test_real_terminal_complaint_is_correction(self):
        from boris_draft import is_correction
        assert is_correction("постоянно се отваря терминала и не се затваря") is True

    def test_real_dashboard_complaint_is_correction(self):
        from boris_draft import is_correction
        assert is_correction("дескборда неработи на 70% съм за да продам") is True

    def test_pure_question_with_question_mark_filtered(self):
        from boris_draft import is_correction
        # Ends with '?' and no negation word
        assert is_correction("кога ще е готово?") is False

    def test_empty_string_not_correction(self):
        from boris_draft import is_correction
        assert is_correction("") is False
        assert is_correction("   ") is False


class TestDedupTokens:
    """Triple token deduplication."""

    def test_triple_stop_collapsed(self):
        from boris_draft import _dedup_tokens
        result = _dedup_tokens("стоп стоп стоп стоп направи това")
        tokens = result.split()
        # At most 2 consecutive identical tokens
        for i in range(2, len(tokens)):
            assert not (tokens[i] == tokens[i - 1] == tokens[i - 2]), (
                f"Triple repetition found at position {i}: {tokens}"
            )

    def test_double_allowed(self):
        from boris_draft import _dedup_tokens
        result = _dedup_tokens("стоп стоп направи")
        assert result == "стоп стоп направи"

    def test_no_repetition_unchanged(self):
        from boris_draft import _dedup_tokens
        text = "клавишната комбинация не работи"
        assert _dedup_tokens(text) == text


class TestFilterCorrections:
    """filter_corrections pipeline."""

    def test_filters_out_questions(self):
        from boris_draft import filter_corrections
        examples = [
            "може ли да ми помогнеш?",
            "клавишната комбинация не работи",
        ]
        result = filter_corrections(examples)
        assert len(result) == 1
        assert "клавишната" in result[0]

    def test_filters_system_noise(self):
        from boris_draft import filter_corrections
        examples = [
            "Stop hook feedback:\n⚠ some noise",
            "програмата неработи",
        ]
        result = filter_corrections(examples)
        assert len(result) == 1

    def test_deduplicates_identical(self):
        from boris_draft import filter_corrections
        examples = ["не работи клавишната", "не работи клавишната", "програмата пада"]
        result = filter_corrections(examples)
        assert result.count("не работи клавишната") == 1

    def test_tolerates_null_and_empty(self):
        from boris_draft import filter_corrections
        result = filter_corrections([None, "", "   ", "не работи"])  # type: ignore
        assert len(result) == 1


class TestSynthesizeRuleWithLlm:
    """LLM synthesis — happy path и fallback."""

    def _make_judge_fn(self, verdict: str, reason: str) -> MagicMock:
        mock = MagicMock(return_value={"verdict": verdict, "score": 0.9, "reason": reason})
        return mock

    def test_llm_happy_path_returns_rule(self):
        from boris_draft import synthesize_rule_with_llm
        judge = self._make_judge_fn("junk", "Винаги рестартирай четенето при грешка в AHK")
        result = synthesize_rule_with_llm(["клавишната не работи"], judge_text_fn=judge)
        assert result == "Винаги рестартирай четенето при грешка в AHK"
        judge.assert_called_once()

    def test_llm_unavailable_returns_fallback(self, monkeypatch):
        """Auto-import path + unavailable transport (returns None) → safe fallback.

        judge_text_fn=None makes synthesize auto-import llm_judge.judge_text; we
        patch that to return None (deterministically simulating an unavailable
        Ollama, regardless of whether one is actually running) and assert the
        safe placeholder is produced rather than a crash or junk.
        """
        import llm_judge
        from boris_draft import synthesize_rule_with_llm
        monkeypatch.setattr(llm_judge, "judge_text", lambda *a, **k: None, raising=False)
        result = synthesize_rule_with_llm(["клавишната не работи"], judge_text_fn=None)
        assert result.startswith("NEEDS HUMAN REWRITE:")

    def test_llm_returns_none_gives_fallback(self):
        from boris_draft import synthesize_rule_with_llm
        judge = MagicMock(return_value=None)
        result = synthesize_rule_with_llm(["терминала се отваря"], judge_text_fn=judge)
        assert result.startswith("NEEDS HUMAN REWRITE:")

    def test_llm_long_rule_truncated_to_140(self):
        from boris_draft import synthesize_rule_with_llm
        long_rule = "Винаги " + "проверявай " * 15  # >140 chars
        judge = self._make_judge_fn("junk", long_rule)
        result = synthesize_rule_with_llm(["нещо не работи"], judge_text_fn=judge)
        assert len(result) <= 140

    def test_llm_result_not_echo_of_example(self):
        from boris_draft import synthesize_rule_with_llm
        example = "клавишната комбинация за четене отново не работи"
        # LLM returns proper rule, not verbatim echo
        rule = "Проверявай AHK скрипта при стартиране и логвай грешки"
        judge = self._make_judge_fn("junk", rule)
        result = synthesize_rule_with_llm([example], judge_text_fn=judge)
        assert result != example

    def test_no_examples_returns_fallback(self):
        from boris_draft import synthesize_rule_with_llm
        judge = MagicMock()
        result = synthesize_rule_with_llm([], judge_text_fn=judge)
        assert result.startswith("NEEDS HUMAN REWRITE:")
        judge.assert_not_called()

    def test_generate_draft_uses_fallback_in_fenced_block(self):
        """auto_apply reads the fenced block — must never be junk verbatim."""
        from boris_draft import generate_draft, _extract_proposed_rule
        # Force LLM unavailable (judge_text_fn=None → import attempt → None fallback)
        draft = generate_draft(
            "TestProj",
            {"count": 4, "examples": ["клавишната не работи", "програмата пада"]},
            judge_text_fn=MagicMock(return_value=None),
        )
        rule = _extract_proposed_rule(draft)
        assert rule is not None
        # Must be either a real rule OR the safe fallback marker
        assert len(rule) <= 140 or rule.startswith("NEEDS HUMAN REWRITE:")
