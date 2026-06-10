"""Tests for habit_ledger.py — TDD suite, written before implementation.

Run with: pytest {{HOME}}/.claude/tests/test_habit_ledger.py -v
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def _habit(project, routine, dist, reward=0.8, count=10, sess=4):
    """Build a minimal habit dict for test fixtures."""
    return {
        "project": project,
        "routine": list(routine),
        "count": count,
        "session_count": sess,
        "distinctiveness": dist,
        "reward_ratio": reward,
        "last_seen": "2026-05-25",
        "strength": 9.0,
        "status": "detected",
        "cue": project,
    }


# ---------------------------------------------------------------------------
# routine_key
# ---------------------------------------------------------------------------

def test_routine_key():
    from habit_ledger import routine_key
    assert routine_key("P", ["Bash:git", "Edit"]) == "P|Bash:git>Edit"


def test_routine_key_single_token():
    from habit_ledger import routine_key
    assert routine_key("MyProj", ["Read"]) == "MyProj|Read"


def test_routine_key_tuple_input():
    from habit_ledger import routine_key
    # routine can also arrive as a tuple (from HabitCandidate dataclass)
    assert routine_key("P", ("Bash:pytest", "Edit", "Bash:git")) == "P|Bash:pytest>Edit>Bash:git"


# ---------------------------------------------------------------------------
# Graduation: detected -> suggested_skill
# ---------------------------------------------------------------------------

def test_graduation_detected_to_suggested(tmp_path):
    """Second observation with high distinctiveness and reward should graduate."""
    from habit_ledger import update_ledger
    habits = [_habit("P", ("Bash:pytest", "Edit"), dist=300.0)]
    ledger = {
        "P|Bash:pytest>Edit": {
            "times_observed": 1,
            "status": "detected",
            "first_seen": "2026-05-01",
        }
    }
    out = update_ledger(habits, ledger, skills_dir=tmp_path)
    assert out["P|Bash:pytest>Edit"]["status"] == "suggested_skill"


def test_graduation_requires_reward_threshold(tmp_path):
    """High distinctiveness but low reward_ratio should NOT graduate."""
    from habit_ledger import update_ledger
    habits = [_habit("P", ("Bash:pytest", "Edit"), dist=300.0, reward=0.3)]
    ledger = {
        "P|Bash:pytest>Edit": {
            "times_observed": 1,
            "status": "detected",
            "first_seen": "2026-05-01",
        }
    }
    out = update_ledger(habits, ledger, skills_dir=tmp_path)
    assert out["P|Bash:pytest>Edit"]["status"] == "detected"


# ---------------------------------------------------------------------------
# No graduation — boundary cases
# ---------------------------------------------------------------------------

def test_no_graduation_low_distinctiveness(tmp_path):
    """Below D_THRESHOLD should stay detected regardless of observation count."""
    from habit_ledger import update_ledger
    habits = [_habit("P", ("Bash:ls", "Read"), dist=50.0)]
    out = update_ledger(habits, {}, skills_dir=tmp_path)
    assert out["P|Bash:ls>Read"]["status"] == "detected"


def test_no_graduation_first_observation(tmp_path):
    """High distinctiveness but only 1 observation (new entry) stays detected."""
    from habit_ledger import update_ledger
    habits = [_habit("P", ("Bash:pytest", "Edit"), dist=300.0)]
    out = update_ledger(habits, {}, skills_dir=tmp_path)
    entry = out["P|Bash:pytest>Edit"]
    assert entry["status"] == "detected"
    assert entry["times_observed"] == 1


# ---------------------------------------------------------------------------
# skill_exists override
# ---------------------------------------------------------------------------

def test_skill_exists_overrides(tmp_path):
    """A matching skill dir should set status to 'skill_exists', overriding suggested."""
    from habit_ledger import update_ledger
    (tmp_path / "git-commit-helper").mkdir()
    habits = [_habit("P", ("Bash:git", "Edit"), dist=300.0)]
    ledger = {
        "P|Bash:git>Edit": {
            "times_observed": 5,
            "status": "suggested_skill",
            "first_seen": "x",
        }
    }
    out = update_ledger(habits, ledger, skills_dir=tmp_path)
    assert out["P|Bash:git>Edit"]["status"] == "skill_exists"


def test_skill_exists_overrides_detected_too(tmp_path):
    """skill_exists should also override a status that hasn't yet graduated."""
    from habit_ledger import update_ledger
    (tmp_path / "pytest-runner").mkdir()
    habits = [_habit("P", ("Bash:pytest", "Edit"), dist=50.0)]
    out = update_ledger(habits, {}, skills_dir=tmp_path)
    assert out["P|Bash:pytest>Edit"]["status"] == "skill_exists"


# ---------------------------------------------------------------------------
# cross_project_patterns
# ---------------------------------------------------------------------------

def test_cross_project_pattern():
    """Routine appearing in >=3 projects with mean distinctiveness >50 is returned."""
    from habit_ledger import cross_project_patterns
    habits = [
        _habit("A", ("Bash:git", "Edit"), 300),
        _habit("B", ("Bash:git", "Edit"), 300),
        _habit("C", ("Bash:git", "Edit"), 300),
        _habit("D", ("Bash:other", "Read"), 50),
    ]
    patterns = cross_project_patterns(habits, min_projects=3)
    assert any(tuple(p["routine"]) == ("Bash:git", "Edit") for p in patterns)


