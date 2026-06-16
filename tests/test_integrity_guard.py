"""tests/test_integrity_guard.py — Tests for integrity_guard.py.

Each check function is covered with:
  - a *violating* case (produces at least one violation)
  - a *clean* case (produces no violations)

Special invariants:
  - Real Cyrillic (U+0400–U+04FF) MUST NOT be flagged as mojibake.
  - U+FFFD MUST be flagged as mojibake.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Ensure scripts/ is importable regardless of how pytest is invoked.
sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))

from integrity_guard import (
    _has_mojibake,
    check_accepted_provenance_valid,
    check_drafts_have_no_tool_ngrams,
    check_graph_counts_match_disk,
    check_no_mojibake_in_jsonl,
    check_queue_noise_ratio,
    check_thresholds_in_bounds,
    run_all,
)


# ---------------------------------------------------------------------------
# Mojibake detection unit tests
# ---------------------------------------------------------------------------


class TestHasMojibake:
    """Unit tests for the _has_mojibake helper."""

    def test_replacement_char_flagged(self):
        assert _has_mojibake("hello � world") is True

    def test_replacement_char_in_cyrillic_context(self):
        # Even mixed with real Cyrillic, the U+FFFD must trigger.
        assert _has_mojibake("Текст � test") is True

    def test_classic_bigram_D_flagged(self):
        # Ð followed by non-ASCII — mangled Cyrillic.
        assert _has_mojibake("ÐºÑ\x80Ð¸Ñ\x80Ð¸Ð»Ð»Ð") is True

    def test_classic_bigram_N_flagged(self):
        assert _has_mojibake("Ñ\x82ÐµÐ¾Ñ\x80Ð¸") is True

    # --- Cyrillic is valid ---

    def test_real_cyrillic_not_flagged(self):
        """Real Cyrillic text (U+0400–U+04FF) must NOT be flagged."""
        assert _has_mojibake("Обичам да програмирам") is False

    def test_bulgarian_sentence_not_flagged(self):
        assert _has_mojibake("Системата работи нормално") is False

    def test_cyrillic_with_latin_not_flagged(self):
        assert _has_mojibake("project: Facturka.bg — Фактура") is False

    def test_mixed_cyrillic_and_code_not_flagged(self):
        assert _has_mojibake("Статус: OK, дата: 2026-06-13") is False

    def test_clean_ascii_not_flagged(self):
        assert _has_mojibake("clean ascii text") is False

    def test_empty_string_not_flagged(self):
        assert _has_mojibake("") is False


# ---------------------------------------------------------------------------
# Check 1 — drafts_have_no_tool_ngrams
# ---------------------------------------------------------------------------


class TestDraftsHaveNoToolNgrams:
    def test_violating_tool_ngram_dir(self, tmp_path):
        """A directory whose name is a pure tool-ngram triggers a high violation."""
        drafts = tmp_path / "skill-drafts"
        (drafts / "edit-read-write").mkdir(parents=True)
        violations = check_drafts_have_no_tool_ngrams(drafts_dir=drafts)
        assert len(violations) == 1
        assert violations[0]["severity"] == "high"
        assert violations[0]["check"] == "drafts_have_no_tool_ngrams"
        assert "edit-read-write" in violations[0]["detail"]

    def test_multiple_tool_ngram_dirs(self, tmp_path):
        """Multiple tool-ngram dirs are reported together."""
        drafts = tmp_path / "skill-drafts"
        (drafts / "grep-cat").mkdir(parents=True)
        (drafts / "git-find").mkdir(parents=True)
        violations = check_drafts_have_no_tool_ngrams(drafts_dir=drafts)
        assert len(violations) == 1
        assert "2" in violations[0]["detail"]

    def test_clean_real_skill_names(self, tmp_path):
        """Directories with meaningful skill names do not trigger violations."""
        drafts = tmp_path / "skill-drafts"
        (drafts / "powershell-monitor").mkdir(parents=True)
        (drafts / "fix-flow").mkdir(parents=True)
        violations = check_drafts_have_no_tool_ngrams(drafts_dir=drafts)
        assert violations == []

    def test_missing_dir_is_clean(self, tmp_path):
        """A non-existent skill-drafts dir returns no violations."""
        violations = check_drafts_have_no_tool_ngrams(drafts_dir=tmp_path / "no-such-dir")
        assert violations == []

    def test_mixed_dirs_only_flags_ngrams(self, tmp_path):
        """Only tool-ngram dirs are counted, real skill dirs are ignored."""
        drafts = tmp_path / "skill-drafts"
        (drafts / "read-write").mkdir(parents=True)   # tool-ngram
        (drafts / "powershell-monitor").mkdir(parents=True)  # real skill
        violations = check_drafts_have_no_tool_ngrams(drafts_dir=drafts)
        assert len(violations) == 1
        assert "1" in violations[0]["detail"]
        assert "powershell-monitor" not in violations[0]["detail"]


# ---------------------------------------------------------------------------
# Check 2 — thresholds_in_bounds
# ---------------------------------------------------------------------------


class TestThresholdsInBounds:
    def _write(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_violating_below_range(self, tmp_path):
        """Value below 50 triggers a medium violation."""
        tp = tmp_path / "thresholds.json"
        ep = tmp_path / "effectiveness.json"
        self._write(tp, {"habit_distinctiveness_min": 30.0})
        self._write(ep, {})
        violations = check_thresholds_in_bounds(thresholds_path=tp, effectiveness_path=ep)
        assert any(v["severity"] == "medium" for v in violations)
        assert any("outside" in v["detail"] for v in violations)

    def test_violating_above_range(self, tmp_path):
        """Value above 500 triggers a medium violation."""
        tp = tmp_path / "thresholds.json"
        ep = tmp_path / "effectiveness.json"
        self._write(tp, {"habit_distinctiveness_min": 999.0})
        self._write(ep, {})
        violations = check_thresholds_in_bounds(thresholds_path=tp, effectiveness_path=ep)
        assert any("outside" in v["detail"] for v in violations)

    def test_violating_lowered_extreme_inflated_precision(self, tmp_path):
        """Value < 150 with high precision and low samples triggers a second violation."""
        tp = tmp_path / "thresholds.json"
        ep = tmp_path / "effectiveness.json"
        self._write(tp, {"habit_distinctiveness_min": 120.0})
        self._write(ep, {
            "precision_by_type": {"habit": 0.75},
            "samples_by_type": {"habit": 10},
        })
        violations = check_thresholds_in_bounds(thresholds_path=tp, effectiveness_path=ep)
        details = " ".join(v["detail"] for v in violations)
        assert "inflated" in details

    def test_lowered_extreme_but_enough_samples(self, tmp_path):
        """Value < 150 with high precision but enough samples does NOT flag inflated."""
        tp = tmp_path / "thresholds.json"
        ep = tmp_path / "effectiveness.json"
        self._write(tp, {"habit_distinctiveness_min": 120.0})
        self._write(ep, {
            "precision_by_type": {"habit": 0.75},
            "samples_by_type": {"habit": 100},  # >= 50 samples: valid
        })
        violations = check_thresholds_in_bounds(thresholds_path=tp, effectiveness_path=ep)
        assert not any("inflated" in v["detail"] for v in violations)

    def test_clean_normal_value(self, tmp_path):
        """Value within [150, 500] and normal precision returns no violations."""
        tp = tmp_path / "thresholds.json"
        ep = tmp_path / "effectiveness.json"
        self._write(tp, {"habit_distinctiveness_min": 195.0})
        self._write(ep, {
            "precision_by_type": {"habit": 0.007},
            "samples_by_type": {"habit": 581},
        })
        violations = check_thresholds_in_bounds(thresholds_path=tp, effectiveness_path=ep)
        assert violations == []

    def test_missing_key_flagged(self, tmp_path):
        """Missing habit_distinctiveness_min key triggers a medium violation."""
        tp = tmp_path / "thresholds.json"
        ep = tmp_path / "effectiveness.json"
        self._write(tp, {"other_key": 100})
        self._write(ep, {})
        violations = check_thresholds_in_bounds(thresholds_path=tp, effectiveness_path=ep)
        assert any("absent" in v["detail"] for v in violations)

    def test_missing_file_is_clean(self, tmp_path):
        """Missing thresholds.json flags 'absent' violation."""
        tp = tmp_path / "no-thresholds.json"
        ep = tmp_path / "no-eff.json"
        violations = check_thresholds_in_bounds(thresholds_path=tp, effectiveness_path=ep)
        assert any("absent" in v["detail"] for v in violations)


# ---------------------------------------------------------------------------
# Check 3 — graph_counts_match_disk
# ---------------------------------------------------------------------------


class TestGraphCountsMatchDisk:
    def _write_graph(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_violating_count_mismatch(self, tmp_path):
        """critical_rule_count claiming 37 when 14 actual rules triggers high violation."""
        gp = tmp_path / "knowledge_graph.json"
        self._write_graph(gp, {
            "meta": {"critical_rule_count": 37, "description": "clean"},
            "critical_rules": ["rule"] * 14,
        })
        violations = check_graph_counts_match_disk(graph_path=gp)
        assert any(v["severity"] == "high" for v in violations)
        details = " ".join(v["detail"] for v in violations)
        assert "37" in details and "14" in details

    def test_violating_mojibake_in_description(self, tmp_path):
        """U+FFFD in meta.description triggers a medium violation."""
        gp = tmp_path / "knowledge_graph.json"
        self._write_graph(gp, {
            "meta": {"description": "bad � encoding"},
            "critical_rules": [],
        })
        violations = check_graph_counts_match_disk(graph_path=gp)
        assert any("U+FFFD" in v["detail"] for v in violations)

    def test_clean_matching_counts(self, tmp_path):
        """Matching counts and clean description returns no violations."""
        gp = tmp_path / "knowledge_graph.json"
        rules = ["rule1", "rule2", "rule3"]
        self._write_graph(gp, {
            "meta": {
                "critical_rule_count": 3,
                "description": "clean description no replacement chars",
            },
            "critical_rules": rules,
        })
        violations = check_graph_counts_match_disk(graph_path=gp)
        assert violations == []

    def test_scalar_test_count_not_treated_as_array(self, tmp_path):
        """test_count/script_count are scalar metrics — a valid int must NOT
        be flagged as a missing 'tests' array (the prior mis-mapping bug)."""
        gp = tmp_path / "knowledge_graph.json"
        self._write_graph(gp, {
            "meta": {"cluster_count": 2, "test_count": 1099, "script_count": 104,
                     "description": "ok"},
            "clusters": {"a": 1, "b": 2},
            "critical_rules": [],
        })
        violations = check_graph_counts_match_disk(graph_path=gp)
        assert violations == []

    def test_scalar_test_count_non_int_flagged_low(self, tmp_path):
        gp = tmp_path / "knowledge_graph.json"
        self._write_graph(gp, {
            "meta": {"test_count": "lots", "description": "ok"},
            "critical_rules": [],
        })
        violations = check_graph_counts_match_disk(graph_path=gp)
        assert any(v["severity"] == "low" and "test_count" in v["detail"]
                   for v in violations)

    def test_no_count_fields_in_meta_is_clean(self, tmp_path):
        """If meta has no count fields, nothing to verify — should be clean."""
        gp = tmp_path / "knowledge_graph.json"
        self._write_graph(gp, {
            "meta": {"description": "valid text"},
            "critical_rules": ["a", "b"],
            "clusters": {"c1": {}, "c2": {}},
        })
        violations = check_graph_counts_match_disk(graph_path=gp)
        assert violations == []

    def test_cluster_count_mismatch(self, tmp_path):
        """cluster_count mismatch is flagged."""
        gp = tmp_path / "knowledge_graph.json"
        self._write_graph(gp, {
            "meta": {"cluster_count": 10, "description": "ok"},
            "clusters": {"a": {}, "b": {}},
        })
        violations = check_graph_counts_match_disk(graph_path=gp)
        assert any("cluster_count" in v["detail"] for v in violations)

    def test_missing_file_returns_low_note(self, tmp_path):
        """Non-existent graph file returns a low-severity note."""
        violations = check_graph_counts_match_disk(graph_path=tmp_path / "no-graph.json")
        assert len(violations) == 1
        assert violations[0]["severity"] == "low"

    def test_cyrillic_description_not_flagged(self, tmp_path):
        """Real Cyrillic in meta.description must NOT trigger mojibake violation."""
        gp = tmp_path / "knowledge_graph.json"
        self._write_graph(gp, {
            "meta": {
                "description": "AI workspace — самоподобряващи се скриптове за Claude Code",
            },
            "critical_rules": [],
        })
        violations = check_graph_counts_match_disk(graph_path=gp)
        assert not any("U+FFFD" in v["detail"] for v in violations)


# ---------------------------------------------------------------------------
# Check 4 — no_mojibake_in_jsonl
# ---------------------------------------------------------------------------


class TestNoMojibakeInJsonl:
    def test_violating_replacement_char_in_jsonl(self, tmp_path):
        """U+FFFD in a JSONL text field triggers a violation."""
        cr = tmp_path / "cross-recall-surfaced.jsonl"
        cr.write_text(
            json.dumps({"ts": "2026-06-13T00:00:00Z", "project": "test", "text": "bad � char"}) + "\n",
            encoding="utf-8",
        )
        inc = tmp_path / "incidents.json"
        violations = check_no_mojibake_in_jsonl(cross_recall_path=cr, incidents_path=inc)
        assert any("cross-recall" in v["detail"] for v in violations)

    def test_violating_mojibake_bigram_in_jsonl(self, tmp_path):
        """Classic cp1252 mojibake bigram in a JSONL project field triggers violation."""
        cr = tmp_path / "cross-recall-surfaced.jsonl"
        # Ð followed by non-ASCII — mangled Cyrillic
        cr.write_text(
            json.dumps({"ts": "2026-06-13T00:00:00Z", "project": "ÐºÐ¾Ð´"}) + "\n",
            encoding="utf-8",
        )
        inc = tmp_path / "incidents.json"
        violations = check_no_mojibake_in_jsonl(cross_recall_path=cr, incidents_path=inc)
        assert any("cross-recall" in v["detail"] for v in violations)

    def test_clean_cyrillic_in_jsonl_not_flagged(self, tmp_path):
        """Real Cyrillic in JSONL must NOT be flagged — this is the most important test."""
        cr = tmp_path / "cross-recall-surfaced.jsonl"
        cr.write_text(
            json.dumps({"ts": "2026-06-13T00:00:00Z", "project": "Facturka.bg", "text": "Тест на кирилица"}) + "\n" +
            json.dumps({"ts": "2026-06-13T00:01:00Z", "project": "Claude", "text": "Системата работи"}) + "\n",
            encoding="utf-8",
        )
        inc = tmp_path / "incidents.json"
        violations = check_no_mojibake_in_jsonl(cross_recall_path=cr, incidents_path=inc)
        assert violations == []

    def test_clean_ascii_jsonl(self, tmp_path):
        """ASCII-only JSONL returns no violations."""
        cr = tmp_path / "cross-recall-surfaced.jsonl"
        cr.write_text(
            json.dumps({"ts": "2026-06-13T00:00:00Z", "project": "Test", "text": "clean"}) + "\n",
            encoding="utf-8",
        )
        inc = tmp_path / "incidents.json"
        violations = check_no_mojibake_in_jsonl(cross_recall_path=cr, incidents_path=inc)
        assert violations == []

    def test_violating_replacement_char_in_incidents(self, tmp_path):
        """U+FFFD in incidents.json triggers a violation."""
        cr = tmp_path / "cross-recall-surfaced.jsonl"
        inc = tmp_path / "incidents.json"
        inc.write_text(
            json.dumps({"open": [{"project": "test", "title": "error � here"}]}),
            encoding="utf-8",
        )
        violations = check_no_mojibake_in_jsonl(cross_recall_path=cr, incidents_path=inc)
        assert any("incidents.json" in v["detail"] for v in violations)

    def test_missing_files_are_clean(self, tmp_path):
        """Missing input files return no violations."""
        violations = check_no_mojibake_in_jsonl(
            cross_recall_path=tmp_path / "no-file.jsonl",
            incidents_path=tmp_path / "no-incidents.json",
        )
        assert violations == []

    def test_ufffd_flagged_explicitly(self, tmp_path):
        """Explicitly verify U+FFFD is flagged (spec requirement)."""
        cr = tmp_path / "cross-recall-surfaced.jsonl"
        line = {"project": "p", "text": "�"}
        cr.write_text(json.dumps(line) + "\n", encoding="utf-8")
        inc = tmp_path / "incidents.json"
        violations = check_no_mojibake_in_jsonl(cross_recall_path=cr, incidents_path=inc)
        assert len(violations) >= 1


# ---------------------------------------------------------------------------
# Check 5 — accepted_provenance_valid
# ---------------------------------------------------------------------------


class TestAcceptedProvenanceValid:
    def test_violating_missing_accepted_by_user(self, tmp_path):
        """An accepted proposal without accepted_by_user triggers a violation."""
        fp = tmp_path / "fix-proposals.json"
        fp.write_text(json.dumps({
            "proposals": [
                {"id": "abc123", "status": "accepted"},
            ]
        }), encoding="utf-8")
        violations = check_accepted_provenance_valid(fix_proposals_path=fp)
        assert len(violations) == 1
        assert "abc123" in violations[0]["detail"]

    def test_violating_invalid_date_format(self, tmp_path):
        """An accepted proposal with a non-ISO accepted_by_user triggers a violation."""
        fp = tmp_path / "fix-proposals.json"
        fp.write_text(json.dumps({
            "proposals": [
                {"id": "xyz789", "status": "accepted", "accepted_by_user": "yesterday"},
            ]
        }), encoding="utf-8")
        violations = check_accepted_provenance_valid(fix_proposals_path=fp)
        assert any("xyz789" in v["detail"] for v in violations)

    def test_clean_valid_iso_date(self, tmp_path):
        """An accepted proposal with a valid ISO date returns no violations."""
        fp = tmp_path / "fix-proposals.json"
        fp.write_text(json.dumps({
            "proposals": [
                {"id": "good01", "status": "accepted", "accepted_by_user": "2026-06-11"},
                {"id": "good02", "status": "proposed"},  # Not accepted — skip.
            ]
        }), encoding="utf-8")
        violations = check_accepted_provenance_valid(fix_proposals_path=fp)
        assert violations == []

    def test_clean_full_iso_datetime(self, tmp_path):
        """Full ISO datetime string is also valid."""
        fp = tmp_path / "fix-proposals.json"
        fp.write_text(json.dumps({
            "proposals": [
                {"id": "dt01", "status": "accepted", "accepted_by_user": "2026-06-11T14:30:00"},
            ]
        }), encoding="utf-8")
        violations = check_accepted_provenance_valid(fix_proposals_path=fp)
        assert violations == []

    def test_proposed_without_provenance_not_flagged(self, tmp_path):
        """Non-accepted proposals do not need provenance."""
        fp = tmp_path / "fix-proposals.json"
        fp.write_text(json.dumps({
            "proposals": [
                {"id": "p1", "status": "proposed"},
                {"id": "p2", "status": "resolved"},
            ]
        }), encoding="utf-8")
        violations = check_accepted_provenance_valid(fix_proposals_path=fp)
        assert violations == []

    def test_missing_file_is_clean(self, tmp_path):
        """Missing fix-proposals.json returns no violations."""
        violations = check_accepted_provenance_valid(fix_proposals_path=tmp_path / "no-file.json")
        assert violations == []

    def test_list_format_also_supported(self, tmp_path):
        """fix-proposals.json as a bare list (not wrapped in dict) is supported."""
        fp = tmp_path / "fix-proposals.json"
        fp.write_text(json.dumps([
            {"id": "bare01", "status": "accepted", "accepted_by_user": "2026-06-13"},
        ]), encoding="utf-8")
        violations = check_accepted_provenance_valid(fix_proposals_path=fp)
        assert violations == []


# ---------------------------------------------------------------------------
# Check 6 — queue_noise_ratio
# ---------------------------------------------------------------------------


class TestQueueNoiseRatio:
    def _habit(self, slug: str, project: str = "proj") -> dict:
        return {
            "id": f"habit-{project}-{slug}",
            "type": "habit",
            "description": f"test habit {slug}",
        }

    def test_violating_high_noise_ratio(self, tmp_path):
        """Majority of habit slugs being tool-ngrams triggers a low violation."""
        qp = tmp_path / "queue.json"
        items = [
            self._habit("read-write"),        # tool-ngram
            self._habit("grep-cat"),           # tool-ngram
            self._habit("powershell-monitor"), # real skill
        ]
        qp.write_text(json.dumps(items), encoding="utf-8")
        violations = check_queue_noise_ratio(queue_path=qp, threshold=0.1)
        assert len(violations) == 1
        assert violations[0]["severity"] == "low"

    def test_clean_no_tool_ngrams(self, tmp_path):
        """Queue with no tool-ngram habit slugs returns no violations."""
        qp = tmp_path / "queue.json"
        items = [
            self._habit("powershell-monitor"),
            self._habit("fix-flow"),
            self._habit("security-aud-code-reviewe"),
        ]
        qp.write_text(json.dumps(items), encoding="utf-8")
        violations = check_queue_noise_ratio(queue_path=qp, threshold=0.1)
        assert violations == []

    def test_below_threshold_is_clean(self, tmp_path):
        """One tool-ngram in ten habits is below 0.1 threshold — clean."""
        qp = tmp_path / "queue.json"
        items = [self._habit("real-skill")] * 9 + [self._habit("read-write")]
        qp.write_text(json.dumps(items), encoding="utf-8")
        violations = check_queue_noise_ratio(queue_path=qp, threshold=0.11)
        assert violations == []

    def test_non_habit_items_ignored(self, tmp_path):
        """boris_rule items with tool-ngram-like ids are not counted."""
        qp = tmp_path / "queue.json"
        items = [
            {"id": "boris-proj-read-write", "type": "boris_rule", "description": "x"},
            self._habit("real-skill"),
        ]
        qp.write_text(json.dumps(items), encoding="utf-8")
        violations = check_queue_noise_ratio(queue_path=qp)
        assert violations == []

    def test_empty_queue_is_clean(self, tmp_path):
        """An empty queue returns no violations."""
        qp = tmp_path / "queue.json"
        qp.write_text("[]", encoding="utf-8")
        violations = check_queue_noise_ratio(queue_path=qp)
        assert violations == []

    def test_missing_file_is_clean(self, tmp_path):
        """Missing queue file returns no violations."""
        violations = check_queue_noise_ratio(queue_path=tmp_path / "no-queue.json")
        assert violations == []


# ---------------------------------------------------------------------------
# run_all integration test
# ---------------------------------------------------------------------------


class TestRunAll:
    def test_run_all_returns_schema(self, tmp_path):
        """run_all() always returns a dict with required keys, even on empty inputs."""
        fixed_dt = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
        report = run_all(
            drafts_dir=tmp_path / "skill-drafts",
            thresholds_path=tmp_path / "thresholds.json",
            effectiveness_path=tmp_path / "effectiveness.json",
            graph_path=tmp_path / "knowledge_graph.json",
            cross_recall_path=tmp_path / "cross-recall-surfaced.jsonl",
            incidents_path=tmp_path / "incidents.json",
            fix_proposals_path=tmp_path / "fix-proposals.json",
            queue_path=tmp_path / "improvement-queue.json",
            now=fixed_dt,
        )
        assert "generated" in report
        assert "violations" in report
        assert "counts" in report
        assert report["generated"] == "2026-06-13T12:00:00+00:00"
        assert isinstance(report["violations"], list)
        assert set(report["counts"].keys()) == {"critical", "high", "medium", "low"}

    def test_run_all_counts_match_violations(self, tmp_path):
        """counts dict must sum to len(violations)."""
        # Inject one known violation: a tool-ngram draft dir.
        drafts = tmp_path / "skill-drafts"
        (drafts / "read-edit").mkdir(parents=True)

        report = run_all(
            drafts_dir=drafts,
            thresholds_path=tmp_path / "thresholds.json",
            effectiveness_path=tmp_path / "effectiveness.json",
            graph_path=tmp_path / "knowledge_graph.json",
            cross_recall_path=tmp_path / "cross-recall-surfaced.jsonl",
            incidents_path=tmp_path / "incidents.json",
            fix_proposals_path=tmp_path / "fix-proposals.json",
            queue_path=tmp_path / "improvement-queue.json",
        )
        total_from_counts = sum(report["counts"].values())
        assert total_from_counts == len(report["violations"])

    def test_run_all_all_clean(self, tmp_path):
        """A fully clean environment produces zero violations."""
        # Write minimal valid files.
        thresholds = tmp_path / "thresholds.json"
        thresholds.write_text(json.dumps({"habit_distinctiveness_min": 195.0}), encoding="utf-8")

        effectiveness = tmp_path / "effectiveness.json"
        effectiveness.write_text(json.dumps({
            "precision_by_type": {"habit": 0.007},
            "samples_by_type": {"habit": 581},
        }), encoding="utf-8")

        graph = tmp_path / "knowledge_graph.json"
        graph.write_text(json.dumps({
            "meta": {"description": "clean"},
        }), encoding="utf-8")

        cr = tmp_path / "cross-recall-surfaced.jsonl"
        cr.write_text(
            json.dumps({"project": "Facturka.bg", "text": "Тест"}) + "\n",
            encoding="utf-8",
        )

        inc = tmp_path / "incidents.json"
        inc.write_text(json.dumps({"open": []}), encoding="utf-8")

        fp = tmp_path / "fix-proposals.json"
        fp.write_text(json.dumps({
            "proposals": [
                {"id": "ok1", "status": "accepted", "accepted_by_user": "2026-06-11"},
            ]
        }), encoding="utf-8")

        queue = tmp_path / "improvement-queue.json"
        queue.write_text(json.dumps([
            {"id": "habit-proj-powershell-monitor", "type": "habit", "description": "x"},
        ]), encoding="utf-8")

        drafts = tmp_path / "skill-drafts"
        (drafts / "real-skill").mkdir(parents=True)

        report = run_all(
            drafts_dir=drafts,
            thresholds_path=thresholds,
            effectiveness_path=effectiveness,
            graph_path=graph,
            cross_recall_path=cr,
            incidents_path=inc,
            fix_proposals_path=fp,
            queue_path=queue,
        )
        assert report["violations"] == []
        assert sum(report["counts"].values()) == 0
