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
    # Hook payload is UTF-8 JSON; Windows piped stdin defaults to cp1251.
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}

    cwd = payload.get("cwd") or os.getcwd()
    project = project_from_cwd(cwd)
    # Defense in depth: if cwd carries Cyrillic (e.g. a "Петър Дънов" path), the
    # parsed project bypasses ns_util's guard — repair any mojibake before it is
    # written to cross-recall-surfaced.jsonl and skews per-project metrics.
    if project:
        try:
            from encoding_guard import guard as _enc_guard
            project = _enc_guard(project)
        except Exception:
            pass

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

    # --- Auto-recall + cross-project surfacing ---------------------------------
    # Embed the project context ONCE, then:
    #   (a) query the project's OWN namespace → track Hebbian recall (as before);
    #   (b) query the shared cross-project layers (_shared + _claude_meta) and
    #       SURFACE the top high-signal lessons — the cross-project PULL that was
    #       previously missing (the old code threw recall results away).
    # Quality-guarded: only hits >= 0.82 similarity, capped at 2, 1 line each, so
    # irrelevant matches stay silent. Bounded timeouts so a slow API never blocks
    # session start. Silent on any error.
    if project:
        try:
            import os as _os, json as _json, sys as _sys
            from urllib.request import Request as _Rq, urlopen as _uo
            _sys.path.insert(0, str(Path(__file__).parent))
            env_path = Path.home() / ".claude" / ".env"
            if env_path.exists():
                for _line in env_path.read_text(encoding="utf-8").splitlines():
                    _line = _line.strip()
                    if _line and not _line.startswith("#") and "=" in _line:
                        _k, _v = _line.split("=", 1)
                        _os.environ.setdefault(_k.strip(), _v.strip())
            _HOST = _os.environ.get("PINECONE_INDEX_HOST", "")
            _KEY = _os.environ.get("PINECONE_API_KEY", "")
            _MODEL = _os.environ.get("PINECONE_EMBED_MODEL", "multilingual-e5-large")
            _BACKEND = _os.environ.get("MEMORY_BACKEND", "local").strip().lower()

            def _pc(url, body, timeout):
                _r = _Rq(url, data=_json.dumps(body).encode(), method="POST")
                _r.add_header("Api-Key", _KEY)
                _r.add_header("Content-Type", "application/json")
                _r.add_header("X-Pinecone-API-Version", "2025-04")
                return _json.loads(_uo(_r, timeout=timeout).read())

            _lr = None
            if _BACKEND == "local":
                try:
                    import local_rag as _lr  # type: ignore
                except Exception:
                    _lr = None

            def _do_embed(text):
                if _lr is not None:
                    # Short timeout: session start must never block on a cold/down
                    # Ollama (cold model load ≈2.5s; budget 8s, then degrade silently).
                    return _lr.local_embed(text, is_query=True, timeout=8)
                return _pc("https://api.pinecone.io/embed",
                           {"model": _MODEL, "inputs": [{"text": text}],
                            "parameters": {"input_type": "query", "truncate": "END"}},
                           2.5)["data"][0]["values"]

            def _do_query(ns, vec, topk):
                if _lr is not None:
                    return [{"id": h["id"], "score": h["score"], "metadata": h.get("meta", {})}
                            for h in _lr.query_vec(vec, namespaces=[ns], topk=topk)]
                return _pc(f"https://{_HOST}/query",
                           {"vector": vec, "topK": topk, "namespace": ns,
                            "includeMetadata": True}, 1.5).get("matches", [])

            if _lr is not None or (_HOST and _KEY):
                # Task-enriched query: bare project name surfaces only generic
                # project-comparison docs; appending the CURRENT task makes the
                # cross-project recall return actionable, task-specific lessons
                # (empirically: 0.82→0.90 scores, and rescues sub-threshold gems).
                # Falls back to the bare name when no current task is found.
                _qtext = project
                try:
                    from session_handoff import detect as _detect, current_task as _ctask  # type: ignore
                    _task = _ctask(_detect(cwd))
                    if not _task:
                        # Code projects rarely keep a SPRINT task; the last git
                        # commit subject is a strong "current work" signal that
                        # makes the cross-project recall task-aware for code too.
                        import subprocess as _sp
                        _r = _sp.run(["git", "-C", cwd, "log", "-1", "--format=%s"],
                                     capture_output=True, text=True, timeout=2)
                        if _r.returncode == 0:
                            _task = (_r.stdout or "").strip()[:120]
                    if _task:
                        _qtext = f"{project} {_task}"
                except Exception:
                    pass
                _vec = _do_embed(_qtext)
                # MUST match the namespace auto_pinecone_save WRITES to (sanitize_ns,
                # dashes + canonical aliases) — otherwise recall silently misses the
                # saved memory for multi-word projects (was project.replace(" ","_")).
                try:
                    from ns_util import sanitize_ns as _san
                    _ns = _san(project)
                except Exception:
                    _ns = project.replace(" ", "_")

                # (a) own namespace — Hebbian recall tracking (not surfaced)
                try:
                    _own_matches = _do_query(_ns, _vec, 3)
                    from recall_tracker import log_hits as _lh  # type: ignore
                    _lh(_ns, _own_matches)
                except Exception:
                    pass

                # (b) cross-project layers — surface top high-signal lessons
                # bge-m3 cosine scores run lower than e5's — calibrate the
                # high-signal threshold per backend (e5≈0.82, bge-m3≈0.60).
                _XTHRESH = 0.60 if _lr is not None else 0.82
                # Only surface meaningful types — raw_passage and typeless chunks
                # are bulk corpus noise and must not crowd out actual lessons.
                _MEANINGFUL_TYPES = frozenset({
                    "promoted", "learning", "decision",
                    "gotcha", "pattern", "antipattern",
                })

                def _do_query_typed(ns, vec, topk):
                    """Query with type_filter on local backend; post-filter for cloud."""
                    if _lr is not None:
                        return [
                            {"id": h["id"], "score": h["score"],
                             "metadata": h.get("meta", {})}
                            for h in _lr.query_vec(
                                vec, namespaces=[ns], topk=topk,
                                type_filter=list(_MEANINGFUL_TYPES),
                            )
                        ]
                    # Cloud Pinecone: no server-side type filter — post-filter client side
                    raw = _do_query(ns, vec, topk * 4)  # fetch more to survive filtering
                    return [
                        m for m in raw
                        if (m.get("metadata") or {}).get("type") in _MEANINGFUL_TYPES
                    ][:topk]

                _xhits = []
                for _xns in ("_shared", "_claude_meta"):
                    if _xns == _ns:
                        continue
                    try:
                        for _m in _do_query_typed(_xns, _vec, 3):
                            if _m.get("score", 0) >= _XTHRESH:
                                _xhits.append((_xns, _m))
                    except Exception:
                        continue
                _xhits.sort(key=lambda t: t[1].get("score", 0), reverse=True)
                _seen: set = set()
                _shown: list = []
                for _xns, _m in _xhits:
                    _txt = " ".join(((_m.get("metadata", {}) or {}).get("text", "") or "").split())
                    if not _txt or _txt[:40] in _seen:
                        continue
                    _seen.add(_txt[:40])
                    alerts.append(f"🔗 cross-project [{_xns}]: {_txt[:115]}")
                    _shown.append({"ns": _xns, "id": _m.get("id"),
                                   "score": round(float(_m.get("score", 0)), 4),
                                   "t": _txt[:120]})
                    if len(_seen) >= 2:
                        break

                # Measurement loop: log this surfacing event so cross_recall_metrics
                # can later answer "does cross-project recall actually help?".
                # Records hit_count AT SURFACE TIME (hc0); since surfacing is
                # non-tracking, any later hit_count rise = genuine re-recall.
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    import recall_tracker as _rt  # type: ignore
                    _rtd = _rt._load(_rt.DEFAULT_PATH)
                    for _s in _shown:
                        _s["hc0"] = int(_rtd.get(_s.get("id") or "", {}).get("hit_count", 0))
                    _rec = {"ts": _dt.now(_tz.utc).isoformat(timespec="seconds"),
                            "project": project, "enriched": _qtext != project,
                            "surfaced": _shown}
                    with (LOGS / "cross-recall-surfaced.jsonl").open("a", encoding="utf-8") as _f:
                        _f.write(_json.dumps(_rec, ensure_ascii=False) + "\n")
                except Exception:
                    pass
        except Exception:
            pass

    # --- Cross-recall metric (visibility: is cross-project surfacing engaging?) ---
    try:
        _cm = json.loads((LOGS / "cross-recall-metrics.json").read_text(encoding="utf-8"))
        if _cm.get("events", 0) >= 5:
            alerts.append(
                f"📊 cross-recall {_cm.get('window_days', 30)}d: "
                f"{_cm.get('surfaced', 0)} surfaced, "
                f"{int(_cm.get('engagement_rate', 0) * 100)}% re-engaged (lower bound)"
            )
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
                        f"— пусни `/handoff` после `/clear` (handoff подготвя всичко)"
                    )
                elif size_kb >= 700:
                    alerts.append(
                        f"⚠ Session resumed at {size_kb:.0f}KB transcript "
                        f"— при смяна на тема: `/handoff` → `/clear`"
                    )
    except Exception:
        pass

    # --- Daily brief pointer (AIOS-style morning brief + {open notes}) ---
    try:
        import re as _re
        _brief = Path(r"{{WIKI_PATH}}\_meta\daily-brief.md")
        if _brief.exists():
            _bt = _brief.read_text(encoding="utf-8", errors="replace")
            _today = datetime.date.today().isoformat()
            _seg = _bt.split("## 📝 Open notes", 1)
            _scan = _re.sub(r"<!--.*?-->", "", _seg[1] if len(_seg) > 1 else "", flags=_re.DOTALL)
            _notes = [m for m in _re.findall(r"\{([^{}]*)\}", _scan)
                      if m.strip() and not _re.match(r"^[\s.…·\-]*$", m)]
            _msg = ("📋 daily-brief.md готов за днес" if f"# Daily Brief — {_today}" in _bt
                    else "📋 daily-brief.md (вчерашен — ще се опресни сутринта)")
            if _notes:
                _msg += f" · {len(_notes)} {{open note}} за обработка"
            alerts.append(_msg)
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
