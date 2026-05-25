#!/usr/bin/env python3
"""stage3_dreaming.py - Weekly self-learning analysis (the "dreaming" engine).

Implements ~7 of Jack Roberts' 8 Claude-OS dreaming dimensions:
  1 conversation analysis · 2 cost intelligence · 3 skill performance ·
  4 memory health (reuses self-regulation signals) · 5 session hygiene ·
  6 workflow patterns (skill candidates) · 8 business context.
  (7 external-opportunities intentionally omitted — needs live web, low ROI.)
See: J:\\Obsidian Resurch\\Claude Code Resurch\\wiki\\summaries\\Agentic-OS-Chase-Jack.md

Reads last N days of JSONL transcripts (incl. per-message usage) + skills + the
self-regulation signal files; distills everything into 4 high-leverage actions.

Usage:
    python stage3_dreaming.py                 # default 7 days
    python stage3_dreaming.py --days 30       # last 30 days
    python stage3_dreaming.py --json          # JSON to stdout
    python stage3_dreaming.py --out path.md   # custom output
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
SKILLS_DIR = Path.home() / ".claude" / "skills"
REPORTS_DIR = Path.home() / ".claude" / "reports"
LOGS_DIR = Path.home() / ".claude" / "logs"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# API-equivalent prices (USD per 1M tokens). Notional on a Pro/Vertex plan —
# used as an OPTIMIZATION signal (model mix, cache efficiency), not a real bill.
PRICES = {
    "opus":   {"in": 15.0, "out": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "sonnet": {"in": 3.0,  "out": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
    "haiku":  {"in": 0.80, "out": 4.0,  "cache_write": 1.0,   "cache_read": 0.08},
}


def model_family(model: str) -> str:
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    return "sonnet"  # default / unknown → sonnet pricing


def cost_of(family: str, u: dict) -> float:
    p = PRICES.get(family, PRICES["sonnet"])
    return (u.get("input_tokens", 0) * p["in"]
            + u.get("output_tokens", 0) * p["out"]
            + u.get("cache_creation_input_tokens", 0) * p["cache_write"]
            + u.get("cache_read_input_tokens", 0) * p["cache_read"]) / 1_000_000


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}

# Stop-words for clustering (BG + EN, common phrases)
STOP_WORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "should",
    "could", "may", "might", "must", "can", "this", "that", "these", "those",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us", "them",
    "my", "your", "his", "her", "its", "our", "their",
    "of", "in", "on", "at", "to", "for", "with", "from", "by", "as", "and", "or",
    "but", "not", "no", "yes", "ok", "ok,", "please", "thanks", "thank",
    # BG common
    "е", "са", "беше", "били", "ще", "трябва", "може", "мога", "правя",
    "това", "тази", "този", "те", "тях", "мен", "ми", "ти", "ние",
    "от", "до", "на", "за", "с", "като", "че", "ако", "но", "или",
    "не", "да", "така", "много", "както", "става", "иска", "искам",
    "ми", "си", "по", "над", "под", "до", "пред", "след",
    "направи", "направя", "провери", "виж", "погледни", "кажи", "напиши",
    "малко", "много", "сега", "после", "преди", "вече", "още",
}

# Action verbs that suggest repeatable tasks (potential skill candidates)
ACTION_PATTERNS = [
    (r"\b(test|provey|проверявам|провери)\b", "testing"),
    (r"\b(fix|поправи|оправи)\b", "fixing"),
    (r"\b(deploy|deploy)\b", "deployment"),
    (r"\b(refactor|рефактори|опрости)\b", "refactoring"),
    (r"\b(review|преглед|ревюирай)\b", "code-review"),
    (r"\b(migrate|мигрирай|миграция)\b", "migration"),
    (r"\b(generate|генерирай|създай)\b", "generation"),
    (r"\b(analyze|анализирай|анализ)\b", "analysis"),
    (r"\b(setup|setup|конфигурирай|настрой)\b", "setup"),
    (r"\b(audit|audit|одит)\b", "audit"),
    (r"\b(format|форматирай)\b", "formatting"),
    (r"\b(translate|преведи)\b", "translation"),
]

CORRECTION_PATTERNS = [
    r"\bне\b", r"\bгрешно\b", r"\bwrong\b", r"\bстоп\b", r"\bстоп\b",
    r"\bno that's\b", r"\bне така\b", r"\bстоп\b", r"\bне правилно\b",
]


@dataclass
class DreamReport:
    days: int
    sessions_analyzed: int = 0
    user_prompts_analyzed: int = 0
    corrections_detected: int = 0
    intent_clusters: dict[str, int] = field(default_factory=dict)
    skill_candidates: list[tuple[str, int]] = field(default_factory=list)
    stale_skills: list[tuple[str, int]] = field(default_factory=list)
    most_common_terms: list[tuple[str, int]] = field(default_factory=list)
    project_activity: dict[str, int] = field(default_factory=dict)
    correction_examples: list[str] = field(default_factory=list)
    # Dimension 2 — cost intelligence
    cost: dict = field(default_factory=dict)
    # Dimension 4 — memory health (reuses self-regulation signals)
    memory_health: dict = field(default_factory=dict)
    # Dimension 5 — session hygiene
    session_hygiene: dict = field(default_factory=dict)
    # Dimension 8 — business context
    business_context: list[tuple[str, int]] = field(default_factory=list)
    # Synthesis — the 4 high-leverage actions Jack's framework distills to
    high_leverage: list[str] = field(default_factory=list)


def parse_timestamp(ts: str) -> datetime | None:
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).astimezone(timezone.utc)
    except Exception:
        return None


def extract_user_text(record: dict) -> str | None:
    """Extract human user prompt from JSONL record (skip tool_results)."""
    if record.get("type") != "user":
        return None
    msg = record.get("message", {})
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                # Skip tool_result entries
                if item.get("type") == "tool_result":
                    continue
                if item.get("type") == "text" and "text" in item:
                    parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts) if parts else None
    return None


def tokenize(text: str) -> list[str]:
    text = text.lower()
    # Keep BG + EN word chars
    words = re.findall(r"[a-zа-я][a-zа-я0-9-]{2,}", text, flags=re.IGNORECASE)
    return [w.lower() for w in words if w.lower() not in STOP_WORDS and len(w) > 2]


def is_correction(text: str) -> bool:
    text_lower = text.lower().strip()
    if len(text_lower) > 200:  # corrections are short
        return False
    for pattern in CORRECTION_PATTERNS:
        if re.search(pattern, text_lower):
            return True
    return False


def analyze_jsonl_files(days: int):
    """Returns dict with conversation + usage signals across all dimensions."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    term_counter = Counter()
    action_counter = Counter()
    project_activity = defaultdict(int)
    sessions = set()
    prompt_count = 0
    correction_examples = []
    corrections_by_project = defaultdict(list)  # Boris: per-project correction texts
    # Cost / session signals
    usage_by_model = defaultdict(lambda: defaultdict(int))   # family -> usage totals
    session_peak = defaultdict(int)                           # sessionId -> peak context size (1 turn)
    assistant_msgs = 0
    tool_seqs_by_session: dict = {}

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        project_name = project_dir.name
        for jsonl_file in project_dir.glob("*.jsonl"):
            # Quick mtime check
            try:
                mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    continue
            except Exception:
                continue

            try:
                with jsonl_file.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue

                        # Filter by record timestamp if present
                        ts = parse_timestamp(rec.get("timestamp", ""))
                        if ts and ts < cutoff:
                            continue

                        # Cost / session usage (assistant records carry usage)
                        if rec.get("type") == "assistant":
                            msg = rec.get("message", {})
                            # sid must be defined for BOTH usage and tool-collection blocks
                            sid = rec.get("sessionId", str(jsonl_file))
                            u = msg.get("usage", {}) or {}
                            if u:
                                fam = model_family(msg.get("model", ""))
                                assistant_msgs += 1
                                for k in ("input_tokens", "output_tokens",
                                          "cache_creation_input_tokens", "cache_read_input_tokens"):
                                    usage_by_model[fam][k] += u.get(k, 0) or 0
                                # peak context = largest single-turn window (input + cached),
                                # NOT a sum of cache re-reads (which over-counts massively)
                                ctx = ((u.get("input_tokens", 0) or 0)
                                       + (u.get("cache_read_input_tokens", 0) or 0)
                                       + (u.get("cache_creation_input_tokens", 0) or 0))
                                if ctx > session_peak[sid]:
                                    session_peak[sid] = ctx
                            # Collect tool sequences for habit mining (single JSONL pass)
                            try:
                                from habit_miner import tool_signature as _tool_sig, GAP_MINUTES as _GAP
                            except Exception:
                                def _tool_sig(n, i=None): return n  # type: ignore[misc]
                                _GAP = 20
                            content_for_tools = msg.get("content") or []
                            ts_for_tools = rec.get("timestamp", "")
                            tools_this_turn = [
                                _tool_sig(item["name"], item.get("input") or {})
                                for item in content_for_tools
                                if isinstance(item, dict)
                                and item.get("type") == "tool_use"
                                and item.get("name", "")
                            ]
                            if tools_this_turn:
                                if sid not in tool_seqs_by_session:
                                    tool_seqs_by_session[sid] = {
                                        "project": project_name, "tools": [], "last_seen": "",
                                        "_last_ts": None}
                                sess = tool_seqs_by_session[sid]
                                # Insert __BREAK__ only on idle gaps > GAP_MINUTES
                                # (design change 2 — NOT after every assistant turn)
                                last_ts_str = sess.get("_last_ts")
                                if last_ts_str and ts_for_tools:
                                    try:
                                        last_dt = datetime.fromisoformat(
                                            last_ts_str.replace("Z", "+00:00"))
                                        cur_dt = datetime.fromisoformat(
                                            ts_for_tools.replace("Z", "+00:00"))
                                        gap_secs = (cur_dt - last_dt).total_seconds()
                                        if gap_secs > _GAP * 60 and sess["tools"]:
                                            sess["tools"].append("__BREAK__")
                                    except Exception:
                                        pass
                                sess["tools"].extend(tools_this_turn)
                                sess["_last_ts"] = ts_for_tools
                                if ts_for_tools > sess["last_seen"]:
                                    sess["last_seen"] = ts_for_tools
                            continue

                        text = extract_user_text(rec)
                        if not text:
                            continue

                        sessions.add(rec.get("sessionId", str(jsonl_file)))
                        prompt_count += 1
                        project_activity[project_name] += 1

                        # Token analysis
                        tokens = tokenize(text)
                        term_counter.update(tokens)

                        # Action detection
                        for pattern, action in ACTION_PATTERNS:
                            if re.search(pattern, text, flags=re.IGNORECASE):
                                action_counter[action] += 1

                        # Correction detection
                        if is_correction(text):
                            if len(correction_examples) < 10:
                                correction_examples.append(text[:120])
                            if len(corrections_by_project[project_name]) < 8:
                                corrections_by_project[project_name].append(text[:160])
            except Exception:
                continue

    return {
        "term_counter": term_counter,
        "action_counter": action_counter,
        "project_activity": dict(project_activity),
        "sessions": len(sessions),
        "prompts": prompt_count,
        "corrections": correction_examples,
        "corrections_by_project": {k: v for k, v in corrections_by_project.items()},
        "usage_by_model": {k: dict(v) for k, v in usage_by_model.items()},
        "session_peak": dict(session_peak),
        "assistant_msgs": assistant_msgs,
        "tool_seqs_by_session": tool_seqs_by_session,
    }


