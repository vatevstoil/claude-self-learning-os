#!/usr/bin/env python3
"""roi_tracker.py — Agentic OS ROI layer (Gap 2).

Jack Roberts' framing: "we can't know ROI unless we know what your time is worth."
This adds the VALUE side to the cost side that stage3_dreaming already computes.

Honest by design: it only counts automation runs that actually SUCCEEDED (rc=0 in
automation.log) in the window, times a configurable minutes-saved-per-run estimate.
No fabricated skill-invocation counts. All assumptions live in an editable config.

    time_saved = Σ(successful_runs × minutes_saved_per_run)
    value      = time_saved_hours × hourly_value
    net_roi    = value − (weekly share of subscription cost)

Config (auto-created on first run): ~/.claude/roi-config.json
Output: ~/.claude/logs/roi.json  (+ stdout summary; dreaming reads roi.json)

Never raises. Safe for the scheduler.

Usage:
    python roi_tracker.py            # last 7 days
    python roi_tracker.py --days 30
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

CLAUDE = Path.home() / ".claude"
LOGS = CLAUDE / "logs"
AUTOMATION_LOG = LOGS / "automation.log"
CONFIG = CLAUDE / "roi-config.json"
ROI_OUT = LOGS / "roi.json"

# Defaults — EDIT roi-config.json to match your reality.
DEFAULT_CONFIG = {
    "_note": "ROI assumptions — edit freely. hourly_value = what an hour of your time is worth.",
    "hourly_value": 50,
    "currency": "USD",
    "subscription_monthly": 200,
    "minutes_saved_per_run": {
        "wiki_freshness_check.py": 8,
        "learning_promoter.py": 15,
        "wiki_lint.py": 8,
        "promotion_auto.py": 20,
        "auto_graphify.py": 30,
        "super_graph_regen.py": 15,
        "cross_project_promoter.py": 12,
        "agentic_os_registry.py": 15,
        "selfreg_monitor.py": 8,
        "pinecone_cleanup_expired.py": 8,
        "ai_video_check_new.py": 8,
        "stage3_dreaming.py": 30,
        "skills_audit.py": 20,
    },
    "default_minutes_per_unknown_run": 5,
}


def load_config() -> dict:
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
            # merge missing keys from defaults (forward-compatible)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    CONFIG.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(DEFAULT_CONFIG)


def count_successful_runs(days: int) -> Counter:
    """Count '<script>.py: rc=0' lines within the window. freshness rc=1 is semantic
    (stale found) → counted as success too."""
    runs: Counter = Counter()
    if not AUTOMATION_LOG.exists():
        return runs
    cutoff = datetime.now() - timedelta(days=days)
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    rc_re = re.compile(r"(\w+\.py):\s*rc=(\d+)")
    for line in AUTOMATION_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        m = ts_re.match(line)
        if m:
            try:
                if datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") < cutoff:
                    continue
            except ValueError:
                pass
        r = rc_re.search(line)
        if r:
            script, rc = r.group(1), int(r.group(2))
            if rc == 0 or (script == "wiki_freshness_check.py" and rc == 1):
                runs[script] += 1
    return runs


def compute(days: int) -> dict:
    cfg = load_config()
    runs = count_successful_runs(days)
    mins_map = cfg.get("minutes_saved_per_run", {})
    default_min = cfg.get("default_minutes_per_unknown_run", 5)

    breakdown = []
    total_minutes = 0
    for script, count in runs.most_common():
        per = mins_map.get(script, default_min)
        saved = count * per
        total_minutes += saved
        breakdown.append({"automation": script, "runs": count,
                          "min_per_run": per, "minutes_saved": saved})

    hours = round(total_minutes / 60, 1)
    hourly = cfg.get("hourly_value", 50)
    value = round(hours * hourly, 2)
    weekly_sub = round(cfg.get("subscription_monthly", 200) / 4.33, 2)
    # scale subscription cost to the window
    window_sub = round(weekly_sub * (days / 7), 2)
    net = round(value - window_sub, 2)
    cur = cfg.get("currency", "USD")

    return {
        "window_days": days,
        "computed": datetime.now().isoformat(timespec="seconds"),
        "currency": cur,
        "hourly_value": hourly,
        "automation_runs": sum(runs.values()),
        "time_saved_hours": hours,
        "value_of_time": value,
        "subscription_cost_window": window_sub,
        "net_roi": net,
        "roi_multiple": round(value / window_sub, 1) if window_sub else None,
        "breakdown": breakdown,
        "note": "Estimate from successful automation runs × configurable minutes saved. Edit ~/.claude/roi-config.json.",
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Agentic OS ROI tracker.")
    p.add_argument("--days", type=int, default=7)
    args = p.parse_args()
    try:
        roi = compute(args.days)
        LOGS.mkdir(parents=True, exist_ok=True)
        ROI_OUT.write_text(json.dumps(roi, ensure_ascii=False, indent=2), encoding="utf-8")
        cur = roi["currency"]
        print(f"roi_tracker: {roi['automation_runs']} runs → {roi['time_saved_hours']}h saved "
              f"≈ {cur} {roi['value_of_time']} | sub {cur} {roi['subscription_cost_window']} "
              f"| net {cur} {roi['net_roi']} ({roi['roi_multiple']}x)")
    except Exception as e:
        print(f"roi_tracker error (suppressed): {e}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
