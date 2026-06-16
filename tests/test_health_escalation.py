"""Tests for health_escalation.py — self-healing escalation layer."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)

_OK_HEALTH = {
    "last_run": "2026-06-10T12:00:00",
    "run_type": "daily",
    "tasks_total": 10,
    "tasks_failed": 0,
    "failures": [],
    "status": "OK",
}

_DEGRADED_OLLAMA = {
    "last_run": "2026-06-10T12:00:00",
    "run_type": "daily",
    "tasks_total": 10,
    "tasks_failed": 1,
    "failures": ["ollama_doctor"],
    "status": "DEGRADED",
}

_DEGRADED_OTHER = {
    "last_run": "2026-06-10T12:00:00",
    "run_type": "daily",
    "tasks_total": 10,
    "tasks_failed": 1,
    "failures": ["wiki_freshness_check"],
    "status": "DEGRADED",
}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


def _mock_remediation_success(**_kw):
    return {"action": "restart_ollama", "success": True, "detail": "mock OK"}


def _mock_remediation_fail(**_kw):
    return {"action": "restart_ollama", "success": False, "detail": "mock FAIL"}


# ---------------------------------------------------------------------------
# consecutive_degraded
# ---------------------------------------------------------------------------

def test_consecutive_zero_on_empty():
    from health_escalation import consecutive_degraded
    assert consecutive_degraded([]) == 0


def test_consecutive_zero_when_last_is_ok():
    from health_escalation import consecutive_degraded
    history = [_DEGRADED_OLLAMA.copy(), _OK_HEALTH.copy()]
    assert consecutive_degraded(history) == 0


def test_consecutive_counts_tail():
    from health_escalation import consecutive_degraded
    history = [_OK_HEALTH.copy(), _DEGRADED_OLLAMA.copy(), _DEGRADED_OLLAMA.copy()]
    assert consecutive_degraded(history) == 2


def test_consecutive_all_degraded():
    from health_escalation import consecutive_degraded
    history = [_DEGRADED_OLLAMA.copy()] * 5
    assert consecutive_degraded(history) == 5


def test_consecutive_filters_by_run_type():
    from health_escalation import consecutive_degraded
    # weekly OK in between — should not break the daily streak
    weekly_ok = {**_OK_HEALTH, "run_type": "weekly"}
    daily_deg = {**_DEGRADED_OLLAMA, "run_type": "daily"}
    history = [daily_deg, weekly_ok, daily_deg]
    # filtered to "daily" only → [daily_deg, daily_deg] → 2
    assert consecutive_degraded(history, run_type="daily") == 2


# ---------------------------------------------------------------------------
# should_escalate
# ---------------------------------------------------------------------------

def test_should_escalate_false_below_threshold():
    from health_escalation import should_escalate
    history = [_DEGRADED_OLLAMA.copy()]
    assert should_escalate(history, threshold=2) is False


def test_should_escalate_true_at_threshold():
    from health_escalation import should_escalate
    history = [_DEGRADED_OLLAMA.copy(), _DEGRADED_OLLAMA.copy()]
    assert should_escalate(history, threshold=2) is True


def test_should_escalate_true_above_threshold():
    from health_escalation import should_escalate
    history = [_DEGRADED_OLLAMA.copy()] * 4
    assert should_escalate(history, threshold=2) is True


# ---------------------------------------------------------------------------
# append_history
# ---------------------------------------------------------------------------

def test_append_history_creates_file(tmp_path):
    from health_escalation import append_history
    p = tmp_path / "h.jsonl"
    append_history(_OK_HEALTH.copy(), history_path=p, now=NOW)
    assert p.exists()
    lines = [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0]["status"] == "OK"
    assert lines[0]["ts"] == NOW.isoformat()


def test_append_history_multiple_entries(tmp_path):
    from health_escalation import append_history
    p = tmp_path / "h.jsonl"
    for _ in range(3):
        append_history(_DEGRADED_OLLAMA.copy(), history_path=p, now=NOW)
    lines = [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
    assert len(lines) == 3


def test_append_history_tolerates_missing_parent(tmp_path):
    from health_escalation import append_history
    p = tmp_path / "sub" / "deep" / "h.jsonl"
    # Should not raise even though parent dirs don't exist
    append_history(_OK_HEALTH.copy(), history_path=p, now=NOW)
    assert p.exists()


# ---------------------------------------------------------------------------
# Corrupt / missing file tolerance
# ---------------------------------------------------------------------------

def test_load_json_tolerates_corrupt(tmp_path):
    from health_escalation import _load_json
    p = tmp_path / "bad.json"
    p.write_text("{not valid{{", encoding="utf-8")
    assert _load_json(p) == {}


def test_load_json_tolerates_missing(tmp_path):
    from health_escalation import _load_json
    assert _load_json(tmp_path / "nonexistent.json") == {}


def test_load_jsonl_tolerates_corrupt_lines(tmp_path):
    from health_escalation import _load_jsonl
    p = tmp_path / "mixed.jsonl"
    p.write_text('{"status":"OK"}\n{BAD}\n{"status":"DEGRADED"}\n', encoding="utf-8")
    records = _load_jsonl(p)
    # 2 valid lines, 1 skipped
    assert len(records) == 2


# ---------------------------------------------------------------------------
# run_escalation — ollama failure path
# ---------------------------------------------------------------------------

def test_escalation_triggers_on_consecutive_degraded(tmp_path):
    from health_escalation import run_escalation

    hp = tmp_path / "health.json"
    hist = tmp_path / "history.jsonl"
    esc = tmp_path / "escalation.json"
    esc_hist = tmp_path / "escalation-history.jsonl"

    _write_json(hp, _DEGRADED_OLLAMA)
    # Seed history with one prior DEGRADED so we hit threshold=2
    _write_jsonl(hist, [_DEGRADED_OLLAMA])

    with patch("health_escalation.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = run_escalation(
            health_path=hp,
            history_path=hist,
            escalation_path=esc,
            escalation_hist_path=esc_hist,
            threshold=2,
            now=NOW,
            remediator=_mock_remediation_success,
        )

    assert result is not None
    assert result["action"] == "restart_ollama"
    assert result["success"] is True
    assert result["recheck_ok"] is True
    assert result["needs_human"] is False
    assert esc.exists()


def test_escalation_needs_human_on_recheck_fail(tmp_path):
    from health_escalation import run_escalation

    hp = tmp_path / "health.json"
    hist = tmp_path / "history.jsonl"
    esc = tmp_path / "escalation.json"
    esc_hist = tmp_path / "escalation-history.jsonl"

    _write_json(hp, _DEGRADED_OLLAMA)
    _write_jsonl(hist, [_DEGRADED_OLLAMA])

    with patch("health_escalation.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1)  # recheck fails
        result = run_escalation(
            health_path=hp,
            history_path=hist,
            escalation_path=esc,
            escalation_hist_path=esc_hist,
            threshold=2,
            now=NOW,
            remediator=_mock_remediation_success,
        )

    assert result["needs_human"] is True
    assert result["recheck_ok"] is False


def test_escalation_needs_human_on_remediation_fail(tmp_path):
    from health_escalation import run_escalation

    hp = tmp_path / "health.json"
    hist = tmp_path / "history.jsonl"
    esc = tmp_path / "escalation.json"
    esc_hist = tmp_path / "escalation-history.jsonl"

    _write_json(hp, _DEGRADED_OLLAMA)
    _write_jsonl(hist, [_DEGRADED_OLLAMA])

    with patch("health_escalation.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = run_escalation(
            health_path=hp,
            history_path=hist,
            escalation_path=esc,
            escalation_hist_path=esc_hist,
            threshold=2,
            now=NOW,
            remediator=_mock_remediation_fail,
        )

    assert result["needs_human"] is True
    assert result["success"] is False


# ---------------------------------------------------------------------------
# run_escalation — non-ollama failure path
# ---------------------------------------------------------------------------

def test_escalation_non_ollama_action_none(tmp_path):
    from health_escalation import run_escalation

    hp = tmp_path / "health.json"
    hist = tmp_path / "history.jsonl"
    esc = tmp_path / "escalation.json"
    esc_hist = tmp_path / "escalation-history.jsonl"

    _write_json(hp, _DEGRADED_OTHER)
    _write_jsonl(hist, [_DEGRADED_OTHER])

    result = run_escalation(
        health_path=hp,
        history_path=hist,
        escalation_path=esc,
        escalation_hist_path=esc_hist,
        threshold=2,
        now=NOW,
    )

    assert result is not None
    assert result["action"] == "none"
    assert result["needs_human"] is True
    assert result["success"] is None


# ---------------------------------------------------------------------------
# run_escalation — deduplication (anti-flapping)
# ---------------------------------------------------------------------------

def test_escalation_suppressed_when_recent_same_failures(tmp_path):
    from health_escalation import run_escalation

    hp = tmp_path / "health.json"
    hist = tmp_path / "history.jsonl"
    esc = tmp_path / "escalation.json"
    esc_hist = tmp_path / "escalation-history.jsonl"

    _write_json(hp, _DEGRADED_OLLAMA)
    _write_jsonl(hist, [_DEGRADED_OLLAMA])

    # Write a recent escalation (<12h ago, same failures)
    recent_ts = (NOW - timedelta(hours=6)).isoformat()
    _write_json(esc, {
        "ts": recent_ts,
        "trigger": {"consecutive": 2, "failures": ["ollama_doctor"]},
        "action": "restart_ollama",
        "success": True,
        "recheck_ok": True,
        "needs_human": False,
    })

    result = run_escalation(
        health_path=hp,
        history_path=hist,
        escalation_path=esc,
        escalation_hist_path=esc_hist,
        threshold=2,
        now=NOW,
    )

    assert result is not None
    assert result["action"] == "suppressed_recent"


def test_escalation_not_suppressed_when_old(tmp_path):
    from health_escalation import run_escalation

    hp = tmp_path / "health.json"
    hist = tmp_path / "history.jsonl"
    esc = tmp_path / "escalation.json"
    esc_hist = tmp_path / "escalation-history.jsonl"

    _write_json(hp, _DEGRADED_OLLAMA)
    _write_jsonl(hist, [_DEGRADED_OLLAMA])

    # Old escalation (>12h)
    old_ts = (NOW - timedelta(hours=13)).isoformat()
    _write_json(esc, {
        "ts": old_ts,
        "trigger": {"consecutive": 2, "failures": ["ollama_doctor"]},
        "action": "restart_ollama",
        "success": True,
        "recheck_ok": True,
        "needs_human": False,
    })

    with patch("health_escalation.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = run_escalation(
            health_path=hp,
            history_path=hist,
            escalation_path=esc,
            escalation_hist_path=esc_hist,
            threshold=2,
            now=NOW,
            remediator=_mock_remediation_success,
        )

    assert result is not None
    assert result["action"] == "restart_ollama"


def test_escalation_not_suppressed_when_different_failures(tmp_path):
    from health_escalation import run_escalation

    hp = tmp_path / "health.json"
    hist = tmp_path / "history.jsonl"
    esc = tmp_path / "escalation.json"
    esc_hist = tmp_path / "escalation-history.jsonl"

    _write_json(hp, _DEGRADED_OLLAMA)
    _write_jsonl(hist, [_DEGRADED_OLLAMA])

    # Recent but different failures
    recent_ts = (NOW - timedelta(hours=3)).isoformat()
    _write_json(esc, {
        "ts": recent_ts,
        "trigger": {"consecutive": 2, "failures": ["wiki_freshness_check"]},
        "action": "none",
        "success": None,
        "recheck_ok": None,
        "needs_human": True,
    })

    with patch("health_escalation.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = run_escalation(
            health_path=hp,
            history_path=hist,
            escalation_path=esc,
            escalation_hist_path=esc_hist,
            threshold=2,
            now=NOW,
            remediator=_mock_remediation_success,
        )

    assert result is not None
    assert result["action"] == "restart_ollama"


# ---------------------------------------------------------------------------
# run_escalation — below threshold / OK status — no escalation
# ---------------------------------------------------------------------------

def test_no_escalation_when_ok_status(tmp_path):
    from health_escalation import run_escalation

    hp = tmp_path / "health.json"
    hist = tmp_path / "history.jsonl"
    esc = tmp_path / "escalation.json"
    esc_hist = tmp_path / "escalation-history.jsonl"

    _write_json(hp, _OK_HEALTH)
    _write_jsonl(hist, [_OK_HEALTH, _OK_HEALTH, _OK_HEALTH])

    result = run_escalation(
        health_path=hp,
        history_path=hist,
        escalation_path=esc,
        escalation_hist_path=esc_hist,
        threshold=2,
        now=NOW,
    )
    assert result is None
    assert not esc.exists()


def test_no_escalation_below_threshold(tmp_path):
    from health_escalation import run_escalation

    hp = tmp_path / "health.json"
    hist = tmp_path / "history.jsonl"
    esc = tmp_path / "escalation.json"
    esc_hist = tmp_path / "escalation-history.jsonl"

    # Only 1 DEGRADED in history + current = 1 < threshold=2 before current is appended
    # After append: history has 2 entries but 1 of them is OK
    _write_json(hp, _DEGRADED_OLLAMA)
    _write_jsonl(hist, [_OK_HEALTH])  # only 1 prior, so consecutive will be 1 after appending current... wait
    # Actually: hist starts with [OK], we append DEGRADED → history=[OK, DEGRADED] → consecutive=1
    result = run_escalation(
        health_path=hp,
        history_path=hist,
        escalation_path=esc,
        escalation_hist_path=esc_hist,
        threshold=2,
        now=NOW,
    )
    assert result is None


# ---------------------------------------------------------------------------
# run_escalation — escalation.json contract / history.jsonl
# ---------------------------------------------------------------------------

def test_escalation_writes_history_jsonl(tmp_path):
    from health_escalation import run_escalation

    hp = tmp_path / "health.json"
    hist = tmp_path / "history.jsonl"
    esc = tmp_path / "escalation.json"
    esc_hist = tmp_path / "escalation-history.jsonl"

    _write_json(hp, _DEGRADED_OLLAMA)
    _write_jsonl(hist, [_DEGRADED_OLLAMA])

    with patch("health_escalation.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        run_escalation(
            health_path=hp,
            history_path=hist,
            escalation_path=esc,
            escalation_hist_path=esc_hist,
            threshold=2,
            now=NOW,
            remediator=_mock_remediation_success,
        )

    assert esc_hist.exists()
    lines = [json.loads(ln) for ln in esc_hist.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0]["action"] == "restart_ollama"


def test_escalation_json_has_required_fields(tmp_path):
    """Verify the contract: all fields daily_brief expects are present."""
    from health_escalation import run_escalation

    hp = tmp_path / "health.json"
    hist = tmp_path / "history.jsonl"
    esc = tmp_path / "escalation.json"
    esc_hist = tmp_path / "escalation-history.jsonl"

    _write_json(hp, _DEGRADED_OLLAMA)
    _write_jsonl(hist, [_DEGRADED_OLLAMA])

    with patch("health_escalation.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = run_escalation(
            health_path=hp,
            history_path=hist,
            escalation_path=esc,
            escalation_hist_path=esc_hist,
            threshold=2,
            now=NOW,
            remediator=_mock_remediation_success,
        )

    required_keys = {"ts", "trigger", "action", "success", "recheck_ok", "needs_human"}
    assert required_keys.issubset(result.keys())

    on_disk = json.loads(esc.read_text(encoding="utf-8"))
    assert required_keys.issubset(on_disk.keys())
    assert isinstance(on_disk["trigger"]["consecutive"], int)
    assert isinstance(on_disk["trigger"]["failures"], list)


# ---------------------------------------------------------------------------
# remediate_ollama — dry_run (safe, no real taskkill in tests)
# ---------------------------------------------------------------------------

def test_remediate_dry_run_returns_success():
    from health_escalation import remediate_ollama
    result = remediate_ollama(dry_run=True)
    assert result["action"] == "restart_ollama"
    assert result["success"] is True
    assert "dry_run" in result["detail"]


def test_remediate_uses_runner_injectable(tmp_path):
    """Verify runner DI: mock runner is called, no real processes spawned."""
    from health_escalation import remediate_ollama

    calls: list[list] = []

    def fake_runner(cmd, **_kw):
        calls.append(cmd)
        m = MagicMock()
        m.returncode = 0
        return m

    # Patch Popen so serve doesn't actually start, and _ollama_responding so wait exits fast
    with patch("health_escalation.subprocess.Popen") as mock_popen, \
         patch("health_escalation._ollama_responding", return_value=True):
        mock_popen.return_value = MagicMock()
        result = remediate_ollama(dry_run=False, runner=fake_runner)

    # taskkill was called for both process names
    assert any("taskkill" in str(c) for c in calls)
    assert result["action"] == "restart_ollama"


# ---------------------------------------------------------------------------
# mark_run_state
# ---------------------------------------------------------------------------

def test_mark_run_state_running_creates_file(tmp_path):
    from health_escalation import mark_run_state
    p = tmp_path / "run-state-daily.json"
    mark_run_state("daily", "RUNNING", pid=12345, now=NOW, path=p)
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["run_type"] == "daily"
    assert data["status"] == "RUNNING"
    assert data["pid"] == 12345
    assert data["ts"] == NOW.isoformat()


def test_mark_run_state_completed_overwrites(tmp_path):
    from health_escalation import mark_run_state
    p = tmp_path / "run-state-daily.json"
    mark_run_state("daily", "RUNNING", pid=12345, now=NOW, path=p)
    mark_run_state("daily", "COMPLETED", pid=12345, now=NOW, path=p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["status"] == "COMPLETED"


def test_mark_run_state_uses_own_pid_by_default(tmp_path):
    import os as _os
    from health_escalation import mark_run_state
    p = tmp_path / "run-state-weekly.json"
    mark_run_state("weekly", "RUNNING", path=p, now=NOW)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["pid"] == _os.getpid()


# ---------------------------------------------------------------------------
# detect_aborted_runs
# ---------------------------------------------------------------------------

def _write_state(directory: "Path", run_type: str, status: str, pid: int, ts: str) -> None:
    """Helper: write a run-state sentinel directly."""
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"run-state-{run_type}.json"
    p.write_text(
        json.dumps({"run_type": run_type, "status": status, "pid": pid, "ts": ts}),
        encoding="utf-8",
    )


def test_detect_aborted_dead_pid(tmp_path):
    """A RUNNING sentinel whose pid is dead is returned as aborted."""
    from health_escalation import detect_aborted_runs
    DEAD_PID = 99999999  # extremely unlikely to exist
    _write_state(tmp_path, "daily", "RUNNING", DEAD_PID, NOW.isoformat())
    with patch("health_escalation._pid_alive", return_value=False):
        result = detect_aborted_runs(now=NOW, states_dir=tmp_path)
    assert len(result) == 1
    assert result[0]["run_type"] == "daily"


def test_detect_aborted_old_timestamp(tmp_path):
    """A RUNNING sentinel >12 h old is returned as aborted even if pid alive."""
    from health_escalation import detect_aborted_runs
    old_ts = (NOW - timedelta(hours=13)).isoformat()
    _write_state(tmp_path, "weekly", "RUNNING", 99999, old_ts)
    # pid_alive returns True — detection should still fire via age check
    with patch("health_escalation._pid_alive", return_value=True):
        result = detect_aborted_runs(now=NOW, states_dir=tmp_path)
    assert len(result) == 1
    assert result[0]["run_type"] == "weekly"


def test_detect_aborted_running_alive_recent(tmp_path):
    """A RUNNING sentinel with alive pid and recent ts is NOT aborted."""
    from health_escalation import detect_aborted_runs
    recent_ts = (NOW - timedelta(hours=1)).isoformat()
    _write_state(tmp_path, "daily", "RUNNING", 99999, recent_ts)
    with patch("health_escalation._pid_alive", return_value=True):
        result = detect_aborted_runs(now=NOW, states_dir=tmp_path)
    assert result == []


def test_detect_aborted_completed_ignored(tmp_path):
    """COMPLETED sentinels are never returned as aborted."""
    from health_escalation import detect_aborted_runs
    DEAD_PID = 99999999
    _write_state(tmp_path, "daily", "COMPLETED", DEAD_PID, NOW.isoformat())
    with patch("health_escalation._pid_alive", return_value=False):
        result = detect_aborted_runs(now=NOW, states_dir=tmp_path)
    assert result == []


def test_detect_aborted_corrupt_state_file_skipped(tmp_path):
    """Corrupt JSON in a state file must not crash detect_aborted_runs."""
    from health_escalation import detect_aborted_runs
    (tmp_path / "run-state-daily.json").write_text("{not json{{", encoding="utf-8")
    result = detect_aborted_runs(now=NOW, states_dir=tmp_path)
    assert result == []


def test_detect_aborted_empty_dir(tmp_path):
    from health_escalation import detect_aborted_runs
    result = detect_aborted_runs(now=NOW, states_dir=tmp_path)
    assert result == []


# ---------------------------------------------------------------------------
# record_aborted
# ---------------------------------------------------------------------------

def test_record_aborted_appends_to_history(tmp_path):
    from health_escalation import record_aborted
    p = tmp_path / "history.jsonl"
    record_aborted("daily", history_path=p, now=NOW)
    lines = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0]["status"] == "ABORTED"
    assert lines[0]["run_type"] == "daily"
    assert lines[0]["ts"] == NOW.isoformat()


def test_record_aborted_multiple_appends(tmp_path):
    from health_escalation import record_aborted
    p = tmp_path / "history.jsonl"
    for rt in ("daily", "weekly", "daily"):
        record_aborted(rt, history_path=p, now=NOW)
    lines = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 3


# ---------------------------------------------------------------------------
# consecutive_degraded — ABORTED counts as bad
# ---------------------------------------------------------------------------

def test_consecutive_aborted_counts_as_bad():
    from health_escalation import consecutive_degraded
    history = [
        {**_OK_HEALTH, "run_type": "daily"},
        {"status": "ABORTED", "run_type": "daily"},
        {"status": "ABORTED", "run_type": "daily"},
    ]
    assert consecutive_degraded(history) == 2


def test_consecutive_mixed_degraded_and_aborted():
    from health_escalation import consecutive_degraded
    history = [
        {**_OK_HEALTH, "run_type": "daily"},
        {**_DEGRADED_OLLAMA, "run_type": "daily"},
        {"status": "ABORTED", "run_type": "daily"},
    ]
    assert consecutive_degraded(history) == 2


def test_consecutive_aborted_broken_by_ok():
    from health_escalation import consecutive_degraded
    history = [
        {"status": "ABORTED", "run_type": "daily"},
        {**_OK_HEALTH, "run_type": "daily"},
        {"status": "ABORTED", "run_type": "daily"},
    ]
    # Only the last ABORTED counts — the OK in the middle resets
    assert consecutive_degraded(history) == 1


def test_consecutive_run_type_filter_ignores_other_types():
    from health_escalation import consecutive_degraded
    weekly_ok = {**_OK_HEALTH, "run_type": "weekly"}
    daily_aborted = {"status": "ABORTED", "run_type": "daily"}
    history = [daily_aborted, weekly_ok, daily_aborted]
    # weekly_ok must NOT break the daily streak
    assert consecutive_degraded(history, run_type="daily") == 2
    # without filter, weekly_ok IS in the tail → breaks streak → count=1
    assert consecutive_degraded(history) == 1


# ---------------------------------------------------------------------------
# run_escalation — aborted runs integrated
# ---------------------------------------------------------------------------

def test_run_escalation_records_aborted_before_reading_history(tmp_path):
    """If detect_aborted_runs finds a victim, record_aborted is called and the
    ABORTED entry ends up in the history file before escalation logic runs."""
    from health_escalation import run_escalation

    hp = tmp_path / "health.json"
    hist = tmp_path / "history.jsonl"
    esc = tmp_path / "escalation.json"
    esc_hist = tmp_path / "escalation-history.jsonl"
    states = tmp_path / "states"

    # Seed one prior DEGRADED so total bad = ABORTED(injected) + DEGRADED(seeded) + DEGRADED(current) = 3
    _write_jsonl(hist, [{**_DEGRADED_OLLAMA, "run_type": "daily"}])
    _write_json(hp, _DEGRADED_OLLAMA)

    # Put a RUNNING sentinel with a dead pid
    DEAD_PID = 99999999
    _write_state(states, "daily", "RUNNING", DEAD_PID, NOW.isoformat())

    with patch("health_escalation._pid_alive", return_value=False), \
         patch("health_escalation.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = run_escalation(
            health_path=hp,
            history_path=hist,
            escalation_path=esc,
            escalation_hist_path=esc_hist,
            threshold=2,
            now=NOW,
            remediator=_mock_remediation_success,
            states_dir=states,
        )

    assert result is not None
    assert result["action"] == "restart_ollama"

    # Verify ABORTED was written to history
    lines = [json.loads(ln) for ln in hist.read_text(encoding="utf-8").splitlines() if ln.strip()]
    aborted_entries = [ln for ln in lines if ln.get("status") == "ABORTED"]
    assert len(aborted_entries) >= 1
