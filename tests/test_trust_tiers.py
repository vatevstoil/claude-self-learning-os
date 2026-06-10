"""Tests for trust_tiers.py — graduated trust for auto-applying drafts."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_eff(tmp_path: Path, precision: dict, samples: dict) -> Path:
    p = tmp_path / "effectiveness.json"
    p.write_text(
        json.dumps({"precision_by_type": precision, "samples_by_type": samples}),
        encoding="utf-8",
    )
    return p


def _write_overrides(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "overrides.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _ledger(tmp_path: Path) -> Path:
    return tmp_path / "ledger.jsonl"


# ---------------------------------------------------------------------------
# get_tier — basic policy
# ---------------------------------------------------------------------------


def test_tier0_missing_data(tmp_path):
    from trust_tiers import get_tier

    eff = tmp_path / "missing.json"
    over = tmp_path / "missing2.json"
    assert get_tier("boris_rule", effectiveness_path=eff, overrides_path=over) == 0


def test_tier0_when_precision_below_threshold(tmp_path):
    from trust_tiers import get_tier

    eff = _write_eff(tmp_path, {"boris_rule": 0.64}, {"boris_rule": 30})
    assert get_tier("boris_rule", effectiveness_path=eff, overrides_path=tmp_path / "x.json") == 0


def test_tier1_when_precision_80_samples_10(tmp_path):
    from trust_tiers import get_tier

    eff = _write_eff(tmp_path, {"habit": 0.85}, {"habit": 15})
    assert get_tier("habit", effectiveness_path=eff, overrides_path=tmp_path / "x.json") == 1


def test_tier2_when_precision_92_samples_25(tmp_path):
    from trust_tiers import get_tier

    eff = _write_eff(tmp_path, {"habit": 0.95}, {"habit": 30})
    assert get_tier("habit", effectiveness_path=eff, overrides_path=tmp_path / "x.json") == 2


def test_tier2_boundary_exact(tmp_path):
    from trust_tiers import get_tier

    eff = _write_eff(tmp_path, {"habit": 0.92}, {"habit": 25})
    assert get_tier("habit", effectiveness_path=eff, overrides_path=tmp_path / "x.json") == 2


def test_tier1_boundary_samples_not_enough_for_tier2(tmp_path):
    from trust_tiers import get_tier

    eff = _write_eff(tmp_path, {"habit": 0.93}, {"habit": 24})
    assert get_tier("habit", effectiveness_path=eff, overrides_path=tmp_path / "x.json") == 1


def test_tier0_sufficient_precision_but_too_few_samples(tmp_path):
    from trust_tiers import get_tier

    eff = _write_eff(tmp_path, {"boris_rule": 0.95}, {"boris_rule": 5})
    assert get_tier("boris_rule", effectiveness_path=eff, overrides_path=tmp_path / "x.json") == 0


def test_tier0_missing_samples_key(tmp_path):
    from trust_tiers import get_tier

    # samples_by_type absent entirely — fail-closed
    eff = tmp_path / "eff.json"
    eff.write_text(json.dumps({"precision_by_type": {"boris_rule": 0.95}}), encoding="utf-8")
    assert get_tier("boris_rule", effectiveness_path=eff, overrides_path=tmp_path / "x.json") == 0


def test_tier0_corrupt_effectiveness(tmp_path):
    from trust_tiers import get_tier

    eff = tmp_path / "eff.json"
    eff.write_text("{broken json{{{", encoding="utf-8")
    assert get_tier("boris_rule", effectiveness_path=eff, overrides_path=tmp_path / "x.json") == 0


# ---------------------------------------------------------------------------
# get_tier — overrides
# ---------------------------------------------------------------------------


def test_override_forces_tier_up(tmp_path):
    from trust_tiers import get_tier

    # precision too low to earn tier 1 naturally
    eff = _write_eff(tmp_path, {"boris_rule": 0.30}, {"boris_rule": 5})
    over = _write_overrides(tmp_path, {"boris_rule": 1})
    assert get_tier("boris_rule", effectiveness_path=eff, overrides_path=over) == 1


def test_override_forces_tier_down(tmp_path):
    from trust_tiers import get_tier

    eff = _write_eff(tmp_path, {"habit": 0.99}, {"habit": 100})
    over = _write_overrides(tmp_path, {"habit": 0})
    assert get_tier("habit", effectiveness_path=eff, overrides_path=over) == 0


def test_override_clamps_to_2(tmp_path):
    from trust_tiers import get_tier

    eff = _write_eff(tmp_path, {}, {})
    over = _write_overrides(tmp_path, {"boris_rule": 99})
    assert get_tier("boris_rule", effectiveness_path=eff, overrides_path=over) == 2


def test_override_clamps_negative_to_0(tmp_path):
    from trust_tiers import get_tier

    eff = _write_eff(tmp_path, {}, {})
    over = _write_overrides(tmp_path, {"boris_rule": -5})
    assert get_tier("boris_rule", effectiveness_path=eff, overrides_path=over) == 0


def test_override_malformed_value_falls_through_to_computed(tmp_path):
    from trust_tiers import get_tier

    eff = _write_eff(tmp_path, {"boris_rule": 0.85}, {"boris_rule": 15})
    over = _write_overrides(tmp_path, {"boris_rule": "yes"})
    # malformed override → computed = 1
    assert get_tier("boris_rule", effectiveness_path=eff, overrides_path=over) == 1


def test_corrupt_overrides_falls_through_to_computed(tmp_path):
    from trust_tiers import get_tier

    eff = _write_eff(tmp_path, {"habit": 0.85}, {"habit": 15})
    over = tmp_path / "over.json"
    over.write_text("{BAD}", encoding="utf-8")
    assert get_tier("habit", effectiveness_path=eff, overrides_path=over) == 1


# ---------------------------------------------------------------------------
# record_application / load_ledger / is_in_ledger
# ---------------------------------------------------------------------------


def test_record_and_load_ledger(tmp_path):
    from trust_tiers import record_application, load_ledger

    ledger = _ledger(tmp_path)
    record_application(
        {"item_id": "x1", "item_type": "boris_rule", "tier": 1,
         "target_file": "/tmp/X.md", "rollback_marker": "x1"},
        ledger_path=ledger, now=NOW,
    )
    entries = load_ledger(ledger)
    assert len(entries) == 1
    assert entries[0]["item_id"] == "x1"
    assert entries[0]["ts"] == NOW.isoformat()
    assert entries[0]["rolled_back"] is False


def test_record_multiple_entries(tmp_path):
    from trust_tiers import record_application, load_ledger

    ledger = _ledger(tmp_path)
    for i in range(3):
        record_application(
            {"item_id": f"item-{i}", "item_type": "boris_rule", "tier": 1,
             "target_file": "/tmp/f.md", "rollback_marker": f"item-{i}"},
            ledger_path=ledger, now=NOW,
        )
    assert len(load_ledger(ledger)) == 3


def test_is_in_ledger_true_for_existing(tmp_path):
    from trust_tiers import record_application, is_in_ledger

    ledger = _ledger(tmp_path)
    record_application(
        {"item_id": "my-item", "item_type": "boris_rule", "tier": 1,
         "target_file": "/f", "rollback_marker": "my-item"},
        ledger_path=ledger, now=NOW,
    )
    assert is_in_ledger("my-item", ledger_path=ledger) is True


def test_is_in_ledger_false_for_missing(tmp_path):
    from trust_tiers import is_in_ledger

    assert is_in_ledger("ghost", ledger_path=_ledger(tmp_path)) is False


def test_is_in_ledger_false_after_rollback(tmp_path):
    from trust_tiers import record_application, rollback_last, is_in_ledger

    ledger = _ledger(tmp_path)
    # Dummy target file
    target = tmp_path / "CLAUDE.md"
    target.write_text("hello", encoding="utf-8")

    record_application(
        {"item_id": "r1", "item_type": "boris_rule", "tier": 1,
         "target_file": str(target), "rollback_marker": "r1"},
        ledger_path=ledger, now=NOW,
    )
    rollback_last(ledger_path=ledger, now=NOW)
    # After rollback the entry exists but is marked rolled_back → not "in ledger"
    assert is_in_ledger("r1", ledger_path=ledger) is False


# ---------------------------------------------------------------------------
# rollback_last
# ---------------------------------------------------------------------------


def test_rollback_removes_marker_block(tmp_path):
    from trust_tiers import record_application, rollback_last

    target = tmp_path / "CLAUDE.md"
    original = "# Rules\n\n## Learned Rules\n"
    item_id = "auto-boris-TestProj"
    block = (
        f"<!-- auto-applied:{item_id} tier:1 2026-06-10T12:00:00+00:00 -->\n"
        "- always run tests\n"
        f"<!-- /auto-applied:{item_id} -->\n"
    )
    target.write_text(original + block, encoding="utf-8")

    ledger = _ledger(tmp_path)
    record_application(
        {"item_id": item_id, "item_type": "boris_rule", "tier": 1,
         "target_file": str(target), "rollback_marker": item_id},
        ledger_path=ledger, now=NOW,
    )

    rolled = rollback_last(ledger_path=ledger, now=NOW)
    assert rolled is not None
    assert rolled["item_id"] == item_id
    assert target.read_text(encoding="utf-8") == original


def test_rollback_byte_identical_after_roundtrip(tmp_path):
    from trust_tiers import record_application, rollback_last

    target = tmp_path / "CLAUDE.md"
    original_text = "# Rules\n\n## Learned Rules\n"
    # Write via text mode (consistent with production code)
    target.write_text(original_text, encoding="utf-8")
    # Capture what was actually written (may differ on Windows due to CRLF)
    original_bytes = target.read_bytes()

    item_id = "auto-boris-Exact"
    block = (
        f"<!-- auto-applied:{item_id} tier:2 2026-06-10T12:00:00+00:00 -->\n"
        "- exact rule\n"
        f"<!-- /auto-applied:{item_id} -->\n"
    )
    # Add block using same text-mode write path as production code
    target.write_text(target.read_text(encoding="utf-8") + block, encoding="utf-8")

    ledger = _ledger(tmp_path)
    record_application(
        {"item_id": item_id, "item_type": "boris_rule", "tier": 2,
         "target_file": str(target), "rollback_marker": item_id},
        ledger_path=ledger, now=NOW,
    )
    rollback_last(ledger_path=ledger, now=NOW)
    # After rollback the file should be byte-identical to what was written originally
    assert target.read_bytes() == original_bytes


def test_rollback_returns_none_if_nothing_to_rollback(tmp_path):
    from trust_tiers import rollback_last

    assert rollback_last(ledger_path=_ledger(tmp_path)) is None


def test_rollback_filters_by_type(tmp_path):
    from trust_tiers import record_application, rollback_last, load_ledger

    target = tmp_path / "f.md"
    target.write_text("", encoding="utf-8")

    ledger = _ledger(tmp_path)
    record_application(
        {"item_id": "h1", "item_type": "habit", "tier": 1,
         "target_file": str(target), "rollback_marker": "h1"},
        ledger_path=ledger, now=NOW,
    )
    record_application(
        {"item_id": "b1", "item_type": "boris_rule", "tier": 1,
         "target_file": str(target), "rollback_marker": "b1"},
        ledger_path=ledger, now=NOW,
    )
    rolled = rollback_last(item_type="habit", ledger_path=ledger, now=NOW)
    assert rolled is not None
    assert rolled["item_id"] == "h1"
    # boris entry should still be active
    entries = load_ledger(ledger)
    boris_entry = next(e for e in entries if e["item_id"] == "b1")
    assert boris_entry["rolled_back"] is False


def test_rollback_skips_already_rolled_back(tmp_path):
    from trust_tiers import record_application, rollback_last

    target = tmp_path / "f.md"
    target.write_text("", encoding="utf-8")
    ledger = _ledger(tmp_path)

    record_application(
        {"item_id": "only-one", "item_type": "boris_rule", "tier": 1,
         "target_file": str(target), "rollback_marker": "only-one"},
        ledger_path=ledger, now=NOW,
    )
    rollback_last(ledger_path=ledger, now=NOW)  # first rollback
    result = rollback_last(ledger_path=ledger, now=NOW)  # second → nothing
    assert result is None


def test_rollback_missing_target_file_does_not_raise(tmp_path):
    from trust_tiers import record_application, rollback_last

    ledger = _ledger(tmp_path)
    record_application(
        {"item_id": "missing-target", "item_type": "boris_rule", "tier": 1,
         "target_file": str(tmp_path / "nonexistent.md"), "rollback_marker": "missing-target"},
        ledger_path=ledger, now=NOW,
    )
    # Should not raise even though the file doesn't exist
    result = rollback_last(ledger_path=ledger, now=NOW)
    assert result is not None


# ---------------------------------------------------------------------------
# append_pending_review
# ---------------------------------------------------------------------------


def test_append_pending_review_creates_file(tmp_path):
    from trust_tiers import append_pending_review

    p = tmp_path / "pending.json"
    append_pending_review({"item_id": "x", "type": "boris_rule"}, pending_path=p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert data[0]["item_id"] == "x"


def test_append_pending_review_accumulates(tmp_path):
    from trust_tiers import append_pending_review

    p = tmp_path / "pending.json"
    for i in range(3):
        append_pending_review({"item_id": f"item-{i}"}, pending_path=p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert len(data) == 3


def test_append_pending_review_tolerates_corrupt(tmp_path):
    from trust_tiers import append_pending_review

    p = tmp_path / "pending.json"
    p.write_text("{BAD JSON}", encoding="utf-8")
    # Should not raise — resets to new list
    append_pending_review({"item_id": "fresh"}, pending_path=p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data[-1]["item_id"] == "fresh"


# ---------------------------------------------------------------------------
# _remove_marked_block
# ---------------------------------------------------------------------------


def test_remove_marked_block_clean(tmp_path):
    from trust_tiers import _remove_marked_block

    item_id = "auto-boris-X"
    content = (
        "before\n"
        f"<!-- auto-applied:{item_id} tier:1 2026-01-01T00:00:00 -->\n"
        "- some rule\n"
        f"<!-- /auto-applied:{item_id} -->\n"
        "after\n"
    )
    result = _remove_marked_block(content, item_id)
    assert "some rule" not in result
    assert "before\n" in result
    assert "after\n" in result


def test_remove_marked_block_noop_if_not_found(tmp_path):
    from trust_tiers import _remove_marked_block

    content = "no markers here\n"
    assert _remove_marked_block(content, "ghost-id") == content


# ---------------------------------------------------------------------------
# boris_draft auto_apply_boris integration
# ---------------------------------------------------------------------------


def test_auto_apply_boris_tier0_skips(tmp_path):
    from boris_draft import auto_apply_boris

    eff = _write_eff(tmp_path, {"boris_rule": 0.30}, {"boris_rule": 3})
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    applied = auto_apply_boris(
        drafts_dir=drafts,
        ledger_path=_ledger(tmp_path),
        pending_review_path=tmp_path / "pending.json",
        effectiveness_path=eff,
        overrides_path=tmp_path / "nover.json",
        now=NOW,
    )
    assert applied == []


def test_auto_apply_boris_tier1_applies_rule(tmp_path):
    from boris_draft import auto_apply_boris, _safe_filename

    eff = _write_eff(tmp_path, {"boris_rule": 0.85}, {"boris_rule": 15})

    # Create a valid draft
    project_key = "X--TestOrg-MyProj"
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    draft_content = (
        "# Boris Draft\n\n"
        "## Proposed Rule\n\n"
        "```\nalways test before commit\n```\n\n"
        "## Target File\n\n`X:\\TestOrg\\MyProj\\CLAUDE.md`\n"
    )
    (drafts / (_safe_filename(project_key) + ".md")).write_text(draft_content, encoding="utf-8")

    # Create the target CLAUDE.md
    claude_dir = tmp_path / "resolved"
    claude_dir.mkdir()
    claude_md = claude_dir / "CLAUDE.md"
    claude_md.write_text("# Rules\n\n## Learned Rules\n", encoding="utf-8")

    # Monkey-patch path resolution
    import boris_draft as bd
    orig_encode = bd._encode_to_path_hint
    orig_resolve = bd._resolve_real_project_dir
    bd._encode_to_path_hint = lambda k: str(claude_dir)
    bd._resolve_real_project_dir = lambda p: claude_dir

    try:
        applied = auto_apply_boris(
            drafts_dir=drafts,
            ledger_path=_ledger(tmp_path),
            pending_review_path=tmp_path / "pending.json",
            effectiveness_path=eff,
            overrides_path=tmp_path / "nover.json",
            now=NOW,
        )
        assert len(applied) == 1
        content = claude_md.read_text(encoding="utf-8")
        assert "always test before commit" in content
        assert "<!-- auto-applied:" in content
    finally:
        bd._encode_to_path_hint = orig_encode
        bd._resolve_real_project_dir = orig_resolve


def test_auto_apply_boris_deduplication(tmp_path):
    from boris_draft import auto_apply_boris, _safe_filename

    eff = _write_eff(tmp_path, {"boris_rule": 0.85}, {"boris_rule": 15})

    project_key = "X--TestOrg-DedupProj"
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    draft_content = (
        "## Proposed Rule\n\n```\nsome rule\n```\n"
    )
    (drafts / (_safe_filename(project_key) + ".md")).write_text(draft_content, encoding="utf-8")

    claude_dir = tmp_path / "resolved2"
    claude_dir.mkdir()
    (claude_dir / "CLAUDE.md").write_text("# Rules\n\n## Learned Rules\n", encoding="utf-8")

    import boris_draft as bd
    orig_encode = bd._encode_to_path_hint
    orig_resolve = bd._resolve_real_project_dir
    bd._encode_to_path_hint = lambda k: str(claude_dir)
    bd._resolve_real_project_dir = lambda p: claude_dir

    try:
        ledger = _ledger(tmp_path)
        pending = tmp_path / "pending.json"
        applied1 = auto_apply_boris(
            drafts_dir=drafts, ledger_path=ledger,
            pending_review_path=pending,
            effectiveness_path=eff, overrides_path=tmp_path / "nover.json", now=NOW,
        )
        applied2 = auto_apply_boris(
            drafts_dir=drafts, ledger_path=ledger,
            pending_review_path=pending,
            effectiveness_path=eff, overrides_path=tmp_path / "nover.json", now=NOW,
        )
        assert len(applied1) == 1
        assert len(applied2) == 0  # already in ledger
    finally:
        bd._encode_to_path_hint = orig_encode
        bd._resolve_real_project_dir = orig_resolve


def test_auto_apply_boris_max_2_per_run(tmp_path):
    from boris_draft import auto_apply_boris, _safe_filename

    eff = _write_eff(tmp_path, {"boris_rule": 0.85}, {"boris_rule": 15})
    drafts = tmp_path / "drafts"
    drafts.mkdir()

    # Create 5 drafts
    for i in range(5):
        key = f"X--Org-Proj{i}"
        content = f"## Proposed Rule\n\n```\nrule {i}\n```\n"
        (drafts / (_safe_filename(key) + ".md")).write_text(content, encoding="utf-8")

    claude_dir = tmp_path / "resolved3"
    claude_dir.mkdir()
    (claude_dir / "CLAUDE.md").write_text("# Rules\n\n## Learned Rules\n", encoding="utf-8")

    import boris_draft as bd
    orig_encode = bd._encode_to_path_hint
    orig_resolve = bd._resolve_real_project_dir
    bd._encode_to_path_hint = lambda k: str(claude_dir)
    bd._resolve_real_project_dir = lambda p: claude_dir

    try:
        applied = auto_apply_boris(
            drafts_dir=drafts, ledger_path=_ledger(tmp_path),
            pending_review_path=tmp_path / "pending.json",
            effectiveness_path=eff, overrides_path=tmp_path / "nover.json", now=NOW,
        )
        assert len(applied) <= 2
    finally:
        bd._encode_to_path_hint = orig_encode
        bd._resolve_real_project_dir = orig_resolve


def test_auto_apply_boris_tier1_writes_pending_review(tmp_path):
    from boris_draft import auto_apply_boris, _safe_filename

    eff = _write_eff(tmp_path, {"boris_rule": 0.85}, {"boris_rule": 15})

    project_key = "X--Org-ReviewProj"
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    draft_content = "## Proposed Rule\n\n```\nreview rule\n```\n"
    (drafts / (_safe_filename(project_key) + ".md")).write_text(draft_content, encoding="utf-8")

    claude_dir = tmp_path / "rdir"
    claude_dir.mkdir()
    (claude_dir / "CLAUDE.md").write_text("# R\n\n## Learned Rules\n", encoding="utf-8")

    pending = tmp_path / "pending.json"

    import boris_draft as bd
    orig_encode = bd._encode_to_path_hint
    orig_resolve = bd._resolve_real_project_dir
    bd._encode_to_path_hint = lambda k: str(claude_dir)
    bd._resolve_real_project_dir = lambda p: claude_dir

    try:
        auto_apply_boris(
            drafts_dir=drafts,
            ledger_path=_ledger(tmp_path),
            pending_review_path=pending,
            effectiveness_path=eff,
            overrides_path=tmp_path / "nover.json",
            now=NOW,
        )
        data = json.loads(pending.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["type"] == "boris_rule"
    finally:
        bd._encode_to_path_hint = orig_encode
        bd._resolve_real_project_dir = orig_resolve


# ---------------------------------------------------------------------------
# habit_to_skill auto_apply_habits integration
# ---------------------------------------------------------------------------


def test_auto_apply_habits_tier0_skips(tmp_path):
    from habit_to_skill import auto_apply_habits

    eff = _write_eff(tmp_path, {"habit": 0.30}, {"habit": 3})
    installed = auto_apply_habits(
        drafts_dir=tmp_path / "drafts",
        skills_dir=tmp_path / "skills",
        ledger_path=_ledger(tmp_path),
        pending_review_path=tmp_path / "pending.json",
        effectiveness_path=eff,
        overrides_path=tmp_path / "nover.json",
        now=NOW,
    )
    assert installed == []


def test_auto_apply_habits_without_judge_json_skips(tmp_path):
    from habit_to_skill import auto_apply_habits

    eff = _write_eff(tmp_path, {"habit": 0.85}, {"habit": 15})
    drafts = tmp_path / "drafts"
    slug_dir = drafts / "edit-python"
    slug_dir.mkdir(parents=True)
    (slug_dir / "SKILL.md").write_text("---\nname: edit-python\n---\n", encoding="utf-8")
    # No judge.json → should not install

    installed = auto_apply_habits(
        drafts_dir=drafts,
        skills_dir=tmp_path / "skills",
        ledger_path=_ledger(tmp_path),
        pending_review_path=tmp_path / "pending.json",
        effectiveness_path=eff,
        overrides_path=tmp_path / "nover.json",
        now=NOW,
    )
    assert installed == []


def test_auto_apply_habits_with_useful_judge_installs(tmp_path):
    from habit_to_skill import auto_apply_habits

    eff = _write_eff(tmp_path, {"habit": 0.85}, {"habit": 15})
    drafts = tmp_path / "drafts"
    slug = "edit-python"
    slug_dir = drafts / slug
    slug_dir.mkdir(parents=True)
    (slug_dir / "SKILL.md").write_text("---\nname: edit-python\n---\ncontent\n", encoding="utf-8")
    (slug_dir / "judge.json").write_text(json.dumps({"verdict": "useful"}), encoding="utf-8")

    skills = tmp_path / "skills"
    installed = auto_apply_habits(
        drafts_dir=drafts,
        skills_dir=skills,
        ledger_path=_ledger(tmp_path),
        pending_review_path=tmp_path / "pending.json",
        effectiveness_path=eff,
        overrides_path=tmp_path / "nover.json",
        now=NOW,
    )
    assert len(installed) == 1
    dest = skills / slug / "SKILL.md"
    assert dest.exists()
    content = dest.read_text(encoding="utf-8")
    assert "<!-- auto-applied:" in content
    assert "content" in content


def test_auto_apply_habits_non_useful_verdict_skips(tmp_path):
    from habit_to_skill import auto_apply_habits

    eff = _write_eff(tmp_path, {"habit": 0.85}, {"habit": 15})
    drafts = tmp_path / "drafts"
    slug_dir = drafts / "my-skill"
    slug_dir.mkdir(parents=True)
    (slug_dir / "SKILL.md").write_text("content", encoding="utf-8")
    (slug_dir / "judge.json").write_text(json.dumps({"verdict": "not_useful"}), encoding="utf-8")

    installed = auto_apply_habits(
        drafts_dir=drafts,
        skills_dir=tmp_path / "skills",
        ledger_path=_ledger(tmp_path),
        pending_review_path=tmp_path / "pending.json",
        effectiveness_path=eff,
        overrides_path=tmp_path / "nover.json",
        now=NOW,
    )
    assert installed == []


def test_auto_apply_habits_max_1_per_run(tmp_path):
    from habit_to_skill import auto_apply_habits

    eff = _write_eff(tmp_path, {"habit": 0.85}, {"habit": 15})
    drafts = tmp_path / "drafts"
    for i in range(3):
        d = drafts / f"skill-{i}"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"skill {i}", encoding="utf-8")
        (d / "judge.json").write_text(json.dumps({"verdict": "useful"}), encoding="utf-8")

    installed = auto_apply_habits(
        drafts_dir=drafts,
        skills_dir=tmp_path / "skills",
        ledger_path=_ledger(tmp_path),
        pending_review_path=tmp_path / "pending.json",
        effectiveness_path=eff,
        overrides_path=tmp_path / "nover.json",
        now=NOW,
    )
    assert len(installed) == 1


def test_auto_apply_habits_deduplication(tmp_path):
    from habit_to_skill import auto_apply_habits

    eff = _write_eff(tmp_path, {"habit": 0.85}, {"habit": 15})
    drafts = tmp_path / "drafts"
    slug_dir = drafts / "my-dup"
    slug_dir.mkdir(parents=True)
    (slug_dir / "SKILL.md").write_text("content", encoding="utf-8")
    (slug_dir / "judge.json").write_text(json.dumps({"verdict": "useful"}), encoding="utf-8")

    skills = tmp_path / "skills"
    ledger = _ledger(tmp_path)
    kwargs = dict(
        drafts_dir=drafts, skills_dir=skills, ledger_path=ledger,
        pending_review_path=tmp_path / "pending.json",
        effectiveness_path=eff, overrides_path=tmp_path / "nover.json", now=NOW,
    )
    first = auto_apply_habits(**kwargs)
    second = auto_apply_habits(**kwargs)
    assert len(first) == 1
    assert len(second) == 0


def test_auto_apply_habits_tier1_writes_pending_review(tmp_path):
    from habit_to_skill import auto_apply_habits

    eff = _write_eff(tmp_path, {"habit": 0.85}, {"habit": 15})
    drafts = tmp_path / "drafts"
    slug_dir = drafts / "review-skill"
    slug_dir.mkdir(parents=True)
    (slug_dir / "SKILL.md").write_text("content", encoding="utf-8")
    (slug_dir / "judge.json").write_text(json.dumps({"verdict": "useful"}), encoding="utf-8")

    pending = tmp_path / "pending.json"
    auto_apply_habits(
        drafts_dir=drafts, skills_dir=tmp_path / "skills",
        ledger_path=_ledger(tmp_path), pending_review_path=pending,
        effectiveness_path=eff, overrides_path=tmp_path / "nover.json", now=NOW,
    )
    data = json.loads(pending.read_text(encoding="utf-8"))
    assert any(d["type"] == "habit" for d in data)


# ---------------------------------------------------------------------------
# habit rollback removes installed SKILL.md
# ---------------------------------------------------------------------------


def test_rollback_removes_installed_skill(tmp_path):
    from habit_to_skill import auto_apply_habits
    from trust_tiers import rollback_last

    eff = _write_eff(tmp_path, {"habit": 0.85}, {"habit": 15})
    drafts = tmp_path / "drafts"
    slug = "rollback-skill"
    slug_dir = drafts / slug
    slug_dir.mkdir(parents=True)
    original_content = "---\nname: rollback-skill\n---\nmy skill\n"
    (slug_dir / "SKILL.md").write_text(original_content, encoding="utf-8")
    (slug_dir / "judge.json").write_text(json.dumps({"verdict": "useful"}), encoding="utf-8")

    skills = tmp_path / "skills"
    ledger = _ledger(tmp_path)
    auto_apply_habits(
        drafts_dir=drafts, skills_dir=skills, ledger_path=ledger,
        pending_review_path=tmp_path / "pending.json",
        effectiveness_path=eff, overrides_path=tmp_path / "nover.json", now=NOW,
    )
    installed_file = skills / slug / "SKILL.md"
    assert installed_file.exists()
    assert "<!-- auto-applied:" in installed_file.read_text(encoding="utf-8")

    rollback_last(item_type="habit", ledger_path=ledger, now=NOW)
    # After rollback the marker block is removed (file exists but markers gone)
    remaining = installed_file.read_text(encoding="utf-8")
    assert "<!-- auto-applied:" not in remaining


# ---------------------------------------------------------------------------
# effectiveness_tracker samples_by_type
# ---------------------------------------------------------------------------


def test_samples_by_type_counts_unique_items(tmp_path):
    from effectiveness_tracker import samples_by_type

    history = [
        {"ts": "T1", "items": [
            {"type": "habit", "id": "h1", "project": "P", "score": 1.0},
            {"type": "boris_rule", "id": "b1", "project": "P", "score": 1.0},
        ]},
        {"ts": "T2", "items": [
            {"type": "habit", "id": "h1", "project": "P", "score": 1.0},  # duplicate
            {"type": "habit", "id": "h2", "project": "P", "score": 1.0},
        ]},
    ]
    result = samples_by_type(history)
    assert result["habit"] == 2   # h1 and h2 (h1 deduplicated)
    assert result["boris_rule"] == 1


def test_samples_by_type_empty_history():
    from effectiveness_tracker import samples_by_type

    assert samples_by_type([]) == {}


def test_effectiveness_main_writes_samples_by_type(tmp_path):
    """After running main(), effectiveness.json must contain samples_by_type."""
    import effectiveness_tracker as et

    # Point all paths to tmp_path
    orig_queue = et.QUEUE_PATH
    orig_history = et.HISTORY_PATH
    orig_ledger = et.LEDGER_PATH
    orig_skill = et.SKILL_DRAFTS_DIR
    orig_boris = et.BORIS_DRAFTS_DIR
    orig_eff = et.EFFECTIVENESS_PATH
    orig_thresh = et.THRESHOLDS_PATH
    orig_ant = et._DEFAULT_ANTICIPATIONS
    orig_habits = et._DEFAULT_HABITS

    et.QUEUE_PATH = tmp_path / "queue.json"
    et.HISTORY_PATH = tmp_path / "history.jsonl"
    et.LEDGER_PATH = tmp_path / "ledger.json"
    et.SKILL_DRAFTS_DIR = tmp_path / "skill-drafts"
    et.BORIS_DRAFTS_DIR = tmp_path / "boris-drafts"
    et.EFFECTIVENESS_PATH = tmp_path / "effectiveness.json"
    et.THRESHOLDS_PATH = tmp_path / "thresholds.json"
    et._DEFAULT_ANTICIPATIONS = tmp_path / "ant.json"
    et._DEFAULT_HABITS = tmp_path / "habits.json"

    try:
        et.main()
        data = json.loads((tmp_path / "effectiveness.json").read_text(encoding="utf-8"))
        assert "samples_by_type" in data
        assert isinstance(data["samples_by_type"], dict)
        # Old keys still present (backwards compat)
        assert "precision_by_type" in data
        assert "total_snapshots" in data
    finally:
        et.QUEUE_PATH = orig_queue
        et.HISTORY_PATH = orig_history
        et.LEDGER_PATH = orig_ledger
        et.SKILL_DRAFTS_DIR = orig_skill
        et.BORIS_DRAFTS_DIR = orig_boris
        et.EFFECTIVENESS_PATH = orig_eff
        et.THRESHOLDS_PATH = orig_thresh
        et._DEFAULT_ANTICIPATIONS = orig_ant
        et._DEFAULT_HABITS = orig_habits


# ---------------------------------------------------------------------------
# Signature contract — no default-path leak possible
# ---------------------------------------------------------------------------


def test_no_default_path_leak(tmp_path):
    """Both auto_apply_* functions must accept pending_review_path kwarg.

    This test is the contract that ensures callers CAN always redirect the
    pending-review write to a tmp location (preventing ~/.claude/logs/ pollution
    during tests).  It does NOT call the real functions with production paths —
    it verifies the parameter exists in the signature and that passing it to a
    no-op invocation works without TypeError.
    """
    import inspect
    from boris_draft import auto_apply_boris
    from habit_to_skill import auto_apply_habits

    # 1. Signature check — the kwarg must be declared
    boris_params = inspect.signature(auto_apply_boris).parameters
    habit_params = inspect.signature(auto_apply_habits).parameters
    assert "pending_review_path" in boris_params, (
        "auto_apply_boris is missing pending_review_path parameter"
    )
    assert "pending_review_path" in habit_params, (
        "auto_apply_habits is missing pending_review_path parameter"
    )

    # 2. Functional check — passing tmp paths must not raise TypeError
    eff = _write_eff(tmp_path, {"boris_rule": 0.10}, {"boris_rule": 1})
    # Tier 0 → functions return immediately; no filesystem writes at all
    result_boris = auto_apply_boris(
        drafts_dir=tmp_path / "drafts",
        ledger_path=tmp_path / "ledger.jsonl",
        pending_review_path=tmp_path / "pending-boris.json",
        effectiveness_path=eff,
        overrides_path=tmp_path / "nover.json",
        now=NOW,
    )
    assert result_boris == []

    eff2 = _write_eff(tmp_path, {"habit": 0.10}, {"habit": 1})
    result_habits = auto_apply_habits(
        drafts_dir=tmp_path / "drafts",
        skills_dir=tmp_path / "skills",
        ledger_path=tmp_path / "ledger2.jsonl",
        pending_review_path=tmp_path / "pending-habits.json",
        effectiveness_path=eff2,
        overrides_path=tmp_path / "nover.json",
        now=NOW,
    )
    assert result_habits == []

    # 3. Verify nothing was written outside tmp_path
    real_pending = Path.home() / ".claude" / "logs" / "auto-applied-pending-review.json"
    # We cannot assert it does not exist (it might exist from production use),
    # but we can assert our test did not GROW it — the tier-0 path exits before
    # any write, so no entry with our NOW timestamp should be present.
    if real_pending.exists():
        try:
            entries = json.loads(real_pending.read_text(encoding="utf-8"))
            leaked = [e for e in entries if e.get("ts", "").startswith("2026-06-10T12:00:00")]
            assert leaked == [], f"Test leaked {len(leaked)} entries into real pending-review file"
        except (json.JSONDecodeError, TypeError):
            pass  # file corrupt or non-list — not our problem