def get_skill_health(stale_days: int = 30) -> list[tuple[str, int]]:
    """Return [(skill_name, days_since_mtime), ...] sorted by stale."""
    if not SKILLS_DIR.exists():
        return []
    now = datetime.now(timezone.utc)
    stale = []
    for skill_dir in SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            mtime = datetime.fromtimestamp(skill_md.stat().st_mtime, tz=timezone.utc)
            age_days = (now - mtime).days
            if age_days >= stale_days:
                stale.append((skill_dir.name, age_days))
        except Exception:
            continue
    return sorted(stale, key=lambda x: -x[1])


def make_skill_candidates(term_counter: Counter, action_counter: Counter, threshold: int = 5) -> list[tuple[str, int]]:
    """Identify repetitive patterns that could be skills."""
    candidates = []
    for action, count in action_counter.most_common(20):
        if count >= threshold:
            candidates.append((action, count))
    return candidates


# --- Dimension 2: Cost intelligence -----------------------------------------
def analyze_cost(usage_by_model: dict) -> dict:
    """Model mix, cache efficiency, API-equivalent cost, optimization flags."""
    total_cost = 0.0
    by_family = {}
    grand_tokens = 0
    cache_read = cache_write = raw_input = 0
    for fam, u in usage_by_model.items():
        c = cost_of(fam, u)
        total_cost += c
        toks = sum(u.get(k, 0) for k in ("input_tokens", "output_tokens",
                                         "cache_creation_input_tokens", "cache_read_input_tokens"))
        grand_tokens += toks
        cache_read += u.get("cache_read_input_tokens", 0)
        cache_write += u.get("cache_creation_input_tokens", 0)
        raw_input += u.get("input_tokens", 0)
        by_family[fam] = {"cost_usd": round(c, 2), "tokens": toks}
    # cache-hit rate = reads / (reads + writes + raw input that could have been cached)
    cache_base = cache_read + cache_write + raw_input
    cache_hit = round(100 * cache_read / cache_base) if cache_base else 0
    flags = []
    # Opus concern is about COST share, not raw token share (cache reads distort tokens)
    opus_cost = by_family.get("opus", {}).get("cost_usd", 0)
    opus_cost_share = (opus_cost / total_cost) if total_cost else 0
    if opus_cost_share > 0.5:
        flags.append(f"Opus = {round(opus_cost_share*100)}% of API-equiv cost — route simple work to Sonnet/Haiku")
    if cache_base > 100_000 and cache_hit < 40:
        flags.append(f"cache-hit only {cache_hit}% — avoid editing early context; batch reads")
    return {
        "total_cost_usd": round(total_cost, 2),
        "by_family": by_family,
        "cache_hit_pct": cache_hit,
        "total_tokens": grand_tokens,
        "opus_cost_share_pct": round(opus_cost_share * 100),
        "flags": flags,
    }


