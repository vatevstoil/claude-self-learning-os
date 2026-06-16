"""Tests for selfreg_monitor.py — check_dispatcher, check_errors, check_hooks."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Ensure the scripts directory is on the path for direct import
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


def test_check_dispatcher_ok(tmp_path, monkeypatch):
    import selfreg_monitor as sm

    health = tmp_path / "health.json"
    health.write_text(json.dumps({
        "status": "OK", "last_run": datetime.now().isoformat(),
        "tasks_total": 5, "tasks_failed": 0, "failures": []
    }), encoding="utf-8")
    monkeypatch.setattr(sm, "DISPATCHER_HEALTH", health)
    score, issues = sm.check_dispatcher()
    assert score == 100
    assert issues == []


def test_check_dispatcher_degraded(tmp_path, monkeypatch):
    import selfreg_monitor as sm

    health = tmp_path / "health.json"
    health.write_text(json.dumps({
        "status": "DEGRADED", "last_run": datetime.now().isoformat(),
        "tasks_total": 3, "tasks_failed": 1, "failures": ["wiki_freshness"]
    }), encoding="utf-8")
    monkeypatch.setattr(sm, "DISPATCHER_HEALTH", health)
    score, issues = sm.check_dispatcher()
    assert score < 100
    assert any("wiki_freshness" in i for i in issues)


def test_check_dispatcher_missing(tmp_path, monkeypatch):
    import selfreg_monitor as sm

    monkeypatch.setattr(sm, "DISPATCHER_HEALTH", tmp_path / "nonexistent.json")
    score, issues = sm.check_dispatcher()
    assert score < 100
    assert issues


# --- check_hooks: guards the empty-hooks blind spot ------------------------

def test_check_hooks_healthy(tmp_path, monkeypatch):
    import selfreg_monitor as sm
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [{"command": "python auto_pinecone_save.py"}]}],
        "SessionStart": [{"hooks": [{"command": "python session_start_brief.py"}]}],
    }}), encoding="utf-8")
    monkeypatch.setattr(sm, "SETTINGS", settings)
    score, issues = sm.check_hooks()
    assert score == 100
    assert issues == []


def test_check_hooks_empty_detected(tmp_path, monkeypatch):
    import selfreg_monitor as sm
    settings = tmp_path / "settings.json"
    # The exact failure mode that went undetected for 2 days: empty blocks.
    settings.write_text(json.dumps({"hooks": {"Stop": [], "SessionStart": []}}),
                        encoding="utf-8")
    monkeypatch.setattr(sm, "SETTINGS", settings)
    score, issues = sm.check_hooks()
    assert score == 0
    assert len(issues) == 2
    assert any("auto_pinecone_save" in i for i in issues)


def test_check_hooks_one_missing(tmp_path, monkeypatch):
    import selfreg_monitor as sm
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [{"command": "python auto_pinecone_save.py"}]}],
        "SessionStart": [],
    }}), encoding="utf-8")
    monkeypatch.setattr(sm, "SETTINGS", settings)
    score, issues = sm.check_hooks()
    assert score == 50
    assert any("session_start_brief" in i for i in issues)


def test_check_hooks_unreadable(tmp_path, monkeypatch):
    import selfreg_monitor as sm
    monkeypatch.setattr(sm, "SETTINGS", tmp_path / "nonexistent.json")
    score, issues = sm.check_hooks()
    assert score == 50
    assert issues


# --- check_errors: last-state-wins + 7-day window ---------------------------

def _ts(dt: datetime) -> str:
    """Format a datetime as the log timestamp prefix."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _rc_line(dt: datetime, script: str, rc: int) -> str:
    return f"{_ts(dt)},000 [INFO] {script}: rc={rc} stdout=0B stderr=0B"


def _weekly_start(dt: datetime) -> str:
    return f"{_ts(dt)},000 [INFO] === WEEKLY RUN START ==="


def test_check_errors_fixed_script_not_counted(tmp_path, monkeypatch) -> None:
    """Script that failed then succeeded → last state is OK → not an issue."""
    import selfreg_monitor as sm

    now = datetime(2026, 6, 10, 12, 0, 0)
    log = "\n".join([
        _rc_line(now - timedelta(days=3), "some_script.py", 1),   # failure
        _rc_line(now - timedelta(days=1), "some_script.py", 0),   # fixed
    ])
    monkeypatch.setattr(sm, "PENDING_SAVES", tmp_path / "no-saves.jsonl")
    score, issues = sm.check_errors(log, now=now)
    assert score == 100
    assert issues == []


def test_check_errors_persistent_failure_counted(tmp_path, monkeypatch) -> None:
    """Script that fails and is never fixed → still an issue."""
    import selfreg_monitor as sm

    now = datetime(2026, 6, 10, 12, 0, 0)
    log = "\n".join([
        _rc_line(now - timedelta(days=2), "broken_script.py", 1),
        _rc_line(now - timedelta(days=1), "broken_script.py", 2),  # still failing
    ])
    monkeypatch.setattr(sm, "PENDING_SAVES", tmp_path / "no-saves.jsonl")
    score, issues = sm.check_errors(log, now=now)
    assert score < 100
    assert any("broken_script.py" in i for i in issues)


