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


def build_brief(now: datetime, health: dict, stale: list, priorities: list[str],
                anticipations: dict, queue_depth: int, carried_notes: list[str],
                *, incidents: list | None = None, kpi: dict | None = None,
                escalation: dict | None = None,
                auto_applied: list | None = None) -> str:
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

    lines += ["", "## 🎯 Днес (top 3)"]
    lines += [f"- {p}" for p in priorities] or ["- *(чисто — няма наложащи действия)*"]

    lines += ["", "## 📊 Система"]
    lines.append(f"- Health: **{grade}** ({score}/100)")
    if escalation:
        ok = escalation.get("success") and escalation.get("recheck_ok")
        mark = "✅ оздравено" if ok else "❌ НЕУСПЕШНО — нужна ръчна намеса"
        lines.append(f"- 🛠 Самолечение: {escalation.get('action', '?')} → {mark} "
                     f"({str(escalation.get('ts', ''))[:16]})")
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

    # Auto-applied items awaiting human glance (Tier 1 notifications).
    if auto_applied:
        lines += ["", "## ✅ Авто-приложени (tier 1 — прегледай при възможност)"]
        for a in auto_applied[-5:]:
            # trust_tiers pending-review entries use "type"; ledger uses "item_type"
            kind = a.get("item_type") or a.get("type", "?")
            lines.append(f"- {kind} `{a.get('item_id', '?')}` → "
                         f"{a.get('target_file', '?')} ({str(a.get('ts', ''))[:16]})")
        lines.append("- ↩️ Отмяна: `python ~/.claude/scripts/trust_tiers.py rollback`")

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
    anticipations = _load_json(LOGS / "anticipations.json", {})
    pend = LOGS / "pending-saves.jsonl"
    queue_depth = 0
    if pend.exists():
        queue_depth = len([l for l in pend.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()])

    incidents = (_load_json(LOGS / "incidents.json", {}) or {}).get("open", [])
    kpi = _load_json(LOGS / "outcome-kpi.json", {}) or None
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

    priorities = top_priorities(queue, stale, queue_depth, incidents=incidents)
    md = build_brief(now, health, stale, priorities, anticipations, queue_depth, carried,
                     incidents=incidents, kpi=kpi, escalation=escalation,
                     auto_applied=auto_applied)

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