# --- ROI (reads roi_tracker output) -----------------------------------------
def load_roi() -> dict:
    return _load_json(LOGS_DIR / "roi.json")


# --- Dimension 4: Memory health (reuse self-regulation signals) -------------
def analyze_memory_health() -> dict:
    fresh = _load_json(LOGS_DIR / "freshness.json")
    selfreg = _load_json(LOGS_DIR / "selfreg-health.json")
    gq = _load_json(LOGS_DIR / "graphify-queue.json")
    flags = []
    stale = fresh.get("stale_count", 0)
    total = fresh.get("total", 0)
    if stale:
        flags.append(f"{stale}/{total} wikis stale — knowledge drifting behind work")
    enrich = [q.get("project") for q in gq.get("queued_for_llm", [])]
    if enrich:
        flags.append(f"graphs need enrichment: {', '.join(filter(None, enrich))[:60]}")
    grade = selfreg.get("grade")
    return {
        "selfreg_grade": grade,
        "selfreg_overall": selfreg.get("overall"),
        "stale_wikis": stale, "total_wikis": total,
        "graphs_need_enrichment": [e for e in enrich if e],
        "flags": flags,
    }


# --- Dimension 5: Session hygiene -------------------------------------------
def analyze_session_hygiene(session_peak: dict) -> dict:
    """Uses PEAK context per session (largest single-turn window), not summed
    throughput. The Claude context window is ~200k; peaks near it = time to /clear."""
    if not session_peak:
        return {"sessions": 0, "flags": []}
    vals = sorted(session_peak.values(), reverse=True)
    n = len(vals)
    avg_peak = round(sum(vals) / n)
    near_limit = [v for v in vals if v > 160_000]   # >80% of 200k window
    flags = []
    if near_limit:
        flags.append(f"{len(near_limit)} session(s) peaked >160k context (near 200k limit) — /clear earlier")
    if avg_peak > 120_000:
        flags.append(f"avg peak context {avg_peak:,} — running hot; /clear on topic switch")
    return {"sessions": n, "avg_peak_context": avg_peak, "max_peak_context": vals[0],
            "near_limit_sessions": len(near_limit), "flags": flags}


