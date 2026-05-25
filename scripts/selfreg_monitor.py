#!/usr/bin/env python3
"""selfreg_monitor.py — Watch the self-regulation layer and detect degradation.

"Who watches the watchers." The ecosystem now self-regulates; this monitor
checks that the regulation itself is healthy, tracks trends over time, and
emits ONE digest the human actually sees.

Health components (each 0-100, weighted):
    cron      — all 3 scheduled tasks registered & enabled
    runs      — daily/weekly ran within their expected window
    errors    — no real script failures in the last weekly block
    freshness — share of active wikis that are fresh
    lint      — average wiki contract compliance

Outputs:
    ~/.claude/logs/selfreg-health.json   — current snapshot
    ~/.claude/logs/selfreg-history.jsonl — one line per run (trend)
    ~/.claude/logs/selfreg-digest.txt    — compact, for SessionStart

Compares to the previous history entry and flags regressions. Never raises.

Usage:
    python selfreg_monitor.py
    python selfreg_monitor.py --print   # also print digest to stdout
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

CLAUDE = Path.home() / ".claude"
LOGS = CLAUDE / "logs"
AUTOMATION_LOG = LOGS / "automation.log"
FRESHNESS = LOGS / "freshness.json"
WIKI_LINT = LOGS / "wiki-lint.json"
PENDING = LOGS / "promotions-pending.md"
HEALTH_OUT = LOGS / "selfreg-health.json"
HISTORY_OUT = LOGS / "selfreg-history.jsonl"
DIGEST_OUT = LOGS / "selfreg-digest.txt"
SETTINGS = CLAUDE / "settings.json"
DASHBOARD = CLAUDE / "scripts" / "agentic_os_dashboard.py"
DISPATCHER_HEALTH = LOGS / "health.json"

EXPECTED_TASKS = ["ClaudeAutomation_Daily", "ClaudeAutomation_Weekly", "ClaudeAutomation_Dreaming"]
# Scripts whose non-zero exit is SEMANTIC, not a failure (freshness exits 1 when stale)
SEMANTIC_NONZERO = {"wiki_freshness_check.py"}
# External binaries the ecosystem/skills depend on. (key: tool, value: what needs it)
EXPECTED_TOOLS = {"python": "core", "git": "core", "node": "gitnexus hook",
                  "ffmpeg": "watch skill", "yt-dlp": "watch skill"}
# Secret patterns that must NEVER appear in the permissions allow-list (the leak
# vector — cached debug commands). The env block may hold config keys by design,
# so we scope this scan to permissions only.
SECRET_RES = [
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    re.compile(r"pcsk_[A-Za-z0-9_]{20,}"),                       # Pinecone
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                          # OpenAI-style
    re.compile(r"AIza[A-Za-z0-9_-]{30,}"),                       # Google
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"),                 # bearer tokens
]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _json(path: Path) -> dict:
    try:
        return json.loads(_read(path) or "{}")
    except Exception:
        return {}


# --------------------------------------------------------------------------- cron
def check_cron() -> tuple[int, list[str]]:
    issues: list[str] = []
    try:
        out = subprocess.run(
            ["schtasks", "/query", "/fo", "LIST"],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        ).stdout or ""
    except Exception as e:
        return 0, [f"cron query failed: {e}"]
    for task in EXPECTED_TASKS:
        if task not in out:
            issues.append(f"missing scheduled task: {task}")
    score = round(100 * (len(EXPECTED_TASKS) - len(issues)) / len(EXPECTED_TASKS))
    return score, issues


# --------------------------------------------------------------------------- runs
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _last_ts(text: str, marker: str) -> datetime | None:
    last = None
    for line in text.splitlines():
        if marker in line:
            m = _TS_RE.match(line)
            if m:
                try:
                    last = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
    return last


def check_runs(text: str) -> tuple[int, list[str]]:
    issues: list[str] = []
    now = datetime.now()
    daily = _last_ts(text, "DAILY RUN END")
    weekly = _last_ts(text, "WEEKLY RUN END")
    score = 100
    if daily is None or (now - daily) > timedelta(days=2):
        issues.append("daily run not seen in last 48h")
        score -= 50
    if weekly is None or (now - weekly) > timedelta(days=8):
        issues.append("weekly run not seen in last 8 days")
        score -= 50
    return max(0, score), issues


# --------------------------------------------------------------------------- errors
def check_errors(text: str) -> tuple[int, list[str]]:
    """Inspect the LAST weekly block for real script failures."""
    issues: list[str] = []
    starts = [i for i, ln in enumerate(text.splitlines()) if "WEEKLY RUN START" in ln]
    lines = text.splitlines()
    if not starts:
        return 100, []  # no weekly block yet — neutral
    block = lines[starts[-1]:]
    for ln in block:
        m = re.search(r"(\w+\.py):\s*rc=(\d+)", ln)
        if m:
            script, rc = m.group(1), int(m.group(2))
            if rc != 0 and script not in SEMANTIC_NONZERO:
                issues.append(f"{script} rc={rc}")
        if "TIMEOUT" in ln:
            issues.append(ln.split("]", 1)[-1].strip()[:80])
        if "[ERROR]" in ln or "[CRITICAL]" in ln:
            issues.append(ln.split("]", 1)[-1].strip()[:80])
    score = 100 if not issues else max(0, 100 - 25 * len(issues))
    return score, issues


# --------------------------------------------------------------------------- freshness / lint
def check_freshness() -> tuple[int, dict]:
    d = _json(FRESHNESS)
    total = d.get("total", 0)
    stale = d.get("stale_count", 0)
    if not total:
        return 100, {"stale": 0, "total": 0}
    fresh_ratio = (total - stale) / total
    return round(100 * fresh_ratio), {"stale": stale, "total": total}


def check_lint() -> tuple[int, dict]:
    d = _json(WIKI_LINT)
    return int(d.get("avg_score", 100)), {"avg_score": d.get("avg_score"), "wikis": d.get("active_wikis")}


def pending_count() -> int:
    return len(re.findall(r"^- \[ \] Candidate", _read(PENDING), flags=re.MULTILINE))


# --------------------------------------------------------------------------- hygiene
def check_hygiene() -> tuple[int, list[str]]:
    """Catch dependency/PATH regressions and security regressions automatically.
    Each finding costs 20 points; binary problems, not gradual."""
    issues: list[str] = []
    # 1. Dependency / PATH — tools the system relies on must resolve
    for tool, why in EXPECTED_TOOLS.items():
        if shutil.which(tool) is None:
            issues.append(f"{tool} not on PATH ({why})")
    # 2. Security — any secret in the permissions allow-list (the real leak vector;
    #    env keys are intentional config so we don't scan them).
    try:
        allow = _json(SETTINGS).get("permissions", {}).get("allow", [])
        blob = "\n".join(allow if isinstance(allow, list) else [])
        if any(rx.search(blob) for rx in SECRET_RES):
            issues.append("secret/token leaked into settings.json permissions")
    except Exception:
        pass
    # 3. Security — dashboard CSRF guard must still be present (regression of the fix)
    if DASHBOARD.exists() and "_csrf_ok" not in _read(DASHBOARD):
        issues.append("dashboard CSRF guard missing")
    score = 100 if not issues else max(0, 100 - 20 * len(issues))
    return score, issues


# --------------------------------------------------------------------------- dispatcher
def check_dispatcher() -> tuple[int, list[str]]:
    """Check dispatcher health.json for recent run status."""
    issues: list[str] = []
    d = _json(DISPATCHER_HEALTH)
    if not d:
        return 70, ["health.json missing (dispatcher never ran or too old)"]

    status = d.get("status", "UNKNOWN")
    failures = d.get("failures", [])
    last_run_raw = d.get("last_run", "")

    # Check recency — health.json should be written at least daily
    try:
        last_run = datetime.fromisoformat(last_run_raw)
        age_hours = (datetime.now() - last_run).total_seconds() / 3600
        if age_hours > 50:  # 48h + 2h slack
            issues.append(f"health.json stale ({int(age_hours)}h old — dispatcher not running?)")
    except Exception:
        issues.append("health.json has invalid last_run timestamp")

    if status == "DEGRADED":
        for f in failures[:3]:
            issues.append(f"dispatcher: {f} failed last run")

    score = 100 if not issues else (70 if status == "OK" else max(0, 100 - 30 * len(issues)))
    return score, issues


# --------------------------------------------------------------------------- main
def grade(score: int) -> str:
    return ("A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70
            else "D" if score >= 60 else "F")


def run(do_print: bool) -> dict:
    text = _read(AUTOMATION_LOG)
    cron_s, cron_i = check_cron()
    runs_s, runs_i = check_runs(text)
    err_s, err_i = check_errors(text)
    fresh_s, fresh_d = check_freshness()
    lint_s, lint_d = check_lint()
    hyg_s, hyg_i = check_hygiene()
    disp_s, disp_i = check_dispatcher()

    # Weighted overall: errors + cron + hygiene are most critical
    overall = round(
        0.20 * cron_s + 0.15 * runs_s + 0.20 * err_s
        + 0.13 * fresh_s + 0.07 * lint_s + 0.15 * hyg_s + 0.10 * disp_s
    )

    snapshot = {
        "date": date.today().isoformat(),
        "checked": datetime.now().isoformat(timespec="seconds"),
        "overall": overall,
        "grade": grade(overall),
        "components": {
            "cron": cron_s, "runs": runs_s, "errors": err_s,
            "freshness": fresh_s, "lint": lint_s, "hygiene": hyg_s,
            "dispatcher": disp_s,
        },
        "issues": {"cron": cron_i, "runs": runs_i, "errors": err_i, "hygiene": hyg_i,
                   "dispatcher": disp_i},
        "freshness": fresh_d,
        "lint": lint_d,
        "pending_promotions": pending_count(),
    }

    # Trend: compare to previous history entry
    prev = None
    if HISTORY_OUT.exists():
        hist_lines = [l for l in _read(HISTORY_OUT).splitlines() if l.strip()]
        if hist_lines:
            try:
                prev = json.loads(hist_lines[-1])
            except Exception:
                prev = None
    regressions: list[str] = []
    if prev:
        if snapshot["overall"] < prev.get("overall", 100) - 5:
            regressions.append(f"overall {prev['overall']}→{snapshot['overall']}")
        if fresh_d["stale"] > prev.get("freshness", {}).get("stale", 0):
            regressions.append(f"stale {prev.get('freshness',{}).get('stale','?')}→{fresh_d['stale']}")
        pl, cl = lint_d.get("avg_score"), prev.get("lint", {}).get("avg_score")
        if isinstance(pl, (int, float)) and isinstance(cl, (int, float)) and pl < cl - 3:
            regressions.append(f"lint {cl}→{pl}")
    snapshot["regressions"] = regressions

    # Persist
    HEALTH_OUT.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    with HISTORY_OUT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    # Cap history growth — keep last 365 snapshots
    try:
        hl = [l for l in _read(HISTORY_OUT).splitlines() if l.strip()]
        if len(hl) > 365:
            HISTORY_OUT.write_text("\n".join(hl[-365:]) + "\n", encoding="utf-8")
    except OSError:
        pass

    # Digest (compact, for SessionStart) — hygiene issues are security/deps, surface first
    all_issues = hyg_i + cron_i + runs_i + err_i + disp_i
    digest_lines = [
        f"selfreg health: {snapshot['grade']} ({overall}/100) | "
        f"stale {fresh_d['stale']}/{fresh_d['total']} | lint {lint_d.get('avg_score')}% | "
        f"pending {snapshot['pending_promotions']}"
    ]
    if all_issues:
        digest_lines.append("ISSUES: " + " · ".join(all_issues[:5]))
    if regressions:
        digest_lines.append("REGRESSION: " + " · ".join(regressions))
    DIGEST_OUT.write_text("\n".join(digest_lines) + "\n", encoding="utf-8")

    if do_print:
        print("\n".join(digest_lines))
        print(json.dumps(snapshot["components"], ensure_ascii=False))
    return snapshot


def main() -> None:
    p = argparse.ArgumentParser(description="Monitor the self-regulation layer.")
    p.add_argument("--print", action="store_true", dest="do_print")
    args = p.parse_args()
    try:
        run(args.do_print)
    except Exception as e:
        try:
            DIGEST_OUT.write_text(f"selfreg monitor error: {e}\n", encoding="utf-8")
        except OSError:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
