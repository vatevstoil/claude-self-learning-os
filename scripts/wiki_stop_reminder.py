#!/usr/bin/env python3
"""Stop hook — remind Claude to update wiki if current project is stale.

Reads cwd from stdin, finds project in freshness.json, fires at most once
per project per day to avoid alert fatigue.

Output: {"decision": "block", "reason": "..."} → Claude gets one more turn
        to update wiki before session truly ends.
Exit 0 silently when not needed.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

LOGS = Path.home() / ".claude" / "logs"
FRESHNESS = LOGS / "freshness.json"
STATE = LOGS / "wiki-reminder-state.json"
LOG_STALE_DAYS = 4   # remind if project log not updated in this many days
GRAPH_STALE_DAYS = 10


# ── helpers ──────────────────────────────────────────────────────────────────

def _project_from_cwd(cwd: str) -> str | None:
    cwd = cwd.replace("\\", "/").rstrip("/")
    for base in ("{{CODE_PATH}}/", "{{WIKI_PATH}}/", "{{RESEARCH_PATH}}/"):
        if cwd.startswith(base):
            rest = cwd[len(base):]
            if rest:
                return rest.split("/", 1)[0]
    return None


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}

    cwd = payload.get("cwd") or os.getcwd()
    project = _project_from_cwd(cwd)
    if not project:
        sys.exit(0)

    if not FRESHNESS.exists():
        sys.exit(0)

    try:
        data = json.loads(FRESHNESS.read_text(encoding="utf-8"))
    except Exception:
        sys.exit(0)

    # Find this project in freshness data
    pnorm = project.lower().replace(" ", "")
    entry = None
    for p in data.get("projects", []):
        if p.get("project", "").lower().replace(" ", "") == pnorm:
            entry = p
            break

    if not entry:
        sys.exit(0)

    log_age = entry.get("log_age_days") or 0
    graph_age = entry.get("graph_age_days") or 0

    issues: list[str] = []
    if log_age >= LOG_STALE_DAYS:
        issues.append(f"log.md не е обновено {log_age}д")
    if graph_age >= GRAPH_STALE_DAYS:
        issues.append(f"knowledge_graph.json не е обновен {graph_age}д")

    if not issues:
        sys.exit(0)

    # Rate-limit: once per project per day
    today = date.today().isoformat()
    state = _load_state()
    if state.get(project) == today:
        sys.exit(0)  # already reminded today

    state[project] = today
    _save_state(state)

    reason = (
        f"⚠ Wiki/{project}: {', '.join(issues)}. "
        f"Обнови wiki преди да затвориш — "
        f"index.md + knowledge_graph.json + Pinecone save."
    )
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