# --- Synthesis: distill to 4 high-leverage actions --------------------------
def synthesize_actions(report: "DreamReport") -> list[str]:
    actions: list[tuple[int, str]] = []  # (priority_weight, text)
    # cost
    for f in report.cost.get("flags", []):
        actions.append((90, f"💰 {f}"))
    # memory health
    for f in report.memory_health.get("flags", []):
        actions.append((80, f"🧠 {f}"))
    # session hygiene
    for f in report.session_hygiene.get("flags", []):
        actions.append((70, f"🪟 {f}"))
    # skill candidate (top repeated action)
    if report.skill_candidates:
        a, c = report.skill_candidates[0]
        actions.append((60, f"🛠️ '{a}' done {c}× — turn into a skill (skill-creator)"))
    # corrections
    if report.corrections_detected >= 3:
        actions.append((55, f"📝 {report.corrections_detected} corrections — codify rules into CLAUDE.md (Boris)"))
    # stale skills
    if len(report.stale_skills) >= 10:
        actions.append((40, f"🧹 {len(report.stale_skills)} skills untouched >30d — prune dead ones"))
    actions.sort(key=lambda x: -x[0])
    return [t for _, t in actions[:4]]


def render_markdown(report: DreamReport) -> str:
    now = datetime.now().isoformat(timespec="seconds")
    lines = [
        f"# Dreaming Report — {now}",
        f"",
        f"> Analysis of last **{report.days} days** of Claude Code usage.",
        f"> Inspired by Jack Roberts Claude OS Dreaming feature ([[Claude-OS-Dashboard-Jack]]).",
        f"",
        f"## Summary",
        f"",
        f"- **Sessions analyzed:** {report.sessions_analyzed}",
        f"- **User prompts:** {report.user_prompts_analyzed}",
        f"- **Corrections detected:** {report.corrections_detected}",
        f"- **API-equiv cost:** ${report.cost.get('total_cost_usd', 0)} "
        f"({report.cost.get('total_tokens', 0):,} tokens, cache-hit {report.cost.get('cache_hit_pct', 0)}%)",
        f"- **Memory health:** {report.memory_health.get('selfreg_grade', '?')} "
        f"({report.memory_health.get('stale_wikis', 0)}/{report.memory_health.get('total_wikis', 0)} wikis stale)",
    ]
    _roi = load_roi()
    if _roi:
        lines.append(
            f"- **Automation ROI ({_roi.get('window_days','?')}d):** {_roi.get('time_saved_hours',0)}h saved "
            f"≈ {_roi.get('currency','USD')} {_roi.get('value_of_time',0)} "
            f"(net {_roi.get('currency','USD')} {_roi.get('net_roi',0)}, {_roi.get('roi_multiple','?')}x)"
        )
    lines += [
        f"",
        f"## ⚡ Top High-Leverage Actions (this week)",
        f"",
    ]
    if report.high_leverage:
        for i, action in enumerate(report.high_leverage, 1):
            lines.append(f"{i}. {action}")
    else:
        lines.append("_(System healthy — no high-leverage actions surfaced)_")
    lines += [
        f"",
        f"---",
        f"",
        f"## 🎯 Dimension Detail",
        f"",
    ]

    # Dimension 2: Cost intelligence
    lines.append("### 💰 Cost Intelligence (API-equivalent)")
    lines.append("")
    c = report.cost
    if c.get("by_family"):
        lines.append("| Model | Tokens | API-equiv $ |")
        lines.append("|-------|--------|-------------|")
        for fam, d in sorted(c["by_family"].items(), key=lambda x: -x[1]["cost_usd"]):
            lines.append(f"| {fam} | {d['tokens']:,} | ${d['cost_usd']} |")
        lines.append(f"| **total** | **{c.get('total_tokens',0):,}** | **${c.get('total_cost_usd',0)}** |")
        lines.append("")
        lines.append(f"Cache-hit rate: **{c.get('cache_hit_pct',0)}%** (higher = cheaper).")
        for f in c.get("flags", []):
            lines.append(f"- ⚠️ {f}")
    else:
        lines.append("_(No usage data in window)_")
    lines.append("")

    # Dimension 4: Memory health
    lines.append("### 🧠 Memory Health")
    lines.append("")
    mh = report.memory_health
    lines.append(f"- Self-regulation grade: **{mh.get('selfreg_grade','?')}** ({mh.get('selfreg_overall','?')}/100)")
    lines.append(f"- Stale wikis: {mh.get('stale_wikis',0)}/{mh.get('total_wikis',0)}")
    if mh.get("graphs_need_enrichment"):
        lines.append(f"- Graphs needing enrichment: {', '.join(mh['graphs_need_enrichment'])}")
    for f in mh.get("flags", []):
        lines.append(f"- ⚠️ {f}")
    lines.append("")

    # Dimension 5: Session hygiene
    lines.append("### 🪟 Session Hygiene")
    lines.append("")
    sh = report.session_hygiene
    if sh.get("sessions"):
        lines.append(f"- Sessions: {sh['sessions']} | avg peak {sh.get('avg_peak_context',0):,} | max peak {sh.get('max_peak_context',0):,} (window ~200k)")
        for f in sh.get("flags", []):
            lines.append(f"- ⚠️ {f}")
        if not sh.get("flags"):
            lines.append("- ✓ Context sizes healthy")
    else:
        lines.append("_(No session data)_")
    lines.append("")

    # Dimension 8: Business context
    lines.append("### 🎯 Business Context (focus areas)")
    lines.append("")
    if report.business_context:
        for proj, count in report.business_context:
            clean = proj.replace("--", "/").replace("-", " ").strip("/")
            lines.append(f"- **{clean}** — {count} prompts")
    else:
        lines.append("_(No activity)_")
    lines.append("")

    lines += [
        f"---",
        f"",
        f"## 📋 Standard Recommendations",
        f"",
    ]

    # Rec 1: Skill candidates
    lines.append("### 1. Skill Candidates (repetitive actions)")
    lines.append("")
    if report.skill_candidates:
        lines.append("Actions performed 5+ times — потенциални skills:")
        lines.append("")
        for action, count in report.skill_candidates[:5]:
            lines.append(f"- **{action}** ({count}× в последните {report.days} дни) — consider `~/.claude/skills/{action}-helper/`")
    else:
        lines.append("_(No clear repetitive actions detected)_")
    lines.append("")

    # Rec 2: Stale skills
    lines.append("### 2. Stale Skills (not modified >30 days)")
    lines.append("")
    if report.stale_skills:
        lines.append("Тези skills не са пипани >30 дни — review дали се ползват:")
        lines.append("")
        for name, days in report.stale_skills[:10]:
            lines.append(f"- `{name}` — {days} дни без modification")
    else:
        lines.append("_(All skills updated within 30 days)_")
    lines.append("")

    # Rec 3: Project activity
    lines.append("### 3. Project Activity Breakdown")
    lines.append("")
    if report.project_activity:
        lines.append("Кои проекти консумират най-много interactions:")
        lines.append("")
        sorted_projects = sorted(report.project_activity.items(), key=lambda x: -x[1])
        for project, count in sorted_projects[:8]:
            # Clean project name (C--Users-... → readable)
            clean = project.replace("--", "/").replace("-", " ").strip("/")
            lines.append(f"- `{project}` — {count} prompts")
    lines.append("")

    # Rec 4: Corrections (Boris pattern signal)
    lines.append("### 4. Correction Signals (Boris pattern)")
    lines.append("")
    if report.corrections_detected > 0:
        lines.append(
            f"**{report.corrections_detected} корекции** засечени в последните {report.days} дни. "
            "Boris правилото: при всяка корекция → update CLAUDE.md."
        )
        lines.append("")
        lines.append("Примерни корекции (review дали трябва ред в проектния CLAUDE.md):")
        lines.append("")
        for example in report.correction_examples[:5]:
            lines.append(f"- `{example.strip()[:120]}`")
    else:
        lines.append(f"_(No corrections detected — Boris pattern satisfied for last {report.days} days)_")
    lines.append("")

    # Top terms (raw data)
    lines.append("---")
    lines.append("")
    lines.append("## Appendix: Top 30 Terms (raw data)")
    lines.append("")
    if report.most_common_terms:
        lines.append("| Term | Count |")
        lines.append("|------|-------|")
        for term, count in report.most_common_terms:
            lines.append(f"| `{term}` | {count} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("Generated: `python ~/.claude/scripts/stage3_dreaming.py`")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Stage 3 weekly self-learning analysis (dreaming)")
    parser.add_argument("--days", type=int, default=7, help="Days of history to analyze (default: 7)")
    parser.add_argument("--stale-days", type=int, default=30, help="Skill stale threshold (default: 30)")
    parser.add_argument("--json", action="store_true", help="JSON output to stdout")
    parser.add_argument("--out", help="Output markdown path (default: ~/.claude/reports/dreaming-{date}.md)")
    args = parser.parse_args()

    print(f"[dreaming] Analyzing last {args.days} days...", file=sys.stderr)

    data = analyze_jsonl_files(args.days)

    # Habit mining — reuse existing JSONL pass data (no double-walk)
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from habit_miner import mine_habits, save_habits
        tool_seqs = data.get("tool_seqs_by_session", {})
        if tool_seqs:
            habits = mine_habits(tool_seqs)
            save_habits(habits)
            print(f"[dreaming] {len(habits)} habit candidates saved", file=sys.stderr)
    except Exception as _he:
        print(f"[dreaming] habit_miner error (non-fatal): {_he}", file=sys.stderr)

    term_counter = data["term_counter"]
    action_counter = data["action_counter"]
    project_activity = data["project_activity"]
    corrections = data["corrections"]
    stale = get_skill_health(stale_days=args.stale_days)
    candidates = make_skill_candidates(term_counter, action_counter)

    report = DreamReport(
        days=args.days,
        sessions_analyzed=data["sessions"],
        user_prompts_analyzed=data["prompts"],
        corrections_detected=len(corrections),
        intent_clusters={},  # reserved for future LLM-based clustering
        skill_candidates=candidates,
        stale_skills=stale,
        most_common_terms=term_counter.most_common(30),
        project_activity=project_activity,
        correction_examples=corrections,
        cost=analyze_cost(data["usage_by_model"]),
        memory_health=analyze_memory_health(),
        session_hygiene=analyze_session_hygiene(data["session_peak"]),
        business_context=sorted(project_activity.items(), key=lambda x: -x[1])[:5],
    )
    report.high_leverage = synthesize_actions(report)

    # Boris semi-automation: write per-project correction→rule candidates.
    # SessionStart surfaces these for the relevant project so the correction→
    # CLAUDE.md-rule loop gets a nudge instead of relying on memory.
    try:
        cbp = data.get("corrections_by_project", {})
        boris = {
            "generated": datetime.now().isoformat(timespec="seconds"),
            "window_days": args.days,
            "projects": {p: {"count": len(ex), "examples": ex[:5]}
                         for p, ex in cbp.items() if len(ex) >= 2},
        }
        (REPORTS_DIR.parent / "logs" / "boris-candidates.json").write_text(
            json.dumps(boris, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    if args.json:
        print(json.dumps({
            "days": report.days,
            "sessions_analyzed": report.sessions_analyzed,
            "user_prompts_analyzed": report.user_prompts_analyzed,
            "corrections_detected": report.corrections_detected,
            "high_leverage": report.high_leverage,
            "cost": report.cost,
            "memory_health": report.memory_health,
            "session_hygiene": report.session_hygiene,
            "skill_candidates": report.skill_candidates,
            "stale_skills": report.stale_skills,
            "project_activity": report.project_activity,
            "business_context": report.business_context,
            "top_terms": report.most_common_terms,
        }, indent=2, ensure_ascii=False))
        return

    md = render_markdown(report)
    out_path = Path(args.out) if args.out else REPORTS_DIR / f"dreaming-{datetime.now():%Y-%m-%d}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")

    # Machine-readable latest snapshot (consumed by the Agentic OS dashboard)
    try:
        (REPORTS_DIR / "dreaming-latest.json").write_text(json.dumps({
            "date": datetime.now().isoformat(timespec="seconds"),
            "days": report.days,
            "high_leverage": report.high_leverage,
            "cost": report.cost,
            "memory_health": report.memory_health,
            "session_hygiene": report.session_hygiene,
            "business_context": report.business_context,
            "corrections_detected": report.corrections_detected,
            "skill_candidates": report.skill_candidates,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    print(f"Wrote: {out_path}", file=sys.stderr)
    print(f"Sessions: {data['sessions']}, Prompts: {data['prompts']}, Corrections: {len(corrections)}", file=sys.stderr)
    print(f"Skill candidates: {len(candidates)}, Stale skills: {len(stale)}", file=sys.stderr)


if __name__ == "__main__":
    main()
