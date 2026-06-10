import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


# ---------------------------------------------------------------------------
# Existing tests (updated where needed for the action-token filter)
# ---------------------------------------------------------------------------

def test_extract_ngrams_basic():
    from habit_miner import extract_ngrams
    # Use Bash:git (action token) so the n-gram is not filtered as pure churn
    seq = ["Bash:git", "Read", "Edit", "Bash:git", "Read", "Edit"]
    result = extract_ngrams(seq, n=3)
    assert ("Bash:git", "Read", "Edit") in result


def test_mine_habits_finds_repeated_sequence():
    from habit_miner import mine_habits
    sessions = {
        "s1": {"project": "Proj", "tools": ["Bash:git", "Read", "Edit", "Bash:git", "Read", "Edit"]},
        "s2": {"project": "Proj", "tools": ["Bash:git", "Read", "Edit"]},
        "s3": {"project": "Proj", "tools": ["Bash:git", "Read", "Edit"]},
    }
    habits = mine_habits(sessions)
    found = [h for h in habits if h.routine == ("Bash:git", "Read", "Edit")]
    assert len(found) == 1
    assert found[0].count >= 4
    assert found[0].session_count >= 2


def test_mine_habits_below_threshold_returns_empty():
    from habit_miner import mine_habits
    # Only 1 session, 1 occurrence -> below threshold (need >=4, >=2 sessions)
    sessions = {
        "s1": {"project": "Proj", "tools": ["Bash:git", "Read", "Edit"]},
    }
    habits = mine_habits(sessions)
    assert len(habits) == 0


def test_mine_habits_ignores_trivial_all_same_tool():
    from habit_miner import mine_habits
    # All-same tool sequence is trivial navigation noise
    sessions = {f"s{i}": {"project": "P", "tools": ["Read"] * 6} for i in range(5)}
    habits = mine_habits(sessions)
    trivial = [h for h in habits if len(set(h.routine)) == 1]
    assert len(trivial) == 0


def test_mine_habits_ignores_pure_read_only_sequences():
    from habit_miner import mine_habits
    # Read+Glob+Grep only = navigational noise, not a habit worth tracking
    sessions = {f"s{i}": {"project": "P", "tools": ["Read", "Glob", "Read", "Glob"]}
                for i in range(5)}
    habits = mine_habits(sessions)
    read_only = {"Read", "Glob", "Grep", "WebSearch", "WebFetch"}
    pure_read = [h for h in habits if set(h.routine).issubset(read_only)]
    assert len(pure_read) == 0


def test_save_and_load_habits(tmp_path):
    from habit_miner import mine_habits, save_habits, load_habits
    sessions = {
        f"s{i}": {"project": "Demo", "tools": ["Bash:git", "Edit", "Bash:git", "Edit"]}
        for i in range(5)
    }
    habits = mine_habits(sessions)
    p = tmp_path / "habits.json"
    save_habits(habits, path=p)
    loaded = load_habits(path=p)
    assert len(loaded) == len(habits)
    if habits:
        assert loaded[0]["routine"] == list(habits[0].routine)


# ---------------------------------------------------------------------------
# NEW tests for the 6 design changes
# ---------------------------------------------------------------------------

def test_tool_signature_bash_verb():
    from habit_miner import tool_signature
    assert tool_signature("Bash", {"command": "git status"}) == "Bash:git"
    assert tool_signature("Bash", {"command": "cd /x && pytest -v"}) == "Bash:pytest"
    assert tool_signature("Bash", {"command": "python ~/.claude/x.py"}) == "Bash:python"


def test_tool_signature_skill_and_agent():
    from habit_miner import tool_signature
    assert tool_signature("Skill", {"skill": "superpowers:writing-plans"}) == "Skill:writing-plans"
    assert tool_signature("Agent", {"subagent_type": "python-pro"}) == "Agent:python-pro"


def test_tool_signature_mcp():
    from habit_miner import tool_signature
    assert tool_signature("mcp__plugin_chrome__use_browser", {}) == "use_browser"


def test_action_token_filter_rejects_pure_churn():
    from habit_miner import mine_habits
    sessions = {f"s{i}": {"project": "P", "tools": ["Read", "Edit"] * 5} for i in range(6)}
    habits = mine_habits(sessions)
    churn = [h for h in habits if set(h.routine) <= {"Read", "Edit"}]
    assert churn == []


