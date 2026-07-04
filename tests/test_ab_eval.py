"""Tests for ab_eval.py — A/B counterfactual eval via Interrupted Time Series.

Convention: all tests use injected ``now`` and ``tmp_path`` for isolation.
No network, no real Ollama, no real filesystem state.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))

# ---------------------------------------------------------------------------
# Fixed reference time — "today" in test-world
# ---------------------------------------------------------------------------

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
WINDOW = 21  # days — default window


# ---------------------------------------------------------------------------
# Helpers to build minimal fixture data
# ---------------------------------------------------------------------------


def _make_ledger_entry(
    item_id: str = "auto-boris-testproject",
    item_type: str = "boris_rule",
    ts_offset_days: float = -(WINDOW + 5),  # applied before both windows close
    project_dir: str = "TestProject",
    rule_preview: str = "always confirm before deleting files",
    tier: int = 1,
    rolled_back: bool = False,
) -> dict:
    """Build a minimal applied-ledger entry."""
    t_applied = NOW + timedelta(days=ts_offset_days)
    return {
        "item_id": item_id,
        "item_type": item_type,
        "tier": tier,
        "target_file": f"J:/Projects/{project_dir}/CLAUDE.md",
        "rollback_marker": item_id,
        "rule_preview": rule_preview,
        "ts": t_applied.isoformat(),
        "rolled_back": rolled_back,
    }


def _make_cluster(
    cluster_id: str = "abc12345",
    project: str = "testproject",
    representative: str = "files keep getting deleted without confirmation",
    examples_before: int = 0,
    examples_after: int = 0,
    ts_applied: datetime | None = None,
    window_days: int = WINDOW,
) -> dict:
    """Build a minimal cluster dict with complaints placed in before/after windows.

    All ``before`` complaints are placed at t_applied - window_days/2.
    All ``after`` complaints are placed at t_applied + window_days/2.
    """
    if ts_applied is None:
        ts_applied = NOW + timedelta(days=-(WINDOW + 5))

    before_ts = ts_applied - timedelta(days=window_days // 2)
    after_ts = ts_applied + timedelta(days=window_days // 2)

    examples = []
    for _ in range(examples_before):
        examples.append({"text": representative, "seen": before_ts.isoformat()})
    for _ in range(examples_after):
        examples.append({"text": representative, "seen": after_ts.isoformat()})

    return {
        "id": cluster_id,
        "project": project,
        "representative": representative,
        "examples": examples,
        "first_seen": before_ts.isoformat() if examples else ts_applied.isoformat(),
        "last_seen": after_ts.isoformat() if examples else ts_applied.isoformat(),
        "status": "open",
    }


def _write_ledger(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "applied-ledger.jsonl"
    lines = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)
    p.write_text(lines + "\n" if lines else "", encoding="utf-8")
    return p


def _write_state(tmp_path: Path, clusters: list[dict]) -> Path:
    p = tmp_path / "incidents-state.json"
    p.write_text(json.dumps({"clusters": clusters}, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. _parse_iso — tolerant timestamp parsing
# ---------------------------------------------------------------------------

class TestParseIso:
    def test_valid_utc(self):
        from ab_eval import _parse_iso
        dt = _parse_iso("2026-06-10T12:00:00+00:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_naive_gets_utc_attached(self):
        from ab_eval import _parse_iso
        dt = _parse_iso("2026-06-10T12:00:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_empty_string_returns_none(self):
        from ab_eval import _parse_iso
        assert _parse_iso("") is None

    def test_garbage_returns_none(self):
        from ab_eval import _parse_iso
        assert _parse_iso("not-a-date") is None


# ---------------------------------------------------------------------------
# 2. _project_from_target_file
# ---------------------------------------------------------------------------

class TestProjectFromTargetFile:
    def test_extracts_parent_dir(self):
        from ab_eval import _project_from_target_file
        result = _project_from_target_file("J:/Projects/MyProject/CLAUDE.md")
        assert result == "myproject"

    def test_empty_string(self):
        from ab_eval import _project_from_target_file
        assert _project_from_target_file("") == ""


# ---------------------------------------------------------------------------
# 2b. _project_from_entry — backfilled non-path target_file fallback
# ---------------------------------------------------------------------------

class TestProjectFromEntry:
    def test_slug_in_item_id_wins_over_path(self):
        """Real auto-applied rows carry BOTH a cwd slug in item_id and a real
        path in target_file. The slug must win: the path yields a directory
        basename ("facturka.bg") that never prefix-matches the slug-form
        cluster keys, starving the scoped pass for 100% of real rows."""
        from ab_eval import _project_from_entry
        entry = {
            "item_id": "auto-boris-j--Antigraviti-Facturka-bg",
            "target_file": "{{CODE_PATH}}\\Facturka.bg\\CLAUDE.md",
        }
        assert _project_from_entry(entry) == "j--antigraviti-facturka-bg"

    def test_real_path_used_when_item_id_has_no_slug(self):
        """No cwd slug in item_id (e.g. real habit rows) -> parent-dir fallback."""
        from ab_eval import _project_from_entry
        entry = {
            "item_id": "auto-habit-grep-read-edit",
            "target_file": "J:/Projects/MyProject/CLAUDE.md",
        }
        assert _project_from_entry(entry) == "myproject"

    def test_backfilled_target_file_falls_back_to_item_id(self):
        """target_file is a non-path placeholder -> derive project from item_id.

        Regression test for the bug: 3 real ledger rows have
        target_file="(backfilled from review-verdicts 2026-06-11)" (no path
        separator), which previously made the project empty and starved the
        project-scoped cluster match.
        """
        from ab_eval import _project_from_entry
        entry = {
            "item_id": "boris-j--antigraviti-facturka-bg",
            "target_file": "(backfilled from review-verdicts 2026-06-11)",
        }
        result = _project_from_entry(entry)
        assert result != ""
        assert result == "j--antigraviti-facturka-bg"

    def test_habit_style_id_with_routine_suffix(self):
        """habit- prefixed id keeps the routine suffix in the derived project."""
        from ab_eval import _project_from_entry
        entry = {
            "item_id": "habit-j--antigraviti-higgsfield-ai-powershell-read",
            "target_file": "(backfilled from review-verdicts 2026-06-11)",
        }
        result = _project_from_entry(entry)
        assert result == "j--antigraviti-higgsfield-ai-powershell-read"

    def test_no_dash_in_item_id_returns_empty(self):
        from ab_eval import _project_from_entry
        entry = {"item_id": "noprefix", "target_file": "not-a-path-either"}
        assert _project_from_entry(entry) == ""

    def test_empty_target_file_and_item_id(self):
        from ab_eval import _project_from_entry
        assert _project_from_entry({"item_id": "", "target_file": ""}) == ""


# ---------------------------------------------------------------------------
# 3. load_ledger_tolerant
# ---------------------------------------------------------------------------

class TestLoadLedgerTolerant:
    def test_empty_file_returns_empty_list(self, tmp_path):
        from ab_eval import load_ledger_tolerant
        p = tmp_path / "ledger.jsonl"
        p.write_text("", encoding="utf-8")
        assert load_ledger_tolerant(p) == []

    def test_missing_file_returns_empty_list(self, tmp_path):
        from ab_eval import load_ledger_tolerant
        assert load_ledger_tolerant(tmp_path / "nonexistent.jsonl") == []

    def test_corrupt_lines_skipped(self, tmp_path):
        from ab_eval import load_ledger_tolerant
        p = tmp_path / "ledger.jsonl"
        p.write_text(
            '{"item_id":"a"}\nNOT JSON\n{"item_id":"b"}\n',
            encoding="utf-8",
        )
        entries = load_ledger_tolerant(p)
        assert len(entries) == 2
        assert entries[0]["item_id"] == "a"
        assert entries[1]["item_id"] == "b"

    def test_valid_entries_parsed(self, tmp_path):
        from ab_eval import load_ledger_tolerant
        entry = {"item_id": "x", "ts": "2026-06-10T12:00:00+00:00"}
        p = tmp_path / "ledger.jsonl"
        p.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        result = load_ledger_tolerant(p)
        assert result == [entry]


# ---------------------------------------------------------------------------
# 4. _complaints_in_window
# ---------------------------------------------------------------------------

class TestComplaintsInWindow:
    def _cluster_with_times(self, times: list[datetime]) -> dict:
        return {
            "examples": [{"text": "x", "seen": t.isoformat()} for t in times],
        }

    def test_all_before_window_excluded(self):
        from ab_eval import _complaints_in_window
        t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        cluster = self._cluster_with_times([t0 - timedelta(days=1)])
        result = _complaints_in_window(cluster, t0, t0 + timedelta(days=7))
        assert result == []

    def test_all_after_window_excluded(self):
        from ab_eval import _complaints_in_window
        t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        cluster = self._cluster_with_times([t0 + timedelta(days=8)])
        result = _complaints_in_window(cluster, t0, t0 + timedelta(days=7))
        assert result == []

    def test_in_window_included(self):
        from ab_eval import _complaints_in_window
        t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        inside = t0 + timedelta(days=3)
        cluster = self._cluster_with_times([inside])
        result = _complaints_in_window(cluster, t0, t0 + timedelta(days=7))
        assert len(result) == 1

    def test_boundary_start_inclusive(self):
        from ab_eval import _complaints_in_window
        t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        cluster = self._cluster_with_times([t0])  # exactly at start
        result = _complaints_in_window(cluster, t0, t0 + timedelta(days=7))
        assert len(result) == 1

    def test_boundary_end_exclusive(self):
        from ab_eval import _complaints_in_window
        t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        cluster = self._cluster_with_times([t0 + timedelta(days=7)])  # exactly at end
        result = _complaints_in_window(cluster, t0, t0 + timedelta(days=7))
        assert result == []  # exclusive upper bound

    def test_corrupt_seen_skipped(self):
        from ab_eval import _complaints_in_window
        t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        cluster = {"examples": [{"text": "x", "seen": "not-a-date"}]}
        result = _complaints_in_window(cluster, t0, t0 + timedelta(days=7))
        assert result == []


# ---------------------------------------------------------------------------
# 5. evaluate_rule — verdict logic
# ---------------------------------------------------------------------------

class TestEvaluateRule:
    """Core verdict tests with injected time and minimal clusters."""

    def _entry(self, days_ago: float, project: str = "testproject") -> dict:
        t_applied = NOW - timedelta(days=days_ago)
        return {
            "item_id": f"auto-boris-{project}",
            "item_type": "boris_rule",
            "tier": 1,
            "target_file": f"J:/Projects/{project}/CLAUDE.md",
            "rollback_marker": f"auto-boris-{project}",
            "rule_preview": "always confirm before deleting files",
            "ts": t_applied.isoformat(),
            "rolled_back": False,
        }

    def _cluster(
        self,
        project: str = "testproject",
        n_before: int = 0,
        n_after: int = 0,
        days_ago: float = WINDOW + 5,
    ) -> dict:
        t_applied = NOW - timedelta(days=days_ago)
        return _make_cluster(
            project=project,
            representative="files keep getting deleted without confirmation",
            examples_before=n_before,
            examples_after=n_after,
            ts_applied=t_applied,
            window_days=WINDOW,
        )

    def test_effective_verdict(self):
        """High before-rate, low after-rate -> effective."""
        from ab_eval import evaluate_rule
        entry = self._entry(days_ago=WINDOW + 5)
        cluster = self._cluster(n_before=8, n_after=2)
        result = evaluate_rule(entry, [cluster], window_days=WINDOW, now=NOW)
        assert result["verdict"] == "effective"
        assert result["n"] == 10
        assert result["confidence"] == "normal"

    def test_no_effect_verdict(self):
        """Similar before and after rates -> no_effect."""
        from ab_eval import evaluate_rule
        entry = self._entry(days_ago=WINDOW + 5)
        cluster = self._cluster(n_before=5, n_after=4)
        result = evaluate_rule(entry, [cluster], window_days=WINDOW, now=NOW)
        assert result["verdict"] == "no_effect"

    def test_regressed_verdict(self):
        """After-rate > before-rate -> regressed."""
        from ab_eval import evaluate_rule
        entry = self._entry(days_ago=WINDOW + 5)
        cluster = self._cluster(n_before=2, n_after=8)
        result = evaluate_rule(entry, [cluster], window_days=WINDOW, now=NOW)
        assert result["verdict"] == "regressed"

    def test_pending_when_window_not_closed(self):
        """Rule applied only 5 days ago (< window_days=21) -> pending."""
        from ab_eval import evaluate_rule
        entry = self._entry(days_ago=5)  # too recent
        cluster = self._cluster(n_before=5, n_after=5, days_ago=5)
        result = evaluate_rule(entry, [cluster], window_days=WINDOW, now=NOW)
        assert result["verdict"] == "pending"

    def test_insufficient_data_when_n_lt_4(self):
        """n < 4 -> insufficient_data regardless of rates."""
        from ab_eval import evaluate_rule
        entry = self._entry(days_ago=WINDOW + 5)
        cluster = self._cluster(n_before=3, n_after=0)
        result = evaluate_rule(entry, [cluster], window_days=WINDOW, now=NOW)
        assert result["verdict"] == "insufficient_data"
        assert result["n"] == 3
        assert result["confidence"] == "low"

    def test_insufficient_data_at_exactly_3(self):
        """n=3 is still insufficient (boundary case)."""
        from ab_eval import evaluate_rule
        entry = self._entry(days_ago=WINDOW + 5)
        cluster = self._cluster(n_before=2, n_after=1)
        result = evaluate_rule(entry, [cluster], window_days=WINDOW, now=NOW)
        assert result["verdict"] == "insufficient_data"

    def test_sufficient_at_exactly_4(self):
        """n=4 is the minimum for a decided verdict."""
        from ab_eval import evaluate_rule
        entry = self._entry(days_ago=WINDOW + 5)
        cluster = self._cluster(n_before=4, n_after=0)
        result = evaluate_rule(entry, [cluster], window_days=WINDOW, now=NOW)
        # n=4 with 0 after -> effective
        assert result["verdict"] == "effective"
        assert result["confidence"] == "normal"

    def test_no_matching_cluster_gives_insufficient_data(self):
        """A rule with no matching cluster -> insufficient_data."""
        from ab_eval import evaluate_rule
        entry = self._entry(days_ago=WINDOW + 5)
        result = evaluate_rule(entry, [], window_days=WINDOW, now=NOW)
        assert result["verdict"] == "insufficient_data"
        assert result["cluster_id"] is None

    def test_bad_ts_gives_insufficient_data(self):
        """Unparseable ts -> insufficient_data (no crash)."""
        from ab_eval import evaluate_rule
        entry = {
            "item_id": "bad-ts",
            "item_type": "boris_rule",
            "ts": "GARBAGE",
            "target_file": "J:/Projects/Foo/CLAUDE.md",
            "rule_preview": "do something",
        }
        result = evaluate_rule(entry, [], window_days=WINDOW, now=NOW)
        assert result["verdict"] == "insufficient_data"

    def test_before_rate_math(self):
        """Verify rates are complaints / window_days (rounded to 4 d.p.)."""
        from ab_eval import evaluate_rule
        # 14 before, 0 after, window=21
        entry = self._entry(days_ago=WINDOW + 5)
        cluster = self._cluster(n_before=14, n_after=0)
        result = evaluate_rule(entry, [cluster], window_days=WINDOW, now=NOW)
        # Rates are stored rounded to 4 decimal places; tolerance matches rounding
        assert abs(result["before_rate"] - 14 / WINDOW) < 1e-4
        assert result["after_rate"] == 0.0

    def test_after_rate_math(self):
        """Verify after-rate is computed correctly (rounded to 4 d.p.)."""
        from ab_eval import evaluate_rule
        entry = self._entry(days_ago=WINDOW + 5)
        cluster = self._cluster(n_before=0, n_after=7)
        result = evaluate_rule(entry, [cluster], window_days=WINDOW, now=NOW)
        assert abs(result["after_rate"] - 7 / WINDOW) < 1e-4

    def test_effective_exact_boundary(self):
        """Exactly 50% reduction -> effective (boundary condition)."""
        from ab_eval import evaluate_rule
        entry = self._entry(days_ago=WINDOW + 5)
        # 8 before, 4 after: after_rate = 0.5 * before_rate exactly -> effective
        cluster = self._cluster(n_before=8, n_after=4)
        result = evaluate_rule(entry, [cluster], window_days=WINDOW, now=NOW)
        assert result["verdict"] == "effective"

    def test_rolled_back_entry_excluded_by_run_eval(self, tmp_path):
        """rolled_back=True entries must be skipped in run_eval."""
        from ab_eval import run_eval
        entry = _make_ledger_entry(rolled_back=True)
        ledger_path = _write_ledger(tmp_path, [entry])
        state_path = _write_state(tmp_path, [])
        out_path = tmp_path / "ab-eval.json"
        result = run_eval(
            ledger_path=ledger_path,
            state_path=state_path,
            out_path=out_path,
            window_days=WINDOW,
            dry_run=True,
            now=NOW,
        )
        assert result["summary"]["total_rules"] == 0


# ---------------------------------------------------------------------------
# 6. compute_summary — portfolio aggregation
# ---------------------------------------------------------------------------

class TestComputeSummary:
    def _records(self, verdicts: list[str]) -> list[dict]:
        return [{"verdict": v} for v in verdicts]

    def test_all_effective(self):
        from ab_eval import compute_summary
        s = compute_summary(self._records(["effective", "effective"]))
        assert s["effective"] == 2
        assert s["effectiveness_pct"] == 100.0

    def test_mixed_verdicts(self):
        from ab_eval import compute_summary
        s = compute_summary(self._records(["effective", "no_effect", "regressed"]))
        assert s["decided"] == 3
        assert abs(s["effectiveness_pct"] - 33.3) < 0.2

    def test_pending_not_counted_in_decided(self):
        from ab_eval import compute_summary
        s = compute_summary(self._records(["pending", "pending"]))
        assert s["decided"] == 0
        assert s["effectiveness_pct"] is None

    def test_empty_records(self):
        from ab_eval import compute_summary
        s = compute_summary([])
        assert s["total_rules"] == 0
        assert s["effectiveness_pct"] is None

    def test_insufficient_data_not_counted_in_decided(self):
        from ab_eval import compute_summary
        s = compute_summary(self._records(["insufficient_data", "effective"]))
        assert s["decided"] == 1
        assert s["effectiveness_pct"] == 100.0


# ---------------------------------------------------------------------------
# 7. run_eval — integration & edge cases
# ---------------------------------------------------------------------------

class TestRunEval:
    def test_empty_ledger_no_crash(self, tmp_path):
        """Empty ledger must not crash and produce valid output."""
        from ab_eval import run_eval
        ledger_path = _write_ledger(tmp_path, [])
        state_path = _write_state(tmp_path, [])
        out_path = tmp_path / "ab-eval.json"
        result = run_eval(
            ledger_path=ledger_path,
            state_path=state_path,
            out_path=out_path,
            window_days=WINDOW,
            now=NOW,
        )
        assert result["summary"]["total_rules"] == 0
        assert out_path.exists()

    def test_missing_ledger_no_crash(self, tmp_path):
        """Missing ledger file must not crash."""
        from ab_eval import run_eval
        state_path = _write_state(tmp_path, [])
        result = run_eval(
            ledger_path=tmp_path / "nonexistent.jsonl",
            state_path=state_path,
            out_path=tmp_path / "ab-eval.json",
            window_days=WINDOW,
            now=NOW,
        )
        assert result["summary"]["total_rules"] == 0

    def test_corrupt_state_no_crash(self, tmp_path):
        """Corrupt incidents-state.json must degrade gracefully."""
        from ab_eval import run_eval
        (tmp_path / "incidents-state.json").write_text("{INVALID JSON", encoding="utf-8")
        entry = _make_ledger_entry()
        ledger_path = _write_ledger(tmp_path, [entry])
        result = run_eval(
            ledger_path=ledger_path,
            state_path=tmp_path / "incidents-state.json",
            out_path=tmp_path / "ab-eval.json",
            window_days=WINDOW,
            now=NOW,
        )
        # Should not raise; rule will be insufficient_data or pending
        assert result["summary"]["total_rules"] == 1

    def test_dry_run_does_not_write(self, tmp_path):
        """dry_run=True must not write the output file."""
        from ab_eval import run_eval
        out_path = tmp_path / "ab-eval.json"
        run_eval(
            ledger_path=tmp_path / "nonexistent.jsonl",
            state_path=tmp_path / "nonexistent-state.json",
            out_path=out_path,
            window_days=WINDOW,
            dry_run=True,
            now=NOW,
        )
        assert not out_path.exists()

    def test_output_file_contains_required_keys(self, tmp_path):
        """Written JSON must have caveats, summary, rules, window_days."""
        from ab_eval import run_eval
        out_path = tmp_path / "ab-eval.json"
        run_eval(
            ledger_path=tmp_path / "nonexistent.jsonl",
            state_path=tmp_path / "nonexistent-state.json",
            out_path=out_path,
            window_days=WINDOW,
            now=NOW,
        )
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert "caveats" in data
        assert "summary" in data
        assert "rules" in data
        assert "window_days" in data
        assert isinstance(data["caveats"], list)
        assert len(data["caveats"]) >= 3

    def test_effective_rule_end_to_end(self, tmp_path):
        """Full pipeline: ledger entry with matching cluster -> effective."""
        from ab_eval import run_eval
        t_applied = NOW - timedelta(days=WINDOW + 5)
        entry = {
            "item_id": "auto-boris-testproject",
            "item_type": "boris_rule",
            "tier": 1,
            "target_file": "J:/Projects/TestProject/CLAUDE.md",
            "rollback_marker": "auto-boris-testproject",
            "rule_preview": "always confirm before deleting files",
            "ts": t_applied.isoformat(),
            "rolled_back": False,
        }
        cluster = _make_cluster(
            project="testproject",
            representative="files keep getting deleted without confirmation",
            examples_before=8,
            examples_after=2,
            ts_applied=t_applied,
            window_days=WINDOW,
        )
        ledger_path = _write_ledger(tmp_path, [entry])
        state_path = _write_state(tmp_path, [cluster])
        out_path = tmp_path / "ab-eval.json"
        result = run_eval(
            ledger_path=ledger_path,
            state_path=state_path,
            out_path=out_path,
            window_days=WINDOW,
            now=NOW,
        )
        assert result["summary"]["effective"] == 1
        assert result["rules"][0]["verdict"] == "effective"

    def test_pending_rule_end_to_end(self, tmp_path):
        """Rule applied recently (< window_days ago) -> pending."""
        from ab_eval import run_eval
        t_applied = NOW - timedelta(days=5)  # only 5 days ago, window=21
        entry = {
            "item_id": "auto-boris-testproject",
            "item_type": "boris_rule",
            "tier": 1,
            "target_file": "J:/Projects/TestProject/CLAUDE.md",
            "rollback_marker": "auto-boris-testproject",
            "rule_preview": "always confirm",
            "ts": t_applied.isoformat(),
            "rolled_back": False,
        }
        ledger_path = _write_ledger(tmp_path, [entry])
        state_path = _write_state(tmp_path, [])
        result = run_eval(
            ledger_path=ledger_path,
            state_path=state_path,
            out_path=tmp_path / "ab-eval.json",
            window_days=WINDOW,
            now=NOW,
        )
        assert result["summary"]["pending"] == 1

    def test_output_atomic_write(self, tmp_path):
        """Atomic write: file must be valid JSON even if inspected mid-run."""
        from ab_eval import run_eval
        out_path = tmp_path / "ab-eval.json"
        run_eval(
            ledger_path=tmp_path / "nonexistent.jsonl",
            state_path=tmp_path / "nonexistent-state.json",
            out_path=out_path,
            window_days=WINDOW,
            now=NOW,
        )
        # Must be parseable
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# 8. find_matching_cluster
# ---------------------------------------------------------------------------

class TestFindMatchingCluster:
    def test_returns_none_for_empty_clusters(self):
        from ab_eval import find_matching_cluster
        assert find_matching_cluster("proj", ["delete", "file"], []) is None

    def test_matches_same_project_over_other(self):
        from ab_eval import find_matching_cluster, normalize
        rule_tokens = normalize("always confirm before deleting files")
        c1 = {
            "id": "c1", "project": "projecta",
            "representative": "files deleted without confirm",
            "examples": [],
        }
        c2 = {
            "id": "c2", "project": "projectb",
            "representative": "files deleted without confirm",
            "examples": [],
        }
        result = find_matching_cluster("projecta", rule_tokens, [c1, c2])
        # Should prefer same-project cluster
        assert result is not None
        assert result["id"] == "c1"

    def test_falls_back_to_cross_project_if_no_same_project(self):
        from ab_eval import find_matching_cluster, normalize
        rule_tokens = normalize("always confirm before deleting files")
        c = {
            "id": "cx", "project": "otherproject",
            "representative": "files deleted without confirm",
            "examples": [],
        }
        result = find_matching_cluster("unknownproject", rule_tokens, [c])
        # Low threshold (0.15) should still match
        assert result is not None

    def test_habit_routine_suffix_scoped_matches_own_project_not_higher_sim_other(self):
        """habit-style item_id-derived project (with routine suffix) must
        prefix-match its own project's cluster in the scoped pass, even when a
        DIFFERENT project's cluster has higher raw text similarity.

        Regression test for the bug: rule_project derived from item_id like
        "j--antigraviti-higgsfield-ai-powershell-read" never equals the
        cluster's plain project key "j--antigraviti-higgsfield-ai" under exact
        matching, so the scoped pass used to fail and fall through to the
        cross-project pass, letting a higher-similarity sibling project
        (e.g. DCTL) win instead.
        """
        from ab_eval import find_matching_cluster, normalize
        rule_project = "j--antigraviti-higgsfield-ai-powershell-read"
        rule_tokens = normalize("use powershell read tool instead of cat")

        own_cluster = {
            "id": "own-cluster",
            "project": "j--antigraviti-higgsfield-ai",
            "representative": "read tool call failed on windows path",
            "examples": [],
        }
        other_cluster_higher_sim = {
            "id": "other-cluster",
            "project": "j--antigraviti-davinci-plugin-dctl",
            "representative": "use powershell read tool instead of cat command",
            "examples": [],
        }

        result = find_matching_cluster(
            rule_project, rule_tokens, [other_cluster_higher_sim, own_cluster]
        )
        assert result is not None
        assert result["id"] == "own-cluster"

    def test_scoped_prefix_match_requires_nonempty_cluster_project(self):
        """An empty cluster project key must never satisfy the scoped pass,
        even though "" is technically a prefix-match-adjacent case."""
        from ab_eval import find_matching_cluster, normalize
        rule_tokens = normalize("always confirm before deleting files")
        c_no_project = {
            "id": "cnone", "project": "",
            "representative": "files deleted without confirm",
            "examples": [],
        }
        c_real = {
            "id": "creal", "project": "realproject",
            "representative": "files deleted without confirm",
            "examples": [],
        }
        result = find_matching_cluster("realproject", rule_tokens, [c_no_project, c_real])
        assert result is not None
        assert result["id"] == "creal"
