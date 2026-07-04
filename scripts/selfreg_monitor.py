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
PENDING_SAVES = LOGS / "pending-saves.jsonl"  # quota-outage fallback queue (pinecone.py)
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
    """Check that each expected scheduled task exists using /tn direct lookup.

    Rationale for per-task /tn queries instead of a bulk LIST parse:
      - Bulk LIST output on Bulgarian Windows can be truncated or return
        zero bytes when schtasks is called from pythonw.exe under Task
        Scheduler while another task is active (transient lock).
      - /tn <name> returns rc=0 if the task exists, rc=1 if not — no
        locale-sensitive text parsing required at all.
      - Retry once on failure to absorb transient COM/RPC blips.
    """
    issues: list[str] = []
    for task in EXPECTED_TASKS:
        found = False
        for _attempt in range(2):  # retry once for transient failures
            try:
                r = subprocess.run(
                    ["schtasks", "/query", "/fo", "LIST", "/tn", task],
                    capture_output=True, text=True, timeout=15,
                    encoding="utf-8", errors="replace",
                )
                if r.returncode == 0:
                    found = True
                    break
            except Exception:
                pass
        if not found:
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
    # Dreaming is scheduled weekly (Sat 11:00) — same 8-day staleness budget as
    # weekly. Without this check a broken dreaming pipeline dies silently while
    # daily/weekly keep health green.
    dreaming = _last_ts(text, "DREAMING RUN END")
    score = 100
    if daily is None or (now - daily) > timedelta(days=2):
        issues.append("daily run not seen in last 48h")
        score -= 50
    if weekly is None or (now - weekly) > timedelta(days=8):
        issues.append("weekly run not seen in last 8 days")
        score -= 25
    if dreaming is None or (now - dreaming) > timedelta(days=8):
        issues.append("dreaming run not seen in last 8 days")
        score -= 25
    return max(0, score), issues


