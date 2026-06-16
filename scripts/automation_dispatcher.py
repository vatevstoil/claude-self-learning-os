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

# Make sibling helper modules importable (run-state sentinel, snapshot, notify).
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

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


# Scripts whose non-zero exit is SEMANTIC information, not failure.
# Must stay in sync with selfreg_monitor.SEMANTIC_NONZERO — kept duplicated
# (not imported) to avoid coupling the dispatcher to selfreg's module loading.
# Currently:
#   - wiki_freshness_check.py: exits 1 when ≥1 wiki is stale (information).
_SEMANTIC_NONZERO = {"wiki_freshness_check.py"}


def run_safe(script_path: Path, args: list[str], timeout: int = 300) -> bool:
    """Run a python script in a subprocess. Always returns; never raises.

    Returns True on success OR on semantic-nonzero exit (e.g. wiki_freshness
    exiting 1 to signal stale wikis). Without this, health.json marks the
    daily run DEGRADED every day a wiki is stale — a permanent false-positive
    that hides real failures.
    """
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
        if result.returncode != 0 and name not in _SEMANTIC_NONZERO:
            log.warning("%s stderr (first 300): %s",
                        name, (result.stderr or "")[:300])
        return result.returncode == 0 or name in _SEMANTIC_NONZERO
    except subprocess.TimeoutExpired:
        log.warning("%s: TIMEOUT after %ds", name, timeout)
        return False
    except Exception as exc:
        log.error("%s: ERROR %s", name, exc)
        return False


def _safe(fn_name: str, *args, **kwargs):
    """Call an optional helper by name from a sibling module; never raise.

    The scheduler requires the dispatcher to never crash, so every cross-module
    helper (run-state sentinel, git snapshot, notify) is invoked through this
    guard — a missing module or a helper error degrades to a logged no-op.
    """
    try:
        mod_name, attr = fn_name.split(".", 1)
        mod = __import__(mod_name)
        return getattr(mod, attr)(*args, **kwargs)
    except Exception as exc:
        log.warning("helper %s failed: %s", fn_name, exc)
        return None


def _mark_state(run_type: str, status: str) -> None:
    """Run-state sentinel: RUNNING at start, COMPLETED at end. If the scheduler
    kills the process mid-run, the file stays RUNNING and health_escalation's
    detect_aborted_runs surfaces it as ABORTED — otherwise the failure is
    invisible (write_health never executed)."""
    _safe("health_escalation.mark_run_state", run_type, status, os.getpid())


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
    _mark_state("daily", "RUNNING")
    # Pre-mutation baseline: snapshot the system's own code BEFORE any
    # auto-apply, so a bad self-modification today is diffable/revertible.
    _safe("git_snapshot.snapshot", "pre-daily")
    results: dict[str, bool] = {}
    results["wiki_freshness"] = run_safe(SCRIPTS_DIR / "wiki_freshness_check.py",
             ["--out", str(FRESHNESS_OUT), "--threshold", "14"])
    write_pending_summary()
    results["habit_miner"] = run_safe(SCRIPTS_DIR / "habit_miner.py", ["--days", "2"], timeout=180)
    results["habit_accept_queue"] = run_safe(SCRIPTS_DIR / "habit_to_skill.py", ["--process-accepted"], timeout=60)
    results["boris_apply"] = run_safe(SCRIPTS_DIR / "boris_draft.py", ["--process-accepted"], timeout=60)
    # Tiered trust: apply boris drafts automatically when the type has earned
    # tier >= 1 (precision history); max 2/run, ledger-recorded, rollback-able.
    results["boris_auto_apply"] = run_safe(SCRIPTS_DIR / "boris_draft.py", ["--auto-apply"], timeout=60)
    # Repeated corrections (>=3 similar in a project) become first-class
    # incidents — surfaced at the top of the daily brief, not just rule drafts.
    results["incident_tracker"] = run_safe(SCRIPTS_DIR / "incident_tracker.py", [], timeout=60)
    # Strong recurring incidents get a drafted, human-gated fix-session prompt
    # (review/launch manually — never auto-executed).
    results["fix_proposer"] = run_safe(SCRIPTS_DIR / "incident_fix_proposer.py", [], timeout=60)
    # Heal the local memory backend FIRST (start/restart Ollama, pull bge-m3 if
    # missing) so the replay below actually has a working embedder to drain into.
    # 1100s: a missing-model pull can take minutes under GPU co-tenancy; the old
    # 210s cap could time out and falsely mark the backend unhealthy.
    results["ollama_doctor"] = run_safe(SCRIPTS_DIR / "ollama_doctor.py", ["--ensure", "--quiet"], timeout=1100)
    # Replay any saves queued while the backend was unavailable (Ollama down or,
    # in pinecone mode, embedding quota exhausted). Cheap no-op when queue empty.
    results["replay_pending_saves"] = run_safe(SCRIPTS_DIR / "pinecone.py", ["replay-queue"], timeout=120)
    # Daily consistent backup of local_rag.db — the only copy of local-first memory.
    results["backup_local_rag"] = run_safe(SCRIPTS_DIR / "backup_local_rag.py", ["--keep", "7"], timeout=120)
    # anticipate must run daily — previously only in weekly dreaming, so the
    # SessionStart hook's 🔮 prediction was up to 6 days stale by mid-week.
    results["anticipate"] = run_safe(SCRIPTS_DIR / "anticipate.py", [], timeout=60)
    # Rebuild the queue + KPI BEFORE the brief reads them — otherwise the brief
    # shows last week's stale queue depth (was 149 vs real 38) and KPI.
    results["improvement_queue"] = run_safe(SCRIPTS_DIR / "self_improvement_queue.py", [], timeout=60)
    results["outcome_kpi"] = run_safe(SCRIPTS_DIR / "outcome_kpi.py", [], timeout=60)
    # Integrity guard: invariant check that catches the system measuring itself
    # with broken rulers (tool-ngram drafts, inflated precision, graph!=disk,
    # mojibake, corrupt accepted-provenance). Runs BEFORE the brief reads it.
    results["integrity_guard"] = run_safe(SCRIPTS_DIR / "integrity_guard.py", [], timeout=60)
    results["selfreg_monitor"] = run_safe(SCRIPTS_DIR / "selfreg_monitor.py", [])
    # Morning brief (reads fresh selfreg/freshness/queue/kpi state above). Captures
    # yesterday's {open notes}. Pure-local, no embedding — safe under quota outage.
    results["daily_brief"] = run_safe(SCRIPTS_DIR / "daily_brief.py", [], timeout=60)
    write_health("daily", results)
    _mark_state("daily", "COMPLETED")
    # Escalation reads the health.json just written: >=2 consecutive DEGRADED
    # triggers automatic remediation (e.g. Ollama restart) + escalation.json.
    # Runs AFTER write_health by design — not part of the graded results.
    run_safe(SCRIPTS_DIR / "health_escalation.py", [], timeout=300)
    # Push the human ONLY when something needs them — escalation that failed
    # remediation, and any open incidents. Dedup'd (20h) inside notify.py.
    run_safe(SCRIPTS_DIR / "notify.py", ["escalation"], timeout=30)
    run_safe(SCRIPTS_DIR / "notify.py", ["incidents"], timeout=30)
    log.info("=== DAILY RUN END ===")