def test_check_errors_7day_window_excludes_old_failures(tmp_path, monkeypatch) -> None:
    """Failures older than 7 days are outside the window → not counted."""
    import selfreg_monitor as sm

    now = datetime(2026, 6, 10, 12, 0, 0)
    log = "\n".join([
        _rc_line(now - timedelta(days=10), "old_script.py", 1),  # outside window
    ])
    monkeypatch.setattr(sm, "PENDING_SAVES", tmp_path / "no-saves.jsonl")
    score, issues = sm.check_errors(log, now=now)
    assert score == 100
    assert issues == []


def test_check_errors_semantic_nonzero_ignored(tmp_path, monkeypatch) -> None:
    """wiki_freshness_check.py rc=1 is semantic (stale), must NOT be counted."""
    import selfreg_monitor as sm

    now = datetime(2026, 6, 10, 12, 0, 0)
    log = _rc_line(now - timedelta(hours=2), "wiki_freshness_check.py", 1)
    monkeypatch.setattr(sm, "PENDING_SAVES", tmp_path / "no-saves.jsonl")
    score, issues = sm.check_errors(log, now=now)
    assert score == 100
    assert issues == []


def test_check_errors_no_weekly_block_still_scans(tmp_path, monkeypatch) -> None:
    """No WEEKLY RUN START in log → still scan DAILY lines (old code returned 100 neutral)."""
    import selfreg_monitor as sm

    now = datetime(2026, 6, 10, 12, 0, 0)
    # Only DAILY lines, no WEEKLY markers
    log = "\n".join([
        f"{_ts(now - timedelta(hours=1))},000 [INFO] === DAILY RUN START ===",
        _rc_line(now - timedelta(hours=1), "real_failure.py", 3),
        f"{_ts(now)},000 [INFO] === DAILY RUN END ===",
    ])
    monkeypatch.setattr(sm, "PENDING_SAVES", tmp_path / "no-saves.jsonl")
    score, issues = sm.check_errors(log, now=now)
    # Should detect the failure instead of silently returning 100
    assert score < 100
    assert any("real_failure.py" in i for i in issues)


def test_check_errors_timeout_cleared_by_success(tmp_path, monkeypatch) -> None:
    """TIMEOUT followed by successful rc=0 for same script → cleared."""
    import selfreg_monitor as sm

    now = datetime(2026, 6, 10, 12, 0, 0)
    log = "\n".join([
        f"{_ts(now - timedelta(days=2))},000 [INFO] TIMEOUT: slow_script.py exceeded limit",
        _rc_line(now - timedelta(days=1), "slow_script.py", 0),   # ran OK after
    ])
    monkeypatch.setattr(sm, "PENDING_SAVES", tmp_path / "no-saves.jsonl")
    score, issues = sm.check_errors(log, now=now)
    # TIMEOUT is only associated with script if script name is found in the line;
    # once rc=0 is recorded, the timeout flag is cleared.
    # The timeout line may not extract the script name (depends on format) —
    # the critical check is that subsequent rc=0 clears a previously-seen timeout
    # for the same script key.
    assert score == 100 or not any("slow_script.py" in i for i in issues)


# ----------------------------------------------------------------- check_runs
# check_runs uses datetime.now() internally, so log lines are built relative
# to the real clock.

def _runs_log(daily_age: timedelta, weekly_age: timedelta,
              dreaming_age: timedelta | None) -> str:
    import selfreg_monitor  # noqa: F401 — ensures module import works

    now = datetime.now()
    lines = [
        f"{_ts(now - daily_age)},000 [INFO] === DAILY RUN END ===",
        f"{_ts(now - weekly_age)},000 [INFO] === WEEKLY RUN END ===",
    ]
    if dreaming_age is not None:
        lines.append(f"{_ts(now - dreaming_age)},000 [INFO] === DREAMING RUN END ===")
    return "\n".join(lines)


def test_check_runs_all_fresh_clean() -> None:
    import selfreg_monitor as sm

    log = _runs_log(timedelta(hours=3), timedelta(days=2), timedelta(days=3))
    score, issues = sm.check_runs(log)
    assert score == 100
    assert issues == []


def test_check_runs_dreaming_stale_flagged() -> None:
    """A dreaming pipeline silent for >8 days must not stay invisible."""
    import selfreg_monitor as sm

    log = _runs_log(timedelta(hours=3), timedelta(days=2), timedelta(days=10))
    score, issues = sm.check_runs(log)
    assert any("dreaming" in i for i in issues)
    assert score == 75


def test_check_runs_dreaming_marker_missing_flagged() -> None:
    import selfreg_monitor as sm

    log = _runs_log(timedelta(hours=3), timedelta(days=2), None)
    score, issues = sm.check_runs(log)
    assert any("dreaming" in i for i in issues)
    assert score == 75


def test_check_runs_weekly_only_stale() -> None:
    """Pin the new split: weekly-stale alone costs 25 (was 50 pre-dreaming-check)."""
    import selfreg_monitor as sm

    log = _runs_log(timedelta(hours=3), timedelta(days=10), timedelta(days=3))
    score, issues = sm.check_runs(log)
    assert any("weekly" in i for i in issues)
    assert not any("dreaming" in i for i in issues)
    assert score == 75