# --------------------------------------------------------------------------- errors
_RC_RE = re.compile(r"(\w[\w.-]*\.py):\s*rc=(\d+)")
_TS_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def check_errors(
    text: str,
    *,
    window_days: int = 7,
    now: datetime | None = None,
) -> tuple[int, list[str]]:
    """Detect real script failures using a 7-day sliding window with last-state-wins.

    Two bugs fixed from the original:

    1. **Only last WEEKLY block**: the old code found WEEKLY RUN START markers and
       took only lines after the last one.  On systems that run daily-only (no
       weekly cron yet), or when the weekly block predates the 7-day window,
       this returned (100, []) — hiding real failures.  Now we use a 7-day
       time-window over ALL log lines regardless of block type.

    2. **History counted, not current state**: if a script failed on day 1 then
       succeeded on day 2, the failure was still counted.  Fix: for each script
       track the LAST seen rc.  A fixed script (last rc=0) is NOT counted as a
       failure.  Same for TIMEOUT and [ERROR]/[CRITICAL] lines: if the script
       ran successfully after the bad line, the bad line is cleared.

    semantic_merge (and other special scripts) remain "unverified" until they
    show a confirmed rc=0 — they are not in SEMANTIC_NONZERO so non-zero is an
    error, but if their last run was clean they are cleared.
    """
    _now = now or datetime.now()
    cutoff = _now - timedelta(days=window_days)
    # A one-off [ERROR] that neither recurs nor is recent (within this horizon)
    # is treated as stale/resolved: a single historical error must not pin the
    # health grade down for the full 7-day window. [CRITICAL], recurring, or
    # recent errors are always surfaced (see per-script evaluation below).
    stale_error_after = timedelta(hours=48)

    lines = text.splitlines()

    # Per-script tracking: last_rc, last_timeout, last_error_line
    # key = script name, value = dict with 'rc', 'timeout', 'error'
    script_state: dict[str, dict] = {}

    for ln in lines:
        # Parse timestamp to apply the 7-day window
        m_ts = _TS_RE.match(ln)
        if m_ts:
            try:
                ts = datetime.strptime(m_ts.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                ts = _now  # can't parse → include (safe default)
        else:
            ts = _now  # no timestamp → include

        if ts < cutoff:
            continue  # outside the 7-day window

        # rc lines
        m_rc = _RC_RE.search(ln)
        if m_rc:
            script, rc = m_rc.group(1), int(m_rc.group(2))
            state = script_state.setdefault(script, {"rc": None, "timeout": False, "error": None})
            state["rc"] = rc
            if rc == 0:
                # Successful run clears previous timeout/error for this script
                state["timeout"] = False
                state["error"] = None
            continue

        # TIMEOUT lines (associate with the script name if detectable)
        if "TIMEOUT" in ln:
            # Try to extract script name from context like "Starting: foo.py"
            m_name = re.search(r"(\w[\w.-]*\.py)", ln)
            key = m_name.group(1) if m_name else f"__timeout_{ln[:30]}"
            state = script_state.setdefault(key, {"rc": None, "timeout": False, "error": None})
            state["timeout"] = True
            continue

        # [ERROR] / [CRITICAL] lines (not associated with a specific script —
        # free-form issues). Track occurrence count, most-recent timestamp, and
        # whether any occurrence was CRITICAL, so a single stale [ERROR] can be
        # distinguished from a recurring or recent one in the evaluation pass.
        if "[ERROR]" in ln or "[CRITICAL]" in ln:
            msg = ln.split("]", 1)[-1].strip()[:80]
            key = f"__log_{msg[:40]}"
            state = script_state.setdefault(
                key,
                {"rc": None, "timeout": False, "error": None,
                 "count": 0, "last_ts": None, "critical": False},
            )
            state["error"] = msg
            state["count"] = state.get("count", 0) + 1
            state["last_ts"] = ts
            if "[CRITICAL]" in ln:
                state["critical"] = True

    # Evaluate last-state per script
    issues: list[str] = []
    for script, state in script_state.items():
        if script.startswith("__timeout_"):
            if state["timeout"]:
                issues.append(script.replace("__timeout_", "TIMEOUT: ")[:80])
        elif script.startswith("__log_"):
            if not state.get("error"):
                continue
            # Surface only if CRITICAL, recurring (seen >1x), or recent — a
            # single, non-recurring, non-recent [ERROR] is treated as stale.
            last_ts = state.get("last_ts")
            recent = last_ts is not None and (_now - last_ts) <= stale_error_after
            if state.get("critical") or state.get("count", 0) >= 2 or recent:
                issues.append(state["error"])
        else:
            rc = state["rc"]
            if rc is not None and rc != 0 and script not in SEMANTIC_NONZERO:
                issues.append(f"{script} rc={rc}")
            if state["timeout"]:
                issues.append(f"TIMEOUT: {script}")

    # Embedding-quota backlog: queued saves = learnings not yet in recall (quota
    # outage). Surface it (and the remedy) so SessionStart shows the degradation.
    try:
        if PENDING_SAVES.exists():
            n = len([l for l in PENDING_SAVES.read_text(encoding="utf-8").splitlines() if l.strip()])
            if n:
                issues.append(f"{n} saves queued (Pinecone quota) — run pinecone.py replay-queue")
    except Exception:
        pass

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


# Critical hooks that MUST be registered for the live self-learning system to
# function. Maps hook event → substring its command block must contain.
EXPECTED_HOOKS = {
    "Stop": "auto_pinecone_save",       # session learnings → local_rag (MEMORY_BACKEND=local)
    "SessionStart": "session_start_brief",  # recall + suggestions at startup
}


def check_hooks() -> tuple[int, list[str]]:
    """Verify the self-learning hooks are registered in settings.json.

    Blind-spot guard: a migration/auto-strip once emptied ALL hooks, leaving
    the Stop-hook saver dead for ~2 days before anyone noticed. This check
    fails loudly if a critical hook's command block is missing or empty.
    """
    issues: list[str] = []
    d = _json(SETTINGS)
    if not d:
        return 50, ["settings.json unreadable — cannot verify hooks"]
    hooks = d.get("hooks", {}) or {}
    for evt, needle in EXPECTED_HOOKS.items():
        blocks = hooks.get(evt, []) or []
        cmds = [h.get("command", "") for b in blocks for h in (b.get("hooks", []) or [])]
        if not any(needle in c for c in cmds):
            issues.append(f"hook missing: {evt} → '{needle}' not registered (system degraded)")
    score = 100 if not issues else max(0, 100 - 50 * len(issues))
    return score, issues


def check_hook_runtime(summary: dict | None = None) -> tuple[int, list[str]]:
    """Score hook RUNTIME health from hook-telemetry.jsonl (7-day window).

    check_hooks() verifies registration only; this consumes the telemetry
    ledger that was previously write-only. Fail-open: telemetry loss must
    never break the health monitor itself.
    """
    issues: list[str] = []
    if summary is None:
        try:
            from hook_telemetry import summarize
            summary = summarize(window_days=7)
        except Exception:
            return 100, []
    score = 100
    fails: list[str] = []
    slows: list[str] = []
    for name, agg in sorted((summary or {}).items()):
        count = agg.get("count", 0) or 0
        if count < 10:
            continue  # too few runs to judge fairly
        rate = agg.get("error_rate", 0.0) or 0.0
        p95 = agg.get("p95_ms", 0.0) or 0.0
        if rate > 0.20:
            fails.append(f"hook {name}: {round(rate * 100)}% fail (n={count})")
            score -= 25
        if p95 > 15000:
            slows.append(f"hook {name}: p95 {round(p95 / 1000)}s (slow)")
            score -= 10
    # Fail-first ordering: issues[0] feeds the single always-surfaced 🚨 slot
    # in run() — a cosmetic slow hook must never mask a hard-failing one.
    issues = fails + slows
    return max(0, score), issues


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
    hooks_s, hooks_i = check_hooks()
    # Runtime health folds into the SAME hooks component — the weighted formula
    # below sums to 1.00 and must not gain a ninth weight.
    hook_rt_s, hook_rt_i = check_hook_runtime()
    hooks_s = min(hooks_s, hook_rt_s)
    hooks_i = hooks_i + hook_rt_i

    # Weighted overall (sums to 1.00): errors + cron + hooks + hygiene are most
    # critical — hooks are the live nervous system, weighted so an empty-hooks
    # outage drops the grade hard and surfaces immediately.
    overall = round(
        0.17 * cron_s + 0.13 * runs_s + 0.18 * err_s
        + 0.12 * fresh_s + 0.05 * lint_s + 0.13 * hyg_s + 0.10 * disp_s
        + 0.12 * hooks_s
    )

    snapshot = {
        "date": date.today().isoformat(),
        "checked": datetime.now().isoformat(timespec="seconds"),
        "overall": overall,
        "grade": grade(overall),
        "components": {
            "cron": cron_s, "runs": runs_s, "errors": err_s,
            "freshness": fresh_s, "lint": lint_s, "hygiene": hyg_s,
            "dispatcher": disp_s, "hooks": hooks_s,
        },
        "issues": {"cron": cron_i, "runs": runs_i, "errors": err_i, "hygiene": hyg_i,
                   "dispatcher": disp_i, "hooks": hooks_i},
        "freshness": fresh_d,
        "lint": lint_d,
        "pending_promotions": pending_count(),
        "open_incidents": len((_json(LOGS / "incidents.json")).get("open", []) or []),
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
    # Hook failure is critical infra — always surface at SessionStart regardless
    # of overall grade (an empty-hooks outage only drops grade to ~B, which the
    # startup digest would otherwise not show). Prepend to regressions, which the
    # SessionStart brief always surfaces.
    # Only HARD hook issues (missing registration / failing) earn the critical
    # banner — a merely slow hook as a permanent 🚨 desensitizes real alerts.
    hard_hooks = [i for i in hooks_i if "(slow)" not in i]
    if hard_hooks:
        regressions.insert(0, f"🚨 {hard_hooks[0]}")
    # Failed self-healing needs a human NOW — always surface at SessionStart.
    esc = _json(LOGS / "escalation.json")
    if esc.get("needs_human"):
        regressions.insert(0, f"🚨 self-healing failed: {esc.get('action','?')} "
                              f"(failures: {', '.join(esc.get('trigger',{}).get('failures',[])[:3])})")
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
        + (f" | 🚨 incidents {snapshot['open_incidents']}" if snapshot["open_incidents"] else "")
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