def weekly_tasks():
    log.info("=== WEEKLY RUN START ===")
    _mark_state("weekly", "RUNNING")
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
    # 900s: the numpy-vectorized pass over the largest namespaces was observed
    # to exceed the old 300s cap (TIMEOUT in selfreg issues 2026-06-10).
    results["semantic_merge"] = run_safe(SCRIPTS_DIR / "semantic_merge.py",
             ["--all-namespaces", "--apply", "--threshold", "0.95"], timeout=900)
    results["salience"] = run_safe(SCRIPTS_DIR / "salience.py", ["--days", "7"], timeout=120)
    write_pending_summary()
    results["improvement_queue"] = run_safe(SCRIPTS_DIR / "self_improvement_queue.py", [], timeout=60)
    # LLM judge scores queue items (graceful no-op when Ollama unavailable) so
    # only semantically useful suggestions reach the human / auto-apply path.
    # 900s: the reasoning model needs ~60-120s/item; llm_judge persists after
    # every item, so hitting this cap keeps all verdicts judged so far.
    results["llm_judge_queue"] = run_safe(SCRIPTS_DIR / "llm_judge.py", ["--judge-queue", "--max", "20"], timeout=900)
    # knowledge_sync now embeds research wikis ({{RESEARCH_PATH}} concepts/sources)
    # in addition to project learnings — heaviest weekly task. Incremental after the
    # first full embed, but give generous headroom for big content drops.
    results["knowledge_sync"] = run_safe(SCRIPTS_DIR / "knowledge_sync.py", [], timeout=1200)
    results["agentic_os_registry"] = run_safe(SCRIPTS_DIR / "agentic_os_registry.py", [])
    # Regenerate the "which skill, when" map from skill/command frontmatter (no drift).
    results["skill_map"] = run_safe(SCRIPTS_DIR / "skill_map_gen.py", [], timeout=60)
    # Refresh the hippocampal knowledge map (cross-wiki index + tracts). Consolidation.
    results["knowledge_map"] = run_safe(SCRIPTS_DIR / "knowledge_map_gen.py", [], timeout=120)
    results["roi_tracker"] = run_safe(SCRIPTS_DIR / "roi_tracker.py", ["--days", "30"])
    results["cross_recall_metrics"] = run_safe(SCRIPTS_DIR / "cross_recall_metrics.py", ["--days", "30"])
    # Outcome KPI — measures RESULTS (repeat-correction rate, recall engagement,
    # apply-funnel throughput), unlike roi_tracker which counts activity.
    results["outcome_kpi"] = run_safe(SCRIPTS_DIR / "outcome_kpi.py", [], timeout=60)
    # A/B counterfactual eval: per-rule before/after recurrence (interrupted
    # time series). Weekly because a verdict needs >= window_days of
    # post-application history. Surfaces regressed/no-effect auto-rules.
    results["ab_eval"] = run_safe(SCRIPTS_DIR / "ab_eval.py", [], timeout=120)
    # Archive report (DRY): how many never-recalled old learning vectors are
    # dead weight. Apply stays a manual, backup-gated decision.
    results["memory_archiver_dry"] = run_safe(SCRIPTS_DIR / "memory_archiver.py", [], timeout=300)
    # Stale human-review queues (DRY): promotions/cross-links pending >7d are
    # reported to queue-aging.json for the daily brief. Apply stays manual.
    results["queue_aging_dry"] = run_safe(SCRIPTS_DIR / "queue_aging.py", [], timeout=60)
    # Skill usage audit (DRY/report-only): surfaces dead skills/commands that
    # have not been invoked in 30d — the producer that populates skill-usage-audit.json
    # which daily_brief.py already renders (dead_skills count). Without this call
    # the consumer reads a stale or absent file.
    results["skill_usage_audit"] = run_safe(SCRIPTS_DIR / "skill_usage_audit.py", [], timeout=60)
    # Amnesty: clear implicit auto-dismissals so valid suggestions the UI hid
    # (no accept/reject handle) get another chance. Idempotent; weekly cadence.
    results["suggestion_amnesty"] = run_safe(SCRIPTS_DIR / "suggestion_feedback.py", ["amnesty"], timeout=30)
    results["selfreg_monitor"] = run_safe(SCRIPTS_DIR / "selfreg_monitor.py", [])
    write_health("weekly", results)
    _mark_state("weekly", "COMPLETED")
    run_safe(SCRIPTS_DIR / "health_escalation.py", [], timeout=300)
    # Weekly human digest (info level) — the otherwise-unread selfreg digest,
    # pushed once a week so the human sees the trend without opening a file.
    run_safe(SCRIPTS_DIR / "notify.py", ["digest"], timeout=30)
    run_safe(SCRIPTS_DIR / "notify.py", ["escalation"], timeout=30)
    log.info("=== WEEKLY RUN END ===")


