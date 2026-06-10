"""Tests for habit_to_skill.py — TDD spec."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def test_slugify_routine():
    from habit_to_skill import slugify_routine

    assert slugify_routine("P", ["Edit", "Bash:python"]) == "edit-python"
    assert slugify_routine("P", ["Bash:grep", "Read", "Edit"]) == "grep-read-edit"


def test_generate_skill_md_contains_required():
    from habit_to_skill import generate_skill_md

    md = generate_skill_md(
        {
            "project": "Proj",
            "routine": ["Bash:pytest", "Edit"],
            "count": 10,
            "distinctiveness": 300.0,
            "reward_ratio": 0.9,
        }
    )
    assert "PENDING" in md
    assert "Bash:pytest" in md or "pytest" in md
    assert "Proj" in md


def test_write_skill_drafts_only_suggested(tmp_path):
    from habit_to_skill import write_skill_drafts

    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "Proj|Bash:pytest>Edit": {"status": "suggested_skill"},
                "Proj|Read>Edit": {"status": "detected"},
            }
        ),
        encoding="utf-8",
    )
    habits = tmp_path / "habits.json"
    habits.write_text(
        json.dumps(
            [
                {
                    "project": "Proj",
                    "routine": ["Bash:pytest", "Edit"],
                    "count": 10,
                    "distinctiveness": 300.0,
                    "reward_ratio": 0.9,
                },
                {
                    "project": "Proj",
                    "routine": ["Read", "Edit"],
                    "count": 99,
                    "distinctiveness": 10.0,
                    "reward_ratio": 1.0,
                },
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "drafts"
    written = write_skill_drafts(ledger, habits, out)
    assert len(written) == 1
    assert written[0].name == "SKILL.md"
    assert "pytest" in written[0].parent.name


def test_write_skill_drafts_missing_files(tmp_path):
    from habit_to_skill import write_skill_drafts

    written = write_skill_drafts(tmp_path / "no.json", tmp_path / "no2.json", tmp_path / "o")
    assert written == []