def test_cross_project_below_threshold():
    """Routine in only 2 projects should NOT appear when min_projects=3."""
    from habit_ledger import cross_project_patterns
    habits = [
        _habit("A", ("Bash:git", "Edit"), 300),
        _habit("B", ("Bash:git", "Edit"), 300),
    ]
    patterns = cross_project_patterns(habits, min_projects=3)
    assert not any(tuple(p["routine"]) == ("Bash:git", "Edit") for p in patterns)


def test_cross_project_low_distinctiveness_filtered():
    """Routine in 3+ projects but mean distinctiveness <= 50 is filtered out."""
    from habit_ledger import cross_project_patterns
    habits = [
        _habit("A", ("Read", "Edit"), 10),
        _habit("B", ("Read", "Edit"), 10),
        _habit("C", ("Read", "Edit"), 10),
    ]
    patterns = cross_project_patterns(habits, min_projects=3)
    assert not any(tuple(p["routine"]) == ("Read", "Edit") for p in patterns)


# ---------------------------------------------------------------------------
# save_ledger / load_ledger — round-trip
# ---------------------------------------------------------------------------

def test_save_and_load_ledger(tmp_path):
    from habit_ledger import save_ledger, load_ledger
    p = tmp_path / "ledger.json"
    save_ledger({"k": {"status": "detected"}}, path=p)
    assert load_ledger(path=p)["k"]["status"] == "detected"


def test_load_ledger_missing_file(tmp_path):
    """Missing file returns empty dict without crashing."""
    from habit_ledger import load_ledger
    result = load_ledger(path=tmp_path / "nonexistent.json")
    assert result == {}


def test_load_ledger_corrupt_file(tmp_path):
    """Corrupt JSON returns empty dict without crashing."""
    from habit_ledger import load_ledger
    p = tmp_path / "corrupt.json"
    p.write_text("{ not valid json !!!", encoding="utf-8")
    result = load_ledger(path=p)
    assert result == {}


def test_save_ledger_atomic(tmp_path):
    """Saving should produce a valid JSON file, not a partial write."""
    from habit_ledger import save_ledger, load_ledger
    p = tmp_path / "sub" / "ledger.json"
    p.parent.mkdir(parents=True)
    data = {"key1": {"status": "suggested_skill", "times_observed": 3}}
    save_ledger(data, path=p)
    loaded = load_ledger(path=p)
    assert loaded == data


# ---------------------------------------------------------------------------
# update_ledger — fields correctness
# ---------------------------------------------------------------------------

def test_update_ledger_fields_on_new_entry(tmp_path):
    """New entry should have first_seen, times_observed=1, last_distinctiveness, last_reward."""
    from habit_ledger import update_ledger
    habits = [_habit("P", ("Bash:pytest", "Edit"), dist=150.0, reward=0.9)]
    out = update_ledger(habits, {}, skills_dir=tmp_path)
    entry = out["P|Bash:pytest>Edit"]
    assert entry["times_observed"] == 1
    assert "first_seen" in entry
    assert entry["last_distinctiveness"] == 150.0
    assert entry["last_reward"] == 0.9


def test_update_ledger_increments_times_observed(tmp_path):
    """Existing entry with times_observed=3 should reach 4 after update."""
    from habit_ledger import update_ledger
    key = "P|Bash:git>Edit"
    ledger = {key: {"times_observed": 3, "status": "detected", "first_seen": "2026-01-01"}}
    habits = [_habit("P", ("Bash:git", "Edit"), dist=50.0)]
    out = update_ledger(habits, ledger, skills_dir=tmp_path)
    assert out[key]["times_observed"] == 4


def test_update_ledger_accepts_dataclass_objects(tmp_path):
    """update_ledger should handle HabitCandidate dataclass objects, not just dicts."""
    from habit_ledger import update_ledger
    from habit_miner import HabitCandidate
    habit_obj = HabitCandidate(
        project="P",
        cue="P",
        routine=("Bash:pytest", "Edit"),
        count=10,
        session_count=4,
        last_seen="2026-05-25",
        strength=9.0,
        distinctiveness=300.0,
        reward_ratio=0.9,
        status="detected",
    )
    out = update_ledger([habit_obj], {}, skills_dir=tmp_path)
    assert "P|Bash:pytest>Edit" in out


# ---------------------------------------------------------------------------
# _skill_matches_routine — exact token matching (not substring)
# ---------------------------------------------------------------------------

def test_skill_match_exact_token_not_substring(tmp_path):
    from habit_ledger import _skill_matches_routine
    (tmp_path / "git-commit-helper").mkdir()
    (tmp_path / "test-engineer").mkdir()
    (tmp_path / "digital-foo").mkdir()
    # exact token match: "git" matches skill "git-commit-helper" (has token "git") ✓
    assert _skill_matches_routine(("Bash:git", "Edit"), tmp_path) is True
    # "git" must NOT match "digital-foo" via substring (di-GIT-al); tokens are {digital,foo}
    only_digital = tmp_path / "only_digital"
    only_digital.mkdir()
    (only_digital / "digital-foo").mkdir()
    assert _skill_matches_routine(("Bash:git", "Edit"), only_digital) is False
    # pytest must NOT match test-engineer (tokens {test,engineer}; "pytest" != any token)
    assert _skill_matches_routine(("Bash:pytest", "Edit"), tmp_path) is False
    # plain churn tokens (no colon) never produce a match
    assert _skill_matches_routine(("Read", "Edit"), tmp_path) is False