def dreaming_tasks():
    """Stage 3 self-learning analysis. Inspired by Jack Roberts Claude OS Dreaming.
    See: {{RESEARCH_PATH}}\\Claude Code Resurch\\wiki\\summaries\\Claude-OS-Dashboard-Jack.md
    """
    log.info("=== DREAMING RUN START ===")
    _mark_state("dreaming", "RUNNING")
    results: dict[str, bool] = {}
    results["stage3_dreaming"] = run_safe(SCRIPTS_DIR / "stage3_dreaming.py", ["--days", "7"], timeout=600)
    results["skills_audit"] = run_safe(SCRIPTS_DIR / "skills_audit.py", [], timeout=120)
    results["habit_miner"] = run_safe(SCRIPTS_DIR / "habit_miner.py", ["--days", "14"], timeout=300)
    results["habit_ledger"] = run_safe(SCRIPTS_DIR / "habit_ledger.py", [], timeout=120)
    results["habit_to_skill"] = run_safe(SCRIPTS_DIR / "habit_to_skill.py", [], timeout=60)
    results["habit_accept_queue"] = run_safe(SCRIPTS_DIR / "habit_to_skill.py", ["--process-accepted"], timeout=60)
    # Judge layer: prune tool-ngram junk drafts (the 477-drafts problem), then
    # LLM-score the survivors so auto-apply has a quality gate to consume.
    results["judge_prune"] = run_safe(SCRIPTS_DIR / "llm_judge.py", ["--prune-drafts", "--apply"], timeout=120)
    # GC: rejected-draft dirs older than 30d (the dir grew to 600+ unbounded).
    results["purge_rejected"] = run_safe(SCRIPTS_DIR / "llm_judge.py", ["--purge-rejected-days", "30", "--apply"], timeout=60)
    # max 6: each judgement can take up to 60s against local Ollama; 6*60=360s
    # leaves headroom inside the 600s cap instead of riding the timeout edge.
    results["judge_drafts"] = run_safe(SCRIPTS_DIR / "llm_judge.py", ["--judge-drafts", "--max", "6"], timeout=600)
    results["boris_draft"] = run_safe(SCRIPTS_DIR / "boris_draft.py", [], timeout=60)
    results["boris_apply"] = run_safe(SCRIPTS_DIR / "boris_draft.py", ["--process-accepted"], timeout=60)
    # Tiered trust auto-apply: boris needs tier>=1; habits need tier>=1 AND a
    # "useful" judge verdict (double gate). Both ledger-recorded + rollback-able.
    results["boris_auto_apply"] = run_safe(SCRIPTS_DIR / "boris_draft.py", ["--auto-apply"], timeout=60)
    results["habit_auto_apply"] = run_safe(SCRIPTS_DIR / "habit_to_skill.py", ["--auto-apply"], timeout=60)
    results["incident_tracker"] = run_safe(SCRIPTS_DIR / "incident_tracker.py", [], timeout=60)
    results["fix_proposer"] = run_safe(SCRIPTS_DIR / "incident_fix_proposer.py", [], timeout=60)
    results["improvement_queue"] = run_safe(SCRIPTS_DIR / "self_improvement_queue.py", [], timeout=60)
    # 900s: the reasoning model needs ~60-120s/item; llm_judge persists after
    # every item, so hitting this cap keeps all verdicts judged so far.
    results["llm_judge_queue"] = run_safe(SCRIPTS_DIR / "llm_judge.py", ["--judge-queue", "--max", "20"], timeout=900)
    results["effectiveness_tracker"] = run_safe(SCRIPTS_DIR / "effectiveness_tracker.py", [], timeout=60)
    results["outcome_kpi"] = run_safe(SCRIPTS_DIR / "outcome_kpi.py", [], timeout=60)
    results["anticipate"] = run_safe(SCRIPTS_DIR / "anticipate.py", [], timeout=60)
    # Hebbian consolidation — extend TTL for frequently recalled memories (salience-boosted)
    results["hebbian"] = run_safe(SCRIPTS_DIR / "hebbian_consolidation.py", ["--apply"], timeout=180)
    write_health("dreaming", results)
    _mark_state("dreaming", "COMPLETED")
    # Capture what the system changed about ITSELF tonight (Boris rules applied,
    # skills installed) as a labelled snapshot — the diff of self-modification.
    _safe("git_snapshot.snapshot", "post-dreaming-autoapply")
    # Dreaming escalates on the FIRST DEGRADED run (threshold=1) — a single failed
    # dreaming session is already diagnostic. Daily/weekly retain threshold=2
    # (one-off transient failures should not page).
    run_safe(SCRIPTS_DIR / "health_escalation.py", ["--threshold", "1"], timeout=300)
    run_safe(SCRIPTS_DIR / "notify.py", ["escalation"], timeout=30)
    log.info("=== DREAMING RUN END ===")


