"""Tests for selfreg_monitor.py — check_dispatcher and related functionality."""
from __future__ import annotations

import json
import sys
from datetime import datetime
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
