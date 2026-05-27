#!/usr/bin/env python3
"""automation_dispatcher.py — Single entry point for scheduled automation.

Called by Windows Task Scheduler (daily/weekly).
Runs each task in a subprocess with timeout, captures errors,
NEVER throws (scheduler must always see exit 0).

Usage:
    python automation_dispatcher.py daily
    python automation_dispatcher.py weekly
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Force UTF-8 in all child processes — scheduled tasks run under pythonw.exe with
# no PYTHONIOENCODING, so a child printing emoji/Cyrillic to its pipe would crash
# on the cp1251 default. This guarantees utf-8 regardless of host environment.
_CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
              "CLAUDE_AUTOMATION_RUN": "1"}

SCRIPTS_DIR = Path(__file__).parent
LOGS_DIR = Path.home() / ".claude" / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOGS_DIR / "automation.log"
HEALTH_PATH = LOGS_DIR / "health.json"
FRESHNESS_OUT = LOGS_DIR / "freshness.json"
PROMOTIONS_OUT = LOGS_DIR / "promotions-pending.md"
CROSSLINKS_OUT = LOGS_DIR / "cross-links-pending.md"

from logging.handlers import RotatingFileHandler

_handler = RotatingFileHandler(
    str(LOG_FILE),
    maxBytes=5_242_880,  # 5 MB
    backupCount=3,
    encoding="utf-8",
)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log = logging.getLogger("dispatcher")
log.setLevel(logging.INFO)
if not log.handlers:
    log.addHandler(_handler)


def run_safe(script_path: Path, args: list[str], timeout: int = 300) -> bool:
    """Run a python script in a subprocess. Always returns; never raises."""
    name = script_path.name
    cmd = [sys.executable, str(script_path)] + args
    log.info("Starting: %s args=%s", name, args)
    try:
        result = subprocess.run(
            cmd,
            timeout=timeout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_CHILD_ENV,
        )
        log.info("%s: rc=%d stdout=%dB stderr=%dB",
                 name, result.returncode,
                 len(result.stdout or ""), len(result.stderr or ""))
        if result.returncode != 0:
            log.warning("%s stderr (first 300): %s",
                        name, (result.stderr or "")[:300])
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log.warning("%s: TIMEOUT after %ds", name, timeout)
        return False
    except Exception as exc:
        log.error("%s: ERROR %s", name, exc)
        return False


def write_health(run_type: str, results: dict[str, bool]) -> None:
    """Write automation health summary to health.json.

    Args:
        run_type: "daily" | "weekly" | "dreaming"
        results: Mapping of task_name -> success bool from run_safe calls.
    """
    failures = [name for name, ok in results.items() if not ok]
    data = {
        "last_run": datetime.now().isoformat(timespec="seconds"),
        "run_type": run_type,
        "tasks_total": len(results),
        "tasks_failed": len(failures),
        "failures": failures,
        "status": "OK" if not failures else "DEGRADED",
    }
    try:
        HEALTH_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        log.error("write_health failed: %s", exc)
    if failures:
        log.warning("Health DEGRADED — failed tasks: %s", failures)
    else:
        log.info("Health OK — all %d tasks succeeded", len(results))


def write_pending_summary():
    """Write a compact summary file for Phase 0 reads."""
    summary_path = LOGS_DIR / "pending-summary.txt"

    freshness_line = "freshness: ?"
    if FRESHNESS_OUT.exists():
        try:
            data = json.loads(FRESHNESS_OUT.read_text(encoding="utf-8"))
            freshness_line = f"freshness: {data.get('stale_count', 0)} stale of {data.get('total', 0)}"
        except Exception:
            pass

    promo_line = "promotions: ?"
    if PROMOTIONS_OUT.exists():
        try:
            text = PROMOTIONS_OUT.read_text(encoding="utf-8")
            pending = len(re.findall(r"^- \[ \] Candidate", text, flags=re.MULTILINE))
            applied = len(re.findall(r"^- \[x\] Candidate", text, flags=re.MULTILINE))
            skipped = len(re.findall(r"^- \[~\] Candidate", text, flags=re.MULTILINE))
            promo_line = f"promotions: {pending} pending, {applied} applied, {skipped} skipped"
        except Exception:
            pass

    crosslinks_line = "crosslinks: ?"
    if CROSSLINKS_OUT.exists():
        try:
            text = CROSSLINKS_OUT.read_text(encoding="utf-8")
            suggestions = len(re.findall(r"^## Suggestion ", text, flags=re.MULTILINE))
            crosslinks_line = f"crosslinks: {suggestions} suggestions"
        except Exception:
            pass

    # NotebookLM new sources count
    notebook_line = "notebooks: ?"
    nb_path = LOGS_DIR / "notebook-new-sources.txt"
    if nb_path.exists():
        try:
            text = nb_path.read_text(encoding="utf-8")
            m = re.search(r"^TOTAL_NEW:\s*(\d+)", text, flags=re.MULTILINE)
            if m:
                count = int(m.group(1))
                notebook_line = f"notebooks: {count} new sources awaiting ingest"
            else:
                notebook_line = "notebooks: 0 new"
        except Exception:
            pass

    summary_path.write_text(
        f"# pending-summary.txt — auto-generated\n"
        f"# Updated: {datetime.now().isoformat(timespec='seconds')}\n"
        f"{freshness_line}\n"
        f"{promo_line}\n"
        f"{crosslinks_line}\n"
        f"{notebook_line}\n",
        encoding="utf-8",
    )
    log.info("Wrote pending-summary.txt")


def daily_tasks():
    log.info("=== DAILY RUN START ===")
    results: dict[str, bool] = {}
    results["wiki_freshness"] = run_safe(SCRIPTS_DIR / "wiki_freshness_check.py",
             ["--out", str(FRESHNESS_OUT), "--threshold", "14"])
    write_pending_summary()
    results["habit_miner"] = run_safe(SCRIPTS_DIR / "habit_miner.py", ["--days", "2"], timeout=180)
    results["habit_accept_queue"] = run_safe(SCRIPTS_DIR / "habit_to_skill.py", ["--process-accepted"], timeout=60)
    results["boris_apply"] = run_safe(SCRIPTS_DIR / "boris_draft.py", ["--process-accepted"], timeout=60)
    results["selfreg_monitor"] = run_safe(SCRIPTS_DIR / "selfreg_monitor.py", [])
    write_health("daily", results)
    log.info("=== DAILY RUN END ===")


def weekly_tasks():
    log.info("=== WEEKLY RUN START ===")
    results: dict[str, bool] = {}
    results["wiki_freshness"] = run_safe(SCRIPTS_DIR / "wiki_freshness_check.py",
             ["--out", str(FRESHNESS_OUT), "--threshold", "14"])
    results["learning_promoter"] = run_safe(SCRIPTS_DIR / "learning_promoter.py",
             ["--out", str(PROMOTIONS_OUT)])
    results["wiki_lint"] = run_safe(SCRIPTS_DIR / "wiki_lint.py", [])
    results["promotion_auto"] = run_safe(SCRIPTS_DIR / "promotion_auto.py", [])
    results["auto_graphify"] = run_safe(SCRIPTS_DIR / "auto_graphify.py", ["--max", "5"])
    results["super_graph_regen"] = run_safe(SCRIPTS_DIR / "super_graph_regen.py", [])
    results["cross_project_promoter"] = run_safe(SCRIPTS_DIR / "cross_project_promoter.py",
             ["--out", str(CROSSLINKS_OUT), "--since-days", "7"])
    results["ai_video_check"] = run_safe(SCRIPTS_DIR / "ai_video_check_new.py", [])
    results["pinecone_cleanup"] = run_safe(SCRIPTS_DIR / "pinecone_cleanup_expired.py",
             ["--all-namespaces", "--apply"])
    results["semantic_merge"] = run_safe(SCRIPTS_DIR / "semantic_merge.py",
             ["--all-namespaces", "--apply", "--threshold", "0.95"], timeout=300)
    results["salience"] = run_safe(SCRIPTS_DIR / "salience.py", ["--days", "7"], timeout=120)
    write_pending_summary()
    results["improvement_queue"] = run_safe(SCRIPTS_DIR / "self_improvement_queue.py", [], timeout=60)
    results["knowledge_sync"] = run_safe(SCRIPTS_DIR / "knowledge_sync.py", [])
    results["agentic_os_registry"] = run_safe(SCRIPTS_DIR / "agentic_os_registry.py", [])
    results["roi_tracker"] = run_safe(SCRIPTS_DIR / "roi_tracker.py", ["--days", "30"])
    results["selfreg_monitor"] = run_safe(SCRIPTS_DIR / "selfreg_monitor.py", [])
    write_health("weekly", results)
    log.info("=== WEEKLY RUN END ===")


def dreaming_tasks():
    """Stage 3 self-learning analysis. Inspired by Jack Roberts Claude OS Dreaming.
    See: J:\\Obsidian Resurch\\Claude Code Resurch\\wiki\\summaries\\Claude-OS-Dashboard-Jack.md
    """
    log.info("=== DREAMING RUN START ===")
    results: dict[str, bool] = {}
    results["stage3_dreaming"] = run_safe(SCRIPTS_DIR / "stage3_dreaming.py", ["--days", "7"], timeout=600)
    results["skills_audit"] = run_safe(SCRIPTS_DIR / "skills_audit.py", [], timeout=120)
    results["habit_miner"] = run_safe(SCRIPTS_DIR / "habit_miner.py", ["--days", "14"], timeout=300)
    results["habit_ledger"] = run_safe(SCRIPTS_DIR / "habit_ledger.py", [], timeout=120)
    results["habit_to_skill"] = run_safe(SCRIPTS_DIR / "habit_to_skill.py", [], timeout=60)
    results["habit_accept_queue"] = run_safe(SCRIPTS_DIR / "habit_to_skill.py", ["--process-accepted"], timeout=60)
    results["boris_draft"] = run_safe(SCRIPTS_DIR / "boris_draft.py", [], timeout=60)
    results["boris_apply"] = run_safe(SCRIPTS_DIR / "boris_draft.py", ["--process-accepted"], timeout=60)
    results["improvement_queue"] = run_safe(SCRIPTS_DIR / "self_improvement_queue.py", [], timeout=60)
    results["effectiveness_tracker"] = run_safe(SCRIPTS_DIR / "effectiveness_tracker.py", [], timeout=60)
    results["anticipate"] = run_safe(SCRIPTS_DIR / "anticipate.py", [], timeout=60)
    # Hebbian consolidation — extend TTL for frequently recalled memories (salience-boosted)
    results["hebbian"] = run_safe(SCRIPTS_DIR / "hebbian_consolidation.py", ["--apply"], timeout=180)
    write_health("dreaming", results)
    log.info("=== DREAMING RUN END ===")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("daily", "weekly", "dreaming"):
        log.error("Usage: automation_dispatcher.py daily|weekly|dreaming")
        sys.exit(0)  # Always exit 0 for scheduler

    mode = sys.argv[1]
    try:
        if mode == "daily":
            daily_tasks()
        elif mode == "weekly":
            weekly_tasks()
        elif mode == "dreaming":
            dreaming_tasks()
    except Exception as exc:
        log.critical("Unhandled error in dispatcher: %s", exc, exc_info=True)
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