_LOCK_PATH = LOGS_DIR / "dispatcher.lock"


def _acquire_lock():
    """Single-writer interprocess lock so daily/weekly/dreaming never overlap.

    Overlapping runs corrupt shared state (queue, ledger, run-state) and double
    the load. msvcrt.locking is released by the OS the instant the process dies
    — no stale-lock cleanup logic needed. Returns the open handle (keep it alive
    for the whole run) or None if another run holds the lock.
    """
    try:
        import msvcrt
        fh = open(_LOCK_PATH, "w", encoding="utf-8")
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return fh
        except OSError:
            fh.close()
            return None
    except Exception:
        # Non-Windows or msvcrt unavailable: degrade to no lock rather than crash.
        return True


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("daily", "weekly", "dreaming"):
        # Interactive misinvocation (human typo / inspection) — NOT an automation
        # failure. Print usage to stderr but do NOT log at ERROR to automation.log:
        # that log feeds selfreg health, and counting a no-arg CLI call as a
        # failure falsely drags the health grade (the recurring "Usage:" issue).
        print("Usage: automation_dispatcher.py daily|weekly|dreaming", file=sys.stderr)
        sys.exit(0)  # Always exit 0 for scheduler

    mode = sys.argv[1]
    lock = _acquire_lock()
    if lock is None:
        log.warning("Another dispatcher run holds the lock — skipping %s run.", mode)
        sys.exit(0)
    try:
        if mode == "daily":
            daily_tasks()
        elif mode == "weekly":
            weekly_tasks()
        elif mode == "dreaming":
            dreaming_tasks()
    except Exception as exc:
        log.critical("Unhandled error in dispatcher: %s", exc, exc_info=True)
        # Even on unhandled crash, record health so the failure is VISIBLE
        # (the scheduler-kill case is covered separately by the run-state sentinel).
        try:
            write_health(mode, {"_dispatcher_crash": False})
        except Exception:
            pass
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
