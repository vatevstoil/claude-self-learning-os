#!/usr/bin/env python3
"""health_escalation.py — self-healing escalation layer for the automation system.

Reads health.json after each run, maintains a rolling history, and triggers
remediation when consecutive DEGRADED states cross a threshold.

Remediation strategy (documented cure for the known Ollama failure mode):
  1. taskkill /IM ollama.exe /F  (and "ollama app.exe")
  2. ollama serve  (detached, hidden window)
  3. wait up to 30s for http://localhost:11434/api/version to respond
  4. re-run ollama_doctor.py --ensure --quiet as a confirmation check

Deduplication: if the last escalation is <12 h old AND failures are identical,
the action is suppressed ("suppressed_recent") to prevent flapping.

CLI:
    python health_escalation.py           # run escalation (safe, exit 0 always)
    python health_escalation.py --status  # print last escalation + consecutive count
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Default paths (injectable for tests)
# ---------------------------------------------------------------------------
_LOGS = Path.home() / ".claude" / "logs"
_HEALTH_PATH = _LOGS / "health.json"
_HISTORY_PATH = _LOGS / "health-history.jsonl"
_ESCALATION_PATH = _LOGS / "escalation.json"
_ESCALATION_HIST_PATH = _LOGS / "escalation-history.jsonl"
_SERVE_LOG = _LOGS / "ollama-serve.log"
_SCRIPTS = Path.home() / ".claude" / "scripts"

_CREATE_NO_WINDOW = 0x08000000
_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


# ---------------------------------------------------------------------------
# I/O helpers (tolerant loaders, atomic writes)
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict[str, Any]:
    """Load JSON file; return {} on any error (missing / corrupt)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load JSONL file; skip corrupt lines. Return [] on missing file."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass
    return records


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically via a temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON line to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Core pure functions
# ---------------------------------------------------------------------------

def append_history(
    health: dict[str, Any],
    history_path: Path = _HISTORY_PATH,
    now: datetime | None = None,
) -> None:
    """Append a health snapshot to the JSONL history log.

    Args:
        health: The health record (from health.json).
        history_path: Target JSONL file path.
        now: Timestamp override (for testing).
    """
    ts = (now or datetime.now(timezone.utc)).isoformat()
    record = dict(health)
    record.setdefault("ts", ts)
    _append_jsonl(history_path, record)


def consecutive_degraded(
    history: list[dict[str, Any]],
    run_type: str | None = None,
) -> int:
    """Count consecutive DEGRADED entries from the tail of history.

    Args:
        history: List of health records (oldest first).
        run_type: If set, filter to entries matching this run_type only.

    Returns:
        Number of consecutive DEGRADED entries from the end.
    """
    filtered = [
        h for h in history
        if run_type is None or h.get("run_type") == run_type
    ]
    count = 0
    for entry in reversed(filtered):
        if entry.get("status") == "DEGRADED":
            count += 1
        else:
            break
    return count


def should_escalate(
    history: list[dict[str, Any]],
    threshold: int = 2,
) -> bool:
    """Return True when consecutive DEGRADED count meets or exceeds threshold.

    Args:
        history: Full history list.
        threshold: Minimum consecutive DEGRADED entries to trigger escalation.
    """
    return consecutive_degraded(history) >= threshold


# ---------------------------------------------------------------------------
# Remediation
# ---------------------------------------------------------------------------

def _ollama_responding(host: str = "http://localhost:11434", timeout: float = 3.0) -> bool:
    """Check if ollama /api/version endpoint responds."""
    try:
        urlopen(Request(f"{host}/api/version"), timeout=timeout).read()
        return True
    except Exception:
        return False


