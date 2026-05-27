#!/usr/bin/env python3
"""SessionStart hook — emit ONLY relevant alerts to Claude.

Reads from stdin: {"cwd": "..."}.
Outputs (to stdout, captured by Claude Code as session context):
- Empty if nothing to alert
- 1-2 short lines if pending promotions or active project wiki is stale

Token-conscious: typically 0-30 tokens output.
"""
from __future__ import annotations

import datetime
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


def _legacy_alerts(alerts: list[str], project: str | None) -> None:
    """Fallback logic for promotions + Boris when the queue module is unavailable."""
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
                    if cnt >= 4:
                        alerts.append(
                            f"🧭 {cnt} корекции в {project} наскоро — обмисли CLAUDE.md правило (Boris). "
                            f"Виж ~/.claude/logs/boris-candidates.json"
                        )
                    break
        except Exception:
            pass


def _format_queue_item(item: object) -> str | None:
    """Format a QueueItem into a one-line alert string."""
    item_type = item.type  # type: ignore[attr-defined]
    project = item.project  # type: ignore[attr-defined]
    desc = item.description  # type: ignore[attr-defined]

    if item_type == "boris_rule":
        return f"🧭 {desc} — обмисли CLAUDE.md правило (Boris)"
    elif item_type == "promotion":
        return f"📋 Pending promotion: {desc[:100]}"
    elif item_type == "habit":
        return f"🔁 {desc[:120]}"
    elif item_type == "graphify":
        return f"🧠 {desc}"
    return None


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}

    cwd = payload.get("cwd") or os.getcwd()
    project = project_from_cwd(cwd)

    alerts: list[str] = []

    # --- Self-regulation health (urgent, always checked first) ---
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

    # --- Unified queue: top-2 queued items for this project ---
    _queue_ok = False
    try:
        from self_improvement_queue import build_queue, filter_for_project  # type: ignore

        all_items = build_queue()
        project_items = filter_for_project(all_items, project or "")
        queued = [i for i in project_items if i.status == "queued"]
        for item in queued[:2]:
            msg = _format_queue_item(item)
            if msg:
                alerts.append(msg)
                # Track implicit feedback — auto-suppress after 5 unacknowledged surfaces
                try:
                    from suggestion_feedback import record_surfaced  # type: ignore
                    record_surfaced(item.id)  # type: ignore[attr-defined]
                except Exception:
                    pass
        _queue_ok = True
    except Exception:
        pass

    # --- Legacy fallback when queue module is unavailable ---
    if not _queue_ok:
        _legacy_alerts(alerts, project)

    # --- Auto-recall: query project's Pinecone namespace (tracks Hebbian hits) ---
    # Fires only when a project is active and credentials are available.
    # Silent on any error — never blocks session start.
    if project:
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
            env_path = Path.home() / ".claude" / ".env"
            if env_path.exists():
                import os as _os
                for _line in env_path.read_text(encoding="utf-8").splitlines():
                    _line = _line.strip()
                    if _line and not _line.startswith("#") and "=" in _line:
                        _k, _v = _line.split("=", 1)
                        _os.environ.setdefault(_k.strip(), _v.strip())
            from pinecone import query_and_track  # type: ignore
            _ns = project.replace(" ", "_")
            query_and_track(_ns, project, topk=3)  # track recall, results unused at start
        except Exception:
            pass

    # --- Anticipation: surface the top predicted routine for THIS project (proactive) ---
    # Matching is order-sensitive: substring match against ~30 boris-encoded keys
    # previously hit the wrong project (e.g. "Claude" matched
    # "...Facturka-bg--claude-worktrees-bold-kilby-..." first). Three-tier match:
    #   1. Exact encoded key (drive + path → "J--Antigraviti-Claude")
    #   2. Segment match: project name must appear as own '-'-bounded segment,
    #      AND the key must NOT be a worktree (skip "--claude-worktrees-...")
    #   3. Fall back to most-recent-data when ambiguous
    if project:
        try:
            ant = json.loads((LOGS / "anticipations.json").read_text(encoding="utf-8"))
            cwd_clean = cwd.replace("\\", "/").rstrip("/")
            # Construct expected key: "{{CODE_PATH}}/Claude" → "J--Antigraviti-Claude"
            expected_key = None
            m = re.match(r"^([A-Za-z]):/(.+)$", cwd_clean)
            if m:
                drive, rest = m.group(1).upper(), m.group(2)
                expected_key = f"{drive}--" + rest.replace("/", "-").replace(" ", "-")

            picked = None
            # Tier 1: exact (case-insensitive) — most reliable
            if expected_key:
                for k in (ant or {}).keys():
                    if k.lower() == expected_key.lower() and ant[k]:
                        picked = (k, ant[k])
                        break

            # Tier 2: segment match excluding worktrees
            if not picked:
                pnorm = _norm(project)
                for k, preds in (ant or {}).items():
                    if "claude-worktrees" in k.lower():
                        continue  # skip auto-generated worktree branches
                    # Project must appear as its own segment (between -- or at start/end)
                    segments = [_norm(s) for s in re.split(r"-+", k) if s]
                    if pnorm and pnorm in segments and preds:
                        picked = (k, preds)
                        break

            if picked:
                _, preds = picked
                routine = (preds[0] or {}).get("routine") or []
                if routine:
                    alerts.append(f"🔮 В {project} обикновено: {' → '.join(routine)}")
        except Exception:
            pass

    # --- Session-size alarm (proactive: detect long-running session at resume) ---
    # Claude Code stores each session as a single .jsonl file under
    # ~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl. File size correlates
    # roughly with context buildup. Warns at SessionStart resume so the user
    # sees it BEFORE doing more work in an already-bloated session.
    try:
        session_id = payload.get("session_id") or payload.get("sessionId")
        source = (payload.get("source") or "").lower()
        # Encode cwd to Claude's project dir format: {{CODE_PATH}}\Claude → J--Antigraviti-Claude
        cwd_norm = cwd.replace("\\", "/").rstrip("/")
        m = re.match(r"^([A-Za-z]):/(.+)$", cwd_norm)
        if m and session_id:
            drive, rest = m.group(1).upper(), m.group(2)
            encoded = f"{drive}--" + rest.replace("/", "-").replace(" ", "-")
            proj_dir = Path.home() / ".claude" / "projects" / encoded
            sess_file = proj_dir / f"{session_id}.jsonl"
            # Fallback: try case-insensitive (Windows) — find any matching dir
            if not sess_file.exists():
                parent = proj_dir.parent
                if parent.exists():
                    for d in parent.iterdir():
                        if d.is_dir() and d.name.lower() == encoded.lower():
                            sess_file = d / f"{session_id}.jsonl"
                            break
            if sess_file.exists() and source in ("resume", "compact"):
                size_kb = sess_file.stat().st_size / 1024
                if size_kb >= 1500:
                    alerts.append(
                        f"🚨 Session resumed at {size_kb/1024:.1f}MB transcript "
                        f"— /clear силно препоръчан преди да продължиш"
                    )
                elif size_kb >= 700:
                    alerts.append(
                        f"⚠ Session resumed at {size_kb:.0f}KB transcript "
                        f"— обмисли /clear при смяна на тема"
                    )
    except Exception:
        pass

    # --- Rotating token hygiene tip (1 per session, cycles daily) ---
    _TIPS = [
        "💡 ≤2 файла, ясна задача → директно без агенти",
        "💡 15-20 msg по темата → /clear",
        "💡 Голям файл → python -c summary, не чети директно",
        "💡 Batch свързани въпроси в 1 prompt → -40% turns",
    ]
    alerts.append(_TIPS[datetime.date.today().toordinal() % len(_TIPS)])

    if alerts:
        print("\n".join(alerts))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Hook contract: never crash
        sys.exit(0)
