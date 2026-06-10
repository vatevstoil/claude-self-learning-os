#!/usr/bin/env python3
"""promotion_auto.py — Confidence-gated autonomous promotion actuator.

Reads ~/.claude/logs/promotions-pending.md and applies the SAFE ones
automatically (Tier 1), leaving uncertain ones queued for human review (Tier 2).

Tier 1 (auto-apply, with backup + dedup + provenance):
    confidence >= AUTO_CONFIDENCE  AND  project_count >= AUTO_MIN_PROJECTS
Tier 2 (queue for human):
    everything else — surfaced at SessionStart via pending-summary.txt

Reuses promotion_apply.apply_candidate (which has the dedup guard).
Never raises; safe to call from the scheduler.

Usage:
    python promotion_auto.py                 # apply Tier 1, queue the rest
    python promotion_auto.py --dry-run       # decide only, change nothing
    python promotion_auto.py --min-confidence 0.8 --min-projects 3
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import promotion_apply as pa

PENDING_FILE = Path.home() / ".claude" / "logs" / "promotions-pending.md"
LOG_FILE = Path.home() / ".claude" / "logs" / "promotion-auto.log"

AUTO_CONFIDENCE = 0.75
AUTO_MIN_PROJECTS = 3

_CONF_RE = re.compile(r"\*\*Confidence:\*\*\s*([\d.]+)")
_PROJ_RE = re.compile(r"\((\d+)\s+projects?\)")
_STATUS_OPEN_RE = re.compile(r"^- \[ \] Candidate (\d+)\b", re.MULTILINE)


def _log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(msg)


def _candidate_meta(body: str) -> tuple[float, int]:
    conf_m = _CONF_RE.search(body)
    proj_m = _PROJ_RE.search(body)
    conf = float(conf_m.group(1)) if conf_m else 0.0
    projects = int(proj_m.group(1)) if proj_m else 0
    return conf, projects


def run(min_conf: float, min_proj: int, dry_run: bool) -> dict:
    result = {"applied": [], "queued": [], "total": 0}
    if not PENDING_FILE.exists():
        _log("promotion_auto: no pending file, nothing to do")
        return result

    text = PENDING_FILE.read_text(encoding="utf-8")
    # Only consider candidates still marked open: "- [ ] Candidate N"
    open_nums = {int(n) for n in _STATUS_OPEN_RE.findall(text)}
    candidates = pa.parse_candidates(text)
    result["total"] = len(open_nums)

    for cand in candidates:
        num = cand["num"]
        if num not in open_nums:
            continue  # already applied/skipped
        conf, projects = _candidate_meta(cand["body"])
        tier1 = conf >= min_conf and projects >= min_proj
        if tier1:
            if dry_run:
                _log(f"[DRY] WOULD AUTO-APPLY Candidate {num} (conf={conf}, proj={projects}): {cand['title'][:60]}")
                result["applied"].append(num)
            else:
                try:
                    pa.apply_candidate(num)  # has dedup + backup + provenance
                    _log(f"AUTO-APPLIED Candidate {num} (conf={conf}, proj={projects}): {cand['title'][:60]}")
                    result["applied"].append(num)
                except SystemExit as e:
                    _log(f"SKIP Candidate {num}: apply failed ({e})")
                    result["queued"].append(num)
                except Exception as e:
                    _log(f"SKIP Candidate {num}: error ({e})")
                    result["queued"].append(num)
        else:
            _log(f"QUEUED Candidate {num} for human (conf={conf}, proj={projects}): {cand['title'][:60]}")
            result["queued"].append(num)

    _log(f"promotion_auto: {len(result['applied'])} auto-applied, "
         f"{len(result['queued'])} queued for review (of {result['total']} open)")
    return result


def main() -> None:
    p = argparse.ArgumentParser(description="Confidence-gated autonomous promotion.")
    p.add_argument("--min-confidence", type=float, default=AUTO_CONFIDENCE)
    p.add_argument("--min-projects", type=int, default=AUTO_MIN_PROJECTS)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    try:
        run(args.min_confidence, args.min_projects, args.dry_run)
    except Exception as e:
        _log(f"promotion_auto FATAL (suppressed): {e}")
    sys.exit(0)


if __name__ == "__main__":
    main()