def remediate_ollama(
    dry_run: bool = False,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Kill stuck Ollama processes and restart ollama serve.

    Args:
        dry_run: If True, log actions but do not execute.
        runner: subprocess.run-compatible callable (injectable for tests).

    Returns:
        {"action": "restart_ollama", "success": bool, "detail": str}
    """
    steps: list[str] = []

    if dry_run:
        return {
            "action": "restart_ollama",
            "success": True,
            "detail": "dry_run — no processes touched",
        }

    # Step 1: kill existing ollama processes (tolerate "not found")
    for proc_name in ("ollama.exe", "ollama app.exe"):
        try:
            result = runner(
                ["taskkill", "/IM", proc_name, "/F"],
                capture_output=True,
                timeout=15,
                creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            rc = result.returncode if hasattr(result, "returncode") else 0
            steps.append(f"taskkill {proc_name!r} rc={rc}")
        except Exception as exc:
            steps.append(f"taskkill {proc_name!r} err={exc}")

    time.sleep(1)

    # Step 2: start detached ollama serve with hidden window
    try:
        _SERVE_LOG.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        ollama_exe = shutil.which("ollama") or "ollama"
        cf = (_CREATE_NO_WINDOW | _DETACHED) if os.name == "nt" else 0
        # The detached child inherits the handle at spawn; closing our copy
        # right after Popen avoids holding the log open for the 30s wait loop.
        with open(_SERVE_LOG, "a", encoding="utf-8") as serve_log_fh:
            subprocess.Popen(
                [ollama_exe, "serve"],
                stdout=serve_log_fh,
                stderr=serve_log_fh,
                creationflags=cf,
                close_fds=True,
            )
        steps.append("ollama serve started (detached)")
    except Exception as exc:
        steps.append(f"serve start failed: {exc}")
        return {"action": "restart_ollama", "success": False, "detail": "; ".join(steps)}

    # Step 3: wait up to 30s for server to respond
    deadline = time.monotonic() + 30
    responding = False
    while time.monotonic() < deadline:
        if _ollama_responding():
            responding = True
            break
        time.sleep(1)

    if responding:
        steps.append("server responding within 30s")
    else:
        steps.append("server did NOT respond within 30s")

    return {
        "action": "restart_ollama",
        "success": responding,
        "detail": "; ".join(steps),
    }


# ---------------------------------------------------------------------------
# Deduplication helper
# ---------------------------------------------------------------------------

def _is_recent_duplicate(
    last_escalation: dict[str, Any],
    current_failures: list[str],
    now: datetime,
    window_hours: int = 12,
) -> bool:
    """Return True if the last escalation is <window_hours old with same failures."""
    ts_raw = last_escalation.get("ts")
    if not ts_raw:
        return False
    try:
        last_ts = datetime.fromisoformat(ts_raw)
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age_hours = (now - last_ts).total_seconds() / 3600
        if age_hours >= window_hours:
            return False
        last_failures = sorted(last_escalation.get("trigger", {}).get("failures", []))
        return last_failures == sorted(current_failures)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_escalation(
    health_path: Path = _HEALTH_PATH,
    history_path: Path = _HISTORY_PATH,
    escalation_path: Path = _ESCALATION_PATH,
    escalation_hist_path: Path = _ESCALATION_HIST_PATH,
    threshold: int = 2,
    now: datetime | None = None,
    remediator: Any = remediate_ollama,
) -> dict[str, Any] | None:
    """Read health.json, maintain history, escalate when threshold is crossed.

    Args:
        health_path: Path to current health.json.
        history_path: Path to health-history.jsonl.
        escalation_path: Path to escalation.json (latest record).
        escalation_hist_path: Path to escalation-history.jsonl.
        threshold: Consecutive DEGRADED count that triggers escalation.
        now: Timestamp override (for testing).
        remediator: Callable matching remediate_ollama signature.

    Returns:
        The escalation record written, or None if no escalation was triggered.
    """
    _now = now or datetime.now(timezone.utc)
    ts = _now.isoformat()

    health = _load_json(health_path)
    if not health:
        return None  # no health data yet

    # Always append current snapshot to history
    append_history(health, history_path=history_path, now=_now)

    # Only escalate on DEGRADED status
    if health.get("status") != "DEGRADED":
        return None

    history = _load_jsonl(history_path)
    consec = consecutive_degraded(history)

    if consec < threshold:
        return None

    current_failures: list[str] = health.get("failures", [])

    # Dedup: suppress if recent escalation for same failures
    last_esc = _load_json(escalation_path)
    if _is_recent_duplicate(last_esc, current_failures, _now):
        record: dict[str, Any] = {
            "ts": ts,
            "trigger": {"consecutive": consec, "failures": current_failures},
            "action": "suppressed_recent",
            "success": None,
            "recheck_ok": None,
            "needs_human": False,
        }
        _atomic_write(escalation_path, record)
        _append_jsonl(escalation_hist_path, record)
        return record

    has_ollama_failure = "ollama_doctor" in current_failures

    if has_ollama_failure:
        # Run remediation
        remediation = remediator()
        action = remediation.get("action", "restart_ollama")
        rem_success = remediation.get("success", False)
        rem_detail = remediation.get("detail", "")

        # Re-check: run ollama_doctor.py --ensure --quiet
        recheck_ok = False
        try:
            doctor_path = _SCRIPTS / "ollama_doctor.py"
            result = subprocess.run(
                [sys.executable, str(doctor_path), "--ensure", "--quiet"],
                capture_output=True,
                timeout=210,
                creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            recheck_ok = result.returncode == 0
        except Exception:
            recheck_ok = False

        needs_human = not (rem_success and recheck_ok)
        record = {
            "ts": ts,
            "trigger": {"consecutive": consec, "failures": current_failures},
            "action": action,
            "success": rem_success,
            "recheck_ok": recheck_ok,
            "needs_human": needs_human,
            "detail": rem_detail,
        }
    else:
        # Unknown failure — cannot auto-remediate, flag for human
        record = {
            "ts": ts,
            "trigger": {"consecutive": consec, "failures": current_failures},
            "action": "none",
            "success": None,
            "recheck_ok": None,
            "needs_human": True,
            "detail": "non-ollama failures require human investigation",
        }

    _atomic_write(escalation_path, record)
    _append_jsonl(escalation_hist_path, record)
    return record


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _status(
    health_path: Path = _HEALTH_PATH,
    history_path: Path = _HISTORY_PATH,
    escalation_path: Path = _ESCALATION_PATH,
) -> None:
    """Print current health status and last escalation."""
    health = _load_json(health_path)
    history = _load_jsonl(history_path)
    last_esc = _load_json(escalation_path)

    consec = consecutive_degraded(history)
    print(f"Current health  : {health.get('status', 'unknown')} "
          f"({health.get('last_run', '?')})", flush=True)
    print(f"Failures        : {health.get('failures', [])}", flush=True)
    print(f"Consecutive DEGRADED: {consec}", flush=True)

    if last_esc:
        print(f"\nLast escalation : {last_esc.get('ts', '?')}", flush=True)
        print(f"  action        : {last_esc.get('action', '?')}", flush=True)
        print(f"  success       : {last_esc.get('success')}", flush=True)
        print(f"  recheck_ok    : {last_esc.get('recheck_ok')}", flush=True)
        print(f"  needs_human   : {last_esc.get('needs_human')}", flush=True)
        trig = last_esc.get("trigger", {})
        print(f"  trigger       : {trig}", flush=True)
    else:
        print("\nLast escalation : none recorded", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true",
                    help="Print current health + last escalation and exit")
    args = ap.parse_args()

    if args.status:
        _status()
        sys.exit(0)

    # Default: run escalation logic (never raise, always exit 0)
    try:
        result = run_escalation()
        if result:
            print(f"[escalation] action={result.get('action')} "
                  f"needs_human={result.get('needs_human')}", flush=True)
    except Exception as exc:
        print(f"[escalation] ERROR: {exc}", file=sys.stderr, flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
