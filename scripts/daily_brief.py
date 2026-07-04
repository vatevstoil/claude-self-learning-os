#!/usr/bin/env python3
"""daily_brief.py — Generate a morning brief note in Obsidian.

Inspired by Nick Milo's AIOS "daily brief": one note you read each morning with
system momentum, today's 2-3 priorities, what's pending, and an OPEN NOTES area
where you can leave ``{async notes}`` for the AI to pick up next session.

Pulls only from existing local state (no LLM, no Pinecone embedding) — safe to
run on the scheduler. Writes ``{{WIKI_PATH}}\\_meta\\daily-brief.md``.

Curly-bracket convention: anything you type inside ``{ ... }`` in the OPEN NOTES
section is captured on the next run — logged to ``logs/open-notes.jsonl`` and
carried into the brief's "captured" list so it isn't lost.

Usage:
    python daily_brief.py                # generate today's brief
    python daily_brief.py --print        # also print to stdout
Never raises a non-zero exit — safe for the dispatcher.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

HOME = Path.home()
LOGS = HOME / ".claude" / "logs"
SKILLS = HOME / ".claude" / "skills"
OUT = Path(r"{{WIKI_PATH}}\_meta\daily-brief.md")
OPEN_NOTES_LOG = LOGS / "open-notes.jsonl"

# Placeholder content that must NOT be treated as a real user note.
_PLACEHOLDER = re.compile(r"^[\s.…·\-]*$")
_CURLY = re.compile(r"\{([^{}]*)\}")


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _growth_pulses() -> list[dict]:
    """One entry per project that has a GROWTH_NEXT_STEPS.md (written by /growth).

    Self-contained: reads the growth-loop config to find each project's output
    file; silent if the config or artifacts are absent (an unrun loop shows
    nothing rather than a stale claim). Returns dicts: name, week_of, actions,
    verdict, path.
    """
    cfg = _load_json(SKILLS / "growth-loop" / "projects.json", {})
    projects = (cfg or {}).get("projects", {})
    if not isinstance(projects, dict):
        return []
    # CHECK-beat eval history per project (loop-engineering verifiable gate, written
    # by growth_runner._write_eval). Append-only chronological. We keep the full
    # history per project so we can surface both the latest verdict AND a sustained
    # regression (the eval-before-promote signal: a loop degrading over weeks).
    check_hist: dict[str, list] = {}
    try:
        for line in (LOGS / "growth-eval.jsonl").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("project"):
                check_hist.setdefault(rec["project"], []).append(rec)
    except Exception:
        pass
    out: list[dict] = []
    for key, p in projects.items():
        outfile = (p or {}).get("output")
        if not outfile:
            continue
        try:
            text = Path(outfile).read_text(encoding="utf-8")
        except Exception:
            continue
        wk = re.search(r"week_of:\s*([0-9-]+)", text)
        # verdict: prefer frontmatter (what the runner writes), fall back to a header
        verdict = re.search(r"(?m)^verdict:\s*(.+)$", text) or re.search(r"##\s*Verdict:\s*(.+)", text)
        # ranked actions are table rows "| <n> |" (same regex as growth_runner CHECK beat)
        actions = len(re.findall(r"(?m)^\|\s*\d+\s*\|", text))
        hist = check_hist.get(key, [])
        chk = hist[-1] if hist else {}
        # Sustained regression: the last >=2 runs both failed to cleanly PASS.
        last_verdicts = [r.get("verdict") for r in hist[-3:]]
        recent = [v for v in last_verdicts if v]
        regressed = len(recent) >= 2 and all(v != "PASS" for v in recent[-2:])
        out.append({
            "name": (p or {}).get("display_name", key),
            "week_of": wk.group(1) if wk else "?",
            "actions": actions,
            "verdict": (verdict.group(1).strip() if verdict else "")[:90],
            "check_verdict": chk.get("verdict", ""),
            "check_score": chk.get("score"),
            "check_issues": chk.get("issues") or [],
            "check_regressed": regressed,
            "check_trend": recent,
            "path": outfile,
        })
    return out


def extract_open_notes(text: str) -> list[str]:
    """Return non-placeholder ``{...}`` notes the user left in a brief.

    Strips HTML comments first (so the template's own example braces are not
    captured) and skips empty / dots-only placeholders like ``{ }`` or ``{…}``.
    Pure function."""
    clean = re.sub(r"<!--.*?-->", "", text or "", flags=re.DOTALL)
    notes: list[str] = []
    for m in _CURLY.findall(clean):
        if not _PLACEHOLDER.match(m):
            notes.append(m.strip())
    return notes


def _proj_short(name: str) -> str:
    return (name or "").replace("J--Antigraviti-", "").replace("j--Antigraviti-", "") \
                       .replace("J--Obsidian-Resurch-", "")


def top_priorities(queue: list, stale: list, queue_depth: int, k: int = 3,
                   incidents: list | None = None) -> list[str]:
    """Synthesize today's 2-3 priorities from system state. Pure function.

    Open incidents (repeated user corrections = unresolved bugs) outrank
    everything else — they are the strongest pain signal the system has.
    """
    out: list[str] = []
    for inc in (incidents or [])[:2]:
        out.append(f"🚨 Инцидент **{_proj_short(inc.get('project', '?'))}** "
                   f"×{inc.get('count', '?')}: {inc.get('title', '')[:70]}")
    if queue_depth:
        out.append(f"♻️ {queue_depth} queued Pinecone save(s) — `pinecone.py replay-queue` "
                   f"(auto-retries; needs quota back)")
    for s in stale[:2]:
        proj = s.get("project", "?") if isinstance(s, dict) else str(s)
        out.append(f"🗺️ Refresh stale graph: **{proj}** (`/graphify` in its session)")
    # highest-scored improvement-queue items not already covered
    def _score(it):
        try:
            return float(it.get("score", 0))
        except Exception:
            return 0.0
    for it in sorted([q for q in queue if isinstance(q, dict)], key=_score, reverse=True):
        if len(out) >= k:
            break
        proj = (it.get("project") or "").replace("J--Antigraviti-", "").replace("j--Antigraviti-", "")
        label = it.get("title") or it.get("suggestion") or it.get("type") or it.get("id", "")
        out.append(f"⚡ {label} · {proj} (score {it.get('score','?')})")
    return out[:k]


def _short_id(item_id: str) -> str:
    """Short review id shown in the brief — MUST stay identical to
    suggestion_feedback._short_id (md5 hex, first 8 chars) so the printed
    accept/reject commands resolve."""
    import hashlib
    return hashlib.md5(item_id.encode()).hexdigest()[:8]


def _item_type_from_id(item_id: str) -> str:
    """Infer item_type from item id prefix for the accept command.

    Covers the known prefixes produced by self_improvement_queue:
    boris-, habit-, promo-, graphify-, discipline-.  Falls back to empty string.

    Args:
        item_id: Queue item identifier string.

    Returns:
        Type string suitable for ``suggestion_feedback.py accept --type``.
    """
    if item_id.startswith("boris-"):
        return "boris_rule"
    if item_id.startswith("habit-"):
        return "habit"
    if item_id.startswith("promo-"):
        return "promotion"
    if item_id.startswith("graphify-"):
        return "graphify"
    if item_id.startswith("discipline-"):
        return "discipline_gap"
    return ""


def build_brief(now: datetime, health: dict, stale: list, priorities: list[str],
                anticipations: dict, queue_depth: int, carried_notes: list[str],
                *, incidents: list | None = None, silenced_count: int = 0,
                kpi: dict | None = None,
                escalation: dict | None = None,
                auto_applied: list | None = None,
                review_items: list | None = None,
                fix_proposals: list | None = None,
                aging: dict | None = None,
                skills_audit: dict | None = None,
                integrity: dict | None = None,
                doctor: dict | None = None,
                gemini_briefs: int = 0) -> str:
    """Assemble the brief markdown. Pure function (deterministic given inputs)."""
    date = now.strftime("%Y-%m-%d")
    hm = now.strftime("%H:%M")
    grade = health.get("grade", "?")
    score = health.get("overall", health.get("score", "?"))

    lines = [
        f"# Daily Brief — {date}",
        f"> generated {hm} · selfreg **{grade}** ({score}/100)",
    ]

    # Incidents first — repeated corrections are unresolved bugs, the single
    # strongest signal the system has. They must be impossible to miss.
    if incidents:
        lines += ["", "## 🚨 Инциденти (повтарящи се проблеми — нерешени бъгове)"]
        for inc in incidents[:5]:
            lines.append(f"- **{_proj_short(inc.get('project', '?'))}** "
                         f"×{inc.get('count', '?')} (от {str(inc.get('first_seen', '?'))[:10]}): "
                         f"{inc.get('title', '')}")

    # "resolution: silence" means the signal stopped, NOT that anyone fixed it —
    # rendered even with zero open incidents (that combination IS the blind spot).
    if silenced_count:
        lines.append(f"- 🤫 {silenced_count} инцидент(а) затворени по замлъкване (без потвърден fix) — "
                     f"провери реално ли са решени: `python ~/.claude/scripts/incident_tracker.py`")

    # Drafted, human-gated fix sessions awaiting approval (never auto-run).
    if fix_proposals:
        lines.append(f"- 🔧 {len(fix_proposals)} предложен(и) fix-сесия(и) за преглед "
                     f"→ `python ~/.claude/scripts/incident_fix_proposer.py --list`")

    # Integrity violations — the system measuring itself with broken rulers.
    # Surface only critical/high (medium/low live in the full report).
    if integrity:
        ic = integrity.get("counts", {}) or {}
        sev = int(ic.get("critical", 0)) + int(ic.get("high", 0))
        if sev:
            top = next((v for v in (integrity.get("violations") or [])
                        if v.get("severity") in ("critical", "high")), {})
            lines.append(f"- 🧮 **{sev} integrity нарушение(я)** (crit/high) — "
                         f"напр. {top.get('check', '?')}: {str(top.get('detail', ''))[:90]} "
                         f"→ `python ~/.claude/scripts/integrity_guard.py`")

    lines += ["", "## 🎯 Днес (top 3)"]
    lines += [f"- {p}" for p in priorities] or ["- *(чисто — няма наложащи действия)*"]

    lines += ["", "## 📊 Система"]
    lines.append(f"- Health: **{grade}** ({score}/100)")
    # Memory backend (Ollama + bge-m3) status — ollama-doctor-last.json. A dead
    # backend means every save silently queues; it must be visible here daily.
    d = doctor or {}
    d_ts_raw = str(d.get("ts", ""))
    d_ts = d_ts_raw[:16].replace("T", " ") if d_ts_raw else "—"
    d_fresh = False
    try:
        dts = datetime.fromisoformat(d_ts_raw)
        if dts.tzinfo is None:
            dts = dts.replace(tzinfo=timezone.utc)
        d_fresh = (now - dts).total_seconds() < 26 * 3600
    except Exception:
        pass
    if not d:
        lines.append("- 🧠 Memory backend: ❓ няма doctor диагностика")
    elif d.get("ok") and d_fresh:
        lines.append("- 🧠 Memory backend: ✓")
    elif d.get("ok"):
        lines.append(f"- 🧠 Memory backend: ❓ doctor не е репортвал от {d_ts}")
    else:
        lines.append(f"- 🧠 Memory backend: ❌ DOWN — stage={d.get('stage', '?')} "
                     f"reason={d.get('reason', '?')} (от {d_ts})")
    if escalation:
        ok = escalation.get("success") and escalation.get("recheck_ok")
        mark = "✅ оздравено" if ok else "❌ НЕУСПЕШНО — нужна ръчна намеса"
        lines.append(f"- 🛠 Самолечение: {escalation.get('action', '?')} → {mark} "
                     f"({str(escalation.get('ts', ''))[:16]})")
    # Hermes agent liveness — read-only pulse written by the dispatcher before this
    # brief runs. A down gateway is surfaced here (visible), never silently dark.
    try:
        _hp = json.loads((HOME / ".claude" / "logs" / "hermes_pulse.json")
                         .read_text(encoding="utf-8"))
    except Exception:
        _hp = None
    if _hp:
        _hg = _hp.get("gateway", "unknown")
        if _hg == "running":
            lines.append(f"- 🤖 Hermes: ✓ (Telegram: {_hp.get('telegram', '?')})")
        elif _hg == "stopped":
            lines.append("- 🤖 Hermes: ⚠️ gateway СПРЯН — стартирай `Hermes.lnk` или tray ▶ Start")
        else:
            lines.append("- 🤖 Hermes: ❓ pulse неясен (виж ~/.claude/logs/hermes_pulse.json)")
    if stale:
        names = ", ".join((s.get("project", "?") if isinstance(s, dict) else str(s)) for s in stale)
        lines.append(f"- Stale графи: {names}")
    else:
        lines.append("- Stale графи: няма ✓")
    if queue_depth:
        lines.append(f"- ⚠️ Pinecone save queue: **{queue_depth}** (quota outage — нищо не се губи)")
    issues = (health.get("issues") or {})
    err = issues.get("errors") if isinstance(issues, dict) else None
    if err:
        lines.append(f"- Errors: {'; '.join(err)[:160]}")
    hook_iss = issues.get("hooks") if isinstance(issues, dict) else None
    if hook_iss:
        lines.append(f"- Hook здраве: {'; '.join(hook_iss)[:160]}")
    # Stale human-review queues (queue_aging weekly DRY report).
    stale_q = (aging or {}).get("stale") or []
    if stale_q:
        lines.append(f"- 🗂 {len(stale_q)} застояли review файла (>7д) → "
                     f"`python ~/.claude/scripts/queue_aging.py --apply` или прегледай")
    # Dead skills are a LEAD for manual pruning, never auto-deleted.
    # "dead" is a list of names in skill-usage-audit.json (tolerate int too).
    dead_skills = (skills_audit or {}).get("dead") or 0
    dead_count = len(dead_skills) if isinstance(dead_skills, (list, tuple)) else int(dead_skills)
    if dead_count:
        lines.append(f"- 🪦 {dead_count} неизползвани skills/commands → "
                     f"`python ~/.claude/scripts/skill_usage_audit.py` (само преглед)")
    if gemini_briefs:
        lines.append(f"- 📤 {gemini_briefs} Gemini brief(s) готови за изпращане → "
                     f"~/.claude/gemini-tasks/")

    # anticipations: one top routine per up-to-3 active projects
    if anticipations:
        lines += ["", "## 🔮 Очаквано (по навик)"]
        shown = 0
        for proj, routines in anticipations.items():
            if shown >= 3 or not routines:
                continue
            r = routines[0]
            chain = " → ".join(r.get("routine", [])) or "?"
            nm = proj.replace("J--Antigraviti-", "").replace("j--Antigraviti-", "")
            lines.append(f"- **{nm}**: {chain} → _{r.get('next','?')}_ ({r.get('confidence',0):.0%})")
            shown += 1

    # Outcome KPI — does the system actually LEARN? (results, not activity)
    if kpi:
        rc = kpi.get("repeat_corrections") or {}
        re_ = kpi.get("recall_engagement") or {}
        af = kpi.get("apply_funnel") or {}
        trend = {"improving": "📉 подобрява се", "degrading": "📈 ВЛОШАВА СЕ",
                 "flat": "→ без промяна"}.get(kpi.get("trend", ""), "· няма данни")
        lines += ["", "## 📈 Учене (outcome KPI)"]
        lines.append(f"- Повторени корекции: **{rc.get('repeats', '?')}/{rc.get('total', '?')}**"
                     f" ({(rc.get('rate') or 0) * 100:.0f}%) {trend}")
        if re_.get("surfaced") is not None:
            lines.append(f"- Recall полезност: {re_.get('engaged', '?')}/{re_.get('surfaced', '?')}"
                         f" ({(re_.get('rate') or 0) * 100:.1f}%)")
        lines.append(f"- Фуния: {af.get('applied_30d', 0)} приложени (30d) · "
                     f"{af.get('queue_depth', '?')} в опашка · "
                     f"{af.get('open_incidents', 0)} отворени инцидента")

    # 🧭 Discipline pulse — Fable-mindset measured habits, refreshed weekly by the
    # dispatcher (discipline_analyzer → discipline_stats.json). Self-contained:
    # renders only when the stats file exists, so an unwired/failed loop shows nothing.
    disc = _load_json(LOGS / "discipline_stats.json", {})
    dt = (disc or {}).get("target") or {}
    db = (disc or {}).get("baseline") or {}
    if dt.get("model"):
        lines += ["", "## 🧭 Дисциплина (Fable-mindset)"]
        lines.append(
            f"- **{dt.get('model', '?')}** vs {db.get('model', '?')}: "
            f"reason>act {dt.get('reason_before_action_pct', '?')}% · "
            f"read>edit {dt.get('read_before_edit_pct', '?')}% · "
            f"test>edit {dt.get('real_test_after_edit_pct', '?')}% · "
            f"abs-path {dt.get('abs_path_hygiene_pct', '?')}%")
        tte = dt.get("real_test_after_edit_pct")
        if isinstance(tte, (int, float)) and tte < 50:
            lines.append("  ↳ test>edit под 50% — пусни реалния тест след edit "
                         "(виж `/mindset wire`)")

    # 🔄 Dreaming loop verdict — Loop-Engineering memory between cycles. The
    # dreaming run writes dreaming-next-steps.json (verdict + open arcs); the
    # brief surfaces "iterate" so a degraded self-improvement loop is visible in
    # the morning, not only in health.json. Self-contained: silent if no file.
    dns = _load_json(LOGS / "dreaming-next-steps.json", {})
    if (dns or {}).get("verdict") == "iterate":
        arcs = ", ".join(dns.get("open_arcs") or []) or "—"
        lines += ["", "## 🔄 Dreaming цикъл"]
        lines.append(
            f"- **iterate** — {dns.get('steps_failed', '?')}/{dns.get('steps_total', '?')} "
            f"стъпки паднаха (арки: {arcs}) → виж `logs/automation.log` + `health.json`")

    # 📈 Growth loops — weekly /growth output per project. Self-contained: renders
    # only for projects whose GROWTH_NEXT_STEPS.md exists. Flags a stale week.
    growth = _growth_pulses()
    if growth:
        lines += ["", "## 📈 Growth loops"]
        for g in growth:
            stale = ""
            try:
                wk = datetime.strptime(g["week_of"], "%Y-%m-%d").date()
                if (now.date() - wk).days > 9:
                    stale = " ⚠️ остаряло — пусни `/growth`"
            except Exception:
                pass
            # CHECK-beat quality flag — surface a degraded/failed loop run.
            cv = g.get("check_verdict")
            cflag = ""
            if cv == "FAIL":
                cflag = " 🔴 CHECK FAIL — изходът е негоден, пусни наново"
            elif cv == "WEAK":
                iss = "; ".join(g.get("check_issues") or []) or "деградиран"
                cflag = f" 🟡 CHECK WEAK ({g.get('check_score')}) — {iss}"
            # Sustained regression across runs — the eval-before-promote alarm.
            if g.get("check_regressed"):
                cflag += (f" ⛔ РЕГРЕСИЯ — тренд {'/'.join(g.get('check_trend') or [])} "
                          f"(последни 2 не са PASS) → ревизирай loop-а")
            lines.append(
                f"- **{g['name']}** (седмица {g['week_of']}): {g['actions']} действия — "
                f"{g['verdict']}{stale}{cflag}")
            lines.append(f"  → `{g['path']}`")

    # 🧪 A/B counterfactual — surface regressed / no-effect auto-rules so the causal
    # verdict reaches the morning brief (not only the manually-opened dashboard).
    abev = _load_json(LOGS / "ab-eval.json", {})
    absum = (abev or {}).get("summary") or {}
    if (absum.get("decided") or 0) > 0 and ((absum.get("regressed") or 0) or (absum.get("no_effect") or 0)):
        lines += ["", "## 🧪 A/B на правилата (каузален ефект)"]
        lines.append(
            f"- {absum.get('regressed', 0)} регресирали · {absum.get('no_effect', 0)} без ефект · "
            f"{absum.get('effective', 0)} ефективни (от {absum.get('decided', 0)} решени) → "
            f"преглед: `python ~/.claude/scripts/ab_eval.py`")

    # Auto-applied items awaiting human glance (Tier 1 notifications).
    if auto_applied:
        lines += ["", "## ✅ Авто-приложени (tier 1 — прегледай при възможност)"]
        for a in auto_applied[-5:]:
            # trust_tiers pending-review entries use "type"; ledger uses "item_type"
            kind = a.get("item_type") or a.get("type", "?")
            lines.append(f"- {kind} `{a.get('item_id', '?')}` → "
                         f"{a.get('target_file', '?')} ({str(a.get('ts', ''))[:16]})")
        lines.append("- ↩️ Отмяна: `python ~/.claude/scripts/trust_tiers.py rollback`")

    # Review section — top-3 actionable items with exact accept/reject commands.
    # This is THE bootstrap lever: accept/reject feeds precision_by_type, which
    # is the only thing that can raise a trust tier above 0.
    # Must NOT contain capturable {braces} — strip them from user-controlled fields.
    if review_items:
        lines += ["", "## 📝 Review (2 мин — това вдига trust tier-а)"]
        for it in review_items[:3]:
            item_id = it.get("id", "")
            sid = _short_id(item_id)
            item_score = it.get("score", "?")
            verdict = it.get("judge_verdict") or "не е оценено"
            raw_desc = (it.get("description") or it.get("title") or it.get("type") or item_id)[:80]
            # Strip curly braces so user-supplied content cannot inject capturable {notes}
            desc = raw_desc.replace("{", "(").replace("}", ")")
            lines.append(f"- [ ] `{sid}` — {desc} (score {item_score}, judge: {verdict})")
        lines.append("  Приеми/откажи: `python ~/.claude/scripts/suggestion_feedback.py "
                     "review --accept <id>` / `review --reject <id>` "
                     "(списък: `review --list`)")

    if carried_notes:
        lines += ["", "## ⏮ Хванати бележки (от вчерашния brief)"]
        lines += [f"- 📌 {n}" for n in carried_notes]

    lines += [
        "",
        "## 📝 Open notes — остави бележка в къдрави скоби по-долу, AI я хваща утре",
        "<!-- Пиши свободни бележки в къдрави скоби, напр. напомни-ми-за-X. "
        "При следваща генерация се логват и пренасят горе. Този коментар се игнорира. -->",
        "{ }",
        "",
        "---",
        f"<!-- daily_brief.py · {now.isoformat(timespec='seconds')} -->",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", dest="do_print", action="store_true")
    args = ap.parse_args()
    now = datetime.now(timezone.utc).astimezone()

    # 1) capture {open notes} from the PREVIOUS brief before overwriting
    carried: list[str] = []
    if OUT.exists():
        prev = OUT.read_text(encoding="utf-8", errors="replace")
        # only scan below the "Open notes" header so we don't re-capture rendered ones
        seg = prev.split("## 📝 Open notes", 1)
        scan = seg[1] if len(seg) > 1 else prev
        carried = extract_open_notes(scan)
        if carried:
            try:
                OPEN_NOTES_LOG.parent.mkdir(parents=True, exist_ok=True)
                with OPEN_NOTES_LOG.open("a", encoding="utf-8") as f:
                    for n in carried:
                        f.write(json.dumps({"ts": now.isoformat(timespec="seconds"),
                                            "note": n}, ensure_ascii=False) + "\n")
            except Exception:
                pass

    # 2) gather state
    health = _load_json(LOGS / "health.json", {}) or _load_json(LOGS / "selfreg-health.json", {})
    if not health.get("grade"):
        health = _load_json(LOGS / "selfreg-health.json", {})
    fresh = _load_json(LOGS / "freshness.json", {})
    stale = fresh.get("stale", []) if isinstance(fresh, dict) else []
    queue = _load_json(LOGS / "improvement-queue.json", [])
    if not isinstance(queue, list):
        queue = queue.get("items", []) if isinstance(queue, dict) else []

    # Graphify-queue age alert: if any enrich=True entry has been waiting >14 days,
    # surface it as a priority so it does not silently accumulate (daily brief is the
    # only cross-project consumer with a time-bounded staleness check).
    _graphify_priority: str | None = None
    try:
        from datetime import date as _date
        _gq = _load_json(LOGS / "graphify-queue.json", {})
        _gq_checked = _gq.get("checked", "") if isinstance(_gq, dict) else ""
        _gq_age = (_date.today() - _date.fromisoformat(_gq_checked[:10])).days if _gq_checked else 0
        _enrich_items = [
            i for i in (_gq.get("queued_for_llm") or [])
            if isinstance(i, dict) and i.get("enrich")
        ] if isinstance(_gq, dict) else []
        if _gq_age > 14 and _enrich_items:
            _proj_names = ", ".join(i.get("project", "?") for i in _enrich_items[:3])
            _graphify_priority = (
                f"GRAPH ENRICHMENT: {_proj_names} — run /graphify (queued {_gq_age}d)"
            )
    except Exception:
        pass

    anticipations = _load_json(LOGS / "anticipations.json", {})
    pend = LOGS / "pending-saves.jsonl"
    queue_depth = 0
    if pend.exists():
        queue_depth = len([l for l in pend.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()])

    incidents_doc = _load_json(LOGS / "incidents.json", {}) or {}
    incidents = incidents_doc.get("open", [])
    silenced = sum(1 for r in (incidents_doc.get("resolved_recent") or [])
                   if isinstance(r, dict) and r.get("resolution") == "silence")
    fix_proposals = [p for p in (_load_json(LOGS / "fix-proposals.json", {}) or {}).get("proposals", [])
                     if isinstance(p, dict) and p.get("status") == "proposed"]
    kpi = _load_json(LOGS / "outcome-kpi.json", {}) or None
    aging = _load_json(LOGS / "queue-aging.json", {}) or None
    skills_audit = _load_json(LOGS / "skill-usage-audit.json", {}) or None
    auto_applied = _load_json(LOGS / "auto-applied-pending-review.json", [])
    if not isinstance(auto_applied, list):
        auto_applied = []
    # Escalation is shown only while fresh (48h) — stale heal events are noise.
    escalation = _load_json(LOGS / "escalation.json", {}) or None
    if escalation:
        try:
            ets = datetime.fromisoformat(escalation.get("ts", ""))
            if ets.tzinfo is None:
                ets = ets.replace(tzinfo=timezone.utc)
            if (now - ets).total_seconds() > 48 * 3600:
                escalation = None
        except Exception:
            escalation = None

    # Memory backend health — written by ollama_doctor on every run.
    doctor = _load_json(LOGS / "ollama-doctor-last.json", {}) or None

    priorities = top_priorities(queue, stale, queue_depth, incidents=incidents)
    if doctor and not doctor.get("ok"):
        # A dead memory backend blocks every save — always priority #1.
        priorities.insert(0, "🧠 Memory backend DOWN → `python ~/.claude/scripts/ollama_doctor.py --ensure`")
        priorities = priorities[:3]
    # Graphify enrichment alert: inject before truncation if queued >14 days.
    # Placed after doctor (backend down outranks everything) but before other items.
    if _graphify_priority and len(priorities) < 3:
        priorities.append(_graphify_priority)
    elif _graphify_priority:
        priorities.insert(-1, _graphify_priority)
        priorities = priorities[:3]

    # Top-5 review items for the actionable Review section.
    # Sort by score desc; only include items with a usable id.
    def _item_score(it: dict) -> float:
        try:
            return float(it.get("score", 0))
        except Exception:
            return 0.0

    # Mirror suggestion_feedback._pending_items: auto_apply items run on their
    # own; accepted/rejected are already reviewed — neither belongs in Review.
    review_items_raw = [
        it for it in queue
        if isinstance(it, dict) and it.get("id")
        and it.get("status") not in ("accepted", "rejected", "auto_apply")
    ]
    review_items_raw.sort(key=_item_score, reverse=True)
    review_items = review_items_raw[:5] if review_items_raw else None

    integrity = _load_json(LOGS / "integrity-report.json", {}) or None

    # Count pending Gemini briefs (missing dir → 0, never crash)
    gemini_briefs = 0
    try:
        gemini_tasks_dir = HOME / ".claude" / "gemini-tasks"
        if gemini_tasks_dir.is_dir():
            gemini_briefs = len(list(gemini_tasks_dir.glob("*.md")))
    except Exception:
        pass

    md = build_brief(now, health, stale, priorities, anticipations, queue_depth, carried,
                     incidents=incidents, silenced_count=silenced,
                     kpi=kpi, escalation=escalation,
                     auto_applied=auto_applied, review_items=review_items,
                     fix_proposals=fix_proposals, aging=aging,
                     skills_audit=skills_audit, integrity=integrity, doctor=doctor,
                     gemini_briefs=gemini_briefs)

    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(md, encoding="utf-8")
        print(f"daily_brief: wrote {OUT} ({len(priorities)} priorities, "
              f"{len(carried)} carried notes)")
    except Exception as exc:
        print(f"daily_brief: cannot write {OUT}: {exc}")
    if args.do_print:
        print("\n" + md)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"daily_brief: {e}")
        sys.exit(0)