def test_action_token_filter_keeps_real_workflow():
    from habit_miner import mine_habits
    sessions = {f"s{i}": {"project": "P", "tools": ["Bash:grep", "Read", "Edit"] * 3}
                for i in range(4)}
    habits = mine_habits(sessions)
    found = [h for h in habits if "Bash:grep" in h.routine]
    assert len(found) >= 1
    assert found[0].distinctiveness > 0


def test_reward_ratio_discounts_corrected():
    from habit_miner import mine_habits
    sessions = {
        "clean1": {"project": "P", "segments": [["Bash:git", "Edit"]] * 3, "corrected_segments": set()},
        "clean2": {"project": "P", "segments": [["Bash:git", "Edit"]] * 3, "corrected_segments": set()},
        "bad1": {"project": "P", "segments": [["Bash:git", "Edit"]], "corrected_segments": {0}},
    }
    habits = mine_habits(sessions)
    h = next(x for x in habits if x.routine == ("Bash:git", "Edit"))
    assert 0.0 < h.reward_ratio < 1.0


# ---------------------------------------------------------------------------
# Cross-project transfer tests
# ---------------------------------------------------------------------------

def _make_cross_sessions(n_per_project=2, projects=("Alpha", "Beta", "Gamma")):
    """Build sessions where the same routine appears across multiple projects."""
    sessions = {}
    for proj in projects:
        for i in range(n_per_project):
            sid = f"{proj}-s{i}"
            # Repeat the routine enough times to meet MIN_COUNT=4 across all sessions
            sessions[sid] = {
                "project": proj,
                "tools": ["Bash:git", "Bash:pytest", "Edit"] * 3,
            }
    return sessions


def test_cross_project_detected():
    from habit_miner import mine_habits
    sessions = _make_cross_sessions()
    habits = mine_habits(sessions)
    cross = [h for h in habits if h.project == "_cross_project"]
    assert len(cross) >= 1


def test_cross_project_routine_correct():
    from habit_miner import mine_habits
    sessions = _make_cross_sessions()
    habits = mine_habits(sessions)
    cross = [h for h in habits if h.project == "_cross_project"]
    routines = {h.routine for h in cross}
    # The 3-gram Bash:git->Bash:pytest->Edit should be detected
    assert ("Bash:git", "Bash:pytest", "Edit") in routines


def test_cross_project_distinctiveness_boost():
    """Cross-project habits should have higher distinctiveness than single-project ones."""
    from habit_miner import mine_habits
    sessions = _make_cross_sessions(n_per_project=3, projects=("Alpha", "Beta", "Gamma"))
    habits = mine_habits(sessions)
    cross = [h for h in habits if h.project == "_cross_project"
             and h.routine == ("Bash:git", "Bash:pytest", "Edit")]
    single = [h for h in habits if h.project != "_cross_project"
              and h.routine == ("Bash:git", "Bash:pytest", "Edit")]
    if cross and single:
        assert cross[0].distinctiveness > single[0].distinctiveness


def test_cross_project_not_emitted_for_single_project():
    from habit_miner import mine_habits
    # All sessions in one project -> no cross-project habits
    sessions = {f"s{i}": {"project": "OnlyOne", "tools": ["Bash:git", "Edit"] * 5}
                for i in range(5)}
    habits = mine_habits(sessions)
    cross = [h for h in habits if h.project == "_cross_project"]
    assert len(cross) == 0


# ---------------------------------------------------------------------------
# Regression: bare "не" must not classify normal Bulgarian as a correction
# (substring match flagged ~99% of messages, starving habit graduation).
# ---------------------------------------------------------------------------

def test_is_correction_word_boundary():
    from habit_miner import _is_correction
    # Real corrections — must be True
    assert _is_correction("не, това е грешно")
    assert _is_correction("не така прави го")
    assert _is_correction("стоп спри")
    assert _is_correction("wrong approach")
    # Normal messages where "не" only appears inside other words — must be False
    assert not _is_correction("това е добро решение, имане налично, внимание")
    assert not _is_correction("благодаря, перфектно работи")
    assert not _is_correction("продължи нататък спокойно")
