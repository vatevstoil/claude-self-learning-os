#!/usr/bin/env python3
"""SessionStart hook — emit ONLY relevant alerts to Claude.

Reads from stdin: {"cwd": "..."}.
Outputs (to stdout, captured by Claude Code as session context):
- Empty if nothing to alert
- 1-2 short lines if pending promotions or active project wiki is stale

Token-conscious: typically 0-30 tokens output.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Force UTF-8 stdout — Windows default cp1251 cannot encode emojis/Cyrillic
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LOGS = Path.home() / ".claude" / "logs"
PENDING_SUMMARY = LOGS / "pending-summary.txt"
STALE_PROJECTS = LOGS / "stale-projects.txt"
SELFREG_HEALTH = LOGS / "selfreg-health.json"
GRAPHIFY_QUEUE = LOGS / "graphify-queue.json"
BORIS_CANDIDATES = LOGS / "boris-candidates.json"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def project_from_cwd(cwd: str) -> str | None:
    cwd_norm = cwd.replace("\\", "/").rstrip("/")
    for base in ("{{CODE_PATH}}/", "{{WIKI_PATH}}/", "{{RESEARCH_PATH}}/"):
        if cwd_norm.startswith(base) or cwd_norm == base.rstrip("/"):
            rest = cwd_norm[len(base):]
            if not rest:
                return None
            return rest.split("/", 1)[0]
    return None


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}

    cwd = payload.get("cwd") or os.getcwd()
    project = project_from_cwd(cwd)

    alerts: list[str] = []

    # Check pending promotions count
    if PENDING_SUMMARY.exists():
        try:
            text = PENDING_SUMMARY.read_text(encoding="utf-8")
            m = re.search(r"promotions:\s*(\d+) pending", text)
            if m and int(m.group(1)) > 0:
                alerts.append(
                    f"📋 {m.group(1)} pending promotions — review with `cat ~/.claude/logs/promotions-pending.md`"
                )
            m = re.search(r"notebooks:\s*(\d+) new sources", text)
            if m and int(m.group(1)) > 0:
                alerts.append(
                    f"📺 {m.group(1)} new NotebookLM sources awaiting ingest — `cat ~/.claude/logs/notebook-new-sources.txt`"
                )
        except Exception:
            pass

    # Check stale wiki for active project
    if project and STALE_PROJECTS.exists():
        try:
            text = STALE_PROJECTS.read_text(encoding="utf-8")
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) >= 3 and parts[0] == project:
                    age, file_label = parts[1], parts[2]
                    if age == "missing":
                        alerts.append(f"⚠ {project}: {file_label} file missing (run /graphify or wiki init)")
                    elif age == "unknown":
                        alerts.append(f"⚠ {project} wiki status unknown")
                    else:
                        alerts.append(f"⚠ {project} wiki: {file_label} is {age} days old")
                    break
        except Exception:
            pass

    # Graphify enrichment — alert if THIS project's graph needs LLM enrichment
    if project and GRAPHIFY_QUEUE.exists():
        try:
            q = json.loads(GRAPHIFY_QUEUE.read_text(encoding="utf-8"))
            # map code-folder name (cwd) to wiki name is non-trivial here; match either
            for item in q.get("queued_for_llm", []):
                proj = item.get("project", "")
                if proj and (proj == project or project.replace(" ", "").lower() in proj.lower()
                             or proj.lower() in project.replace(" ", "").lower()):
                    if item.get("enrich"):
                        alerts.append(f"🧠 {proj} graph needs enrichment — run /graphify to add critical_rules")
                    break
        except Exception:
            pass

    # Boris loop — surface per-project correction→rule candidates for THIS project
    if project and BORIS_CANDIDATES.exists():
        try:
            b = json.loads(BORIS_CANDIDATES.read_text(encoding="utf-8"))
            pnorm = _norm(project)
            for key, info in (b.get("projects", {}) or {}).items():
                knorm = _norm(key)
                if pnorm and (pnorm in knorm or knorm in pnorm):
                    cnt = info.get("count", 0)
                    # ≥4 to cut false positives (Bulgarian "не" is ubiquitous);
                    # full list stays in boris-candidates.json for review.
                    if cnt >= 4:
                        alerts.append(
                            f"🧭 {cnt} корекции в {project} наскоро — обмисли CLAUDE.md правило (Boris). "
                            f"Виж ~/.claude/logs/boris-candidates.json"
                        )
                    break
        except Exception:
            pass

    # Self-regulation health — only alert when degraded (grade C or worse, or regressions)
    if SELFREG_HEALTH.exists():
        try:
            h = json.loads(SELFREG_HEALTH.read_text(encoding="utf-8"))
            grade = h.get("grade", "A")
            regressions = h.get("regressions", [])
            issues = [x for v in h.get("issues", {}).values() for x in v]
            if grade in ("C", "D", "F") or regressions:
                msg = f"🩺 selfreg health {grade} ({h.get('overall')}/100)"
                if issues:
                    msg += " — " + "; ".join(issues[:2])
                if regressions:
                    msg += " — regression: " + "; ".join(regressions[:2])
                alerts.append(msg)
        except Exception:
            pass

    if alerts:
        # Output goes to Claude as session context
        print("\n".join(alerts))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Hook contract: never crash
        sys.exit(0)
