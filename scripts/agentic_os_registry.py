#!/usr/bin/env python3
"""agentic_os_registry.py — The Agentic OS backbone (Chase AI "architecture" layer).

Auto-discovers everything the system can DO and maps it into domains:
    domain -> { skills, commands, automations, projects }

This is the codification layer: "turn daily workflows into skills, skills into
automations, automations into architecture." Re-running picks up new skills/
automations automatically, so the registry self-maintains (self-improving OS).

Discovers from ground truth:
    skills      ~/.claude/skills/*/            (+ SKILL.md description)
    commands    ~/.claude/commands/*.md
    automations automation_dispatcher.py steps (+ cadence) and scheduled tasks
    projects    {{WIKI_PATH}}/_shared/wiki-map.json (active only)

Outputs:
    {{WIKI_PATH}}/_meta/domain-registry.json      — machine-readable backbone
    {{WIKI_PATH}}/_meta/AGENTIC_OS_REGISTRY.md     — human-readable view

Domain classification is keyword-based (DOMAIN_RULES). Anything unmatched lands
in 'uncategorized' for the human to reclassify — never silently dropped.

Never raises. Safe for the scheduler.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

CLAUDE = Path.home() / ".claude"
SKILLS_DIR = CLAUDE / "skills"
COMMANDS_DIR = CLAUDE / "commands"
DISPATCHER = CLAUDE / "scripts" / "automation_dispatcher.py"
WIKI_MAP = Path(r"{{WIKI_PATH}}\_shared\wiki-map.json")
META = Path(r"{{WIKI_PATH}}\_meta")
REGISTRY_JSON = META / "domain-registry.json"
REGISTRY_MD = META / "AGENTIC_OS_REGISTRY.md"

# Domain definitions + keyword rules (first match wins, order matters)
DOMAINS = {
    "dev_saas": "Software development — SaaS products, scaffolding, review, deploy",
    "memory_knowledge": "4-layer memory, RAG, knowledge graphs, wiki ingestion",
    "ai_video_content": "AI video/image generation, design, presentations",
    "research": "Deep research, reports, strategy, knowledge ingestion",
    "ops_automation": "Meta-ops: token/cost, monitoring, skill lifecycle, orchestration",
    "utility": "Standalone tools — docs, spreadsheets, browser, misc",
}

DOMAIN_RULES = [
    ("memory_knowledge", ["pinecone", "memory", "kb-", "four-layer", "graphify",
                          "dev-wiki", "gitnexus", "recall", "wiki", "research-ingest",
                          "super_graph", "promotion", "promoter", "freshness", "cross_project",
                          "wrap-up"]),
    ("ai_video_content", ["higgsfield", "seedance", "soul-id", "photoshoot", "marketplace",
                          "design", "ui-ux", "pptx", "artifacts", "image", "video"]),
    ("dev_saas", ["fastapi", "react", "deploy", "code-review", "commit", "test",
                  "refactor", "enum", "bulk-fixer", "architecture", "api-doc", "prd",
                  "feature", "docker", "pre-deploy", "scaffold", "changelog",
                  "update-docs", "website", "create-architecture", "fix"]),
    ("research", ["notebooklm", "advise", "strategy", "ultra-think", "learning-report",
                  "research", "kb-ingest", "kb-init"]),
    ("ops_automation", ["monitor", "token", "usage", "caveman", "skill-creator",
                        "skill-feedback", "mcp-builder", "agent-team", "toggle",
                        "workflow-orchestrator", "selfreg", "skills_audit", "auto_graphify",
                        "ai_video_check", "cleanup", "lint", "registry", "todo"]),
    ("utility", ["excel", "pdf", "playwright", "pinokio", "output", "gepeto",
                 "claudbot", "resolve-edit", "monitor-tool", "watch"]),
]


def classify(name: str) -> str:
    low = name.lower()
    for domain, kws in DOMAIN_RULES:
        if any(k in low for k in kws):
            return domain
    return "uncategorized"


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def skill_desc(skill_dir: Path) -> str:
    md = skill_dir / "SKILL.md"
    if not md.exists():
        return ""
    txt = _read(md)
    m = re.search(r'^description:\s*["\']?(.+?)["\']?\s*$', txt, re.MULTILINE)
    if m:
        return m.group(1)[:140]
    for line in txt.splitlines():
        s = line.strip()
        if s and not s.startswith(("---", "#", "name:", "description:")):
            return s[:140]
    return ""


def discover_skills() -> dict:
    out = {}
    if SKILLS_DIR.is_dir():
        for d in sorted(SKILLS_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and d.name != "references":
                out[d.name] = skill_desc(d)
    return out


def discover_commands() -> list:
    if not COMMANDS_DIR.is_dir():
        return []
    return sorted(p.stem for p in COMMANDS_DIR.glob("*.md"))


def discover_automations() -> dict:
    """Parse dispatcher to map each automation script to its cadence(s)."""
    txt = _read(DISPATCHER)
    autos: dict = {}
    # crude block parse: find def daily_tasks / weekly_tasks / dreaming_tasks bodies
    for cadence, fn in [("daily", "daily_tasks"), ("weekly", "weekly_tasks"),
                        ("dreaming", "dreaming_tasks")]:
        m = re.search(rf"def {fn}\(.*?\):(.*?)(?=\ndef |\Z)", txt, re.DOTALL)
        if not m:
            continue
        for s in re.findall(r'SCRIPTS_DIR / "([a-z_]+\.py)"', m.group(1)):
            autos.setdefault(s, {"cadence": [], "domain": classify(s)})
            if cadence not in autos[s]["cadence"]:
                autos[s]["cadence"].append(cadence)
    return autos


def discover_projects() -> dict:
    wmap = json.loads(_read(WIKI_MAP) or "{}")
    meta = wmap.get("metadata", {})
    active = {}
    for code, wiki in wmap.get("mapping", {}).items():
        if (meta.get(wiki, {}) or {}).get("status", "active") == "active":
            active[wiki] = (meta.get(wiki, {}) or {}).get("domain", classify(wiki))
    return active


REPORTS = CLAUDE / "reports"


def automation_candidates(skills: dict, automations: dict, commands: list) -> list:
    """Close the loop with dreaming: repeated manual actions that aren't yet a
    skill/automation/command. Chase: 'repeated task -> skill -> automation'."""
    dreaming = json.loads(_read(REPORTS / "dreaming-latest.json") or "{}")
    cands = dreaming.get("skill_candidates", [])  # [[action, count], ...]
    have = " ".join(list(skills) + list(automations) + list(commands)).lower()
    out = []
    for item in cands:
        try:
            action, count = item[0], item[1]
        except (IndexError, TypeError):
            continue
        # coverage check: stem the action (fixing->fix, migration->migrat) then
        # look for the root in any skill/command/automation name.
        root = re.sub(r"(ing|tion|ation|ment|ge)$", "", action.lower())
        covered = (len(root) >= 3 and root in have) or action.lower() in have
        out.append({"action": action, "count": count, "covered": covered})
    return out


def scheduled_tasks() -> list:
    try:
        out = subprocess.run(["schtasks", "/query", "/fo", "LIST"],
                             capture_output=True, text=True, timeout=30,
                             encoding="utf-8", errors="replace").stdout or ""
        return [l.split(":", 1)[1].strip() for l in out.splitlines()
                if "TaskName" in l and "ClaudeAutomation" in l]
    except Exception:
        return []


def build() -> dict:
    skills = discover_skills()
    commands = discover_commands()
    automations = discover_automations()
    projects = discover_projects()

    domains: dict = {d: {"description": desc, "skills": [], "commands": [],
                         "automations": [], "projects": []}
                     for d, desc in DOMAINS.items()}
    domains["uncategorized"] = {"description": "Needs human reclassification",
                               "skills": [], "commands": [], "automations": [], "projects": []}

    for name in skills:
        domains[classify(name)]["skills"].append(name)
    for name in commands:
        domains[classify(name)]["commands"].append(name)
    for name in automations:
        domains[classify(name)]["automations"].append(name)
    for wiki, dom in projects.items():
        (domains.get(dom) or domains["uncategorized"])["projects"].append(wiki)

    return {
        "meta": {
            "generated": datetime.now().isoformat(timespec="seconds"),
            "generator": "agentic_os_registry.py",
            "note": "Auto-discovered backbone. Edit DOMAIN_RULES to reclassify; re-run to refresh.",
        },
        "stats": {
            "skills": len(skills), "commands": len(commands),
            "automations": len(automations), "active_projects": len(projects),
            "uncategorized": len(domains["uncategorized"]["skills"]
                                 + domains["uncategorized"]["commands"]),
        },
        "domains": domains,
        "automations_detail": automations,
        "skill_descriptions": skills,
        "automation_candidates": automation_candidates(skills, automations, commands),
        "scheduled_tasks": scheduled_tasks(),
    }


def render_md(reg: dict) -> str:
    s = reg["stats"]
    lines = [
        "---",
        "type: agentic-os-registry",
        "tags: [domain::meta, project::all, status::active, agentic-os]",
        f"created: {date.today()}",
        f"last_updated: {date.today()}",
        "---",
        "",
        "# Agentic OS — Domain Registry",
        "",
        "> Auto-generated by `agentic_os_registry.py` (weekly). The **backbone**: every",
        "> capability mapped to a domain. Chase AI step 1 — \"architecture\".",
        "",
        f"**Inventory:** {s['skills']} skills · {s['commands']} commands · "
        f"{s['automations']} automations · {s['active_projects']} active projects"
        + (f" · ⚠ {s['uncategorized']} uncategorized" if s['uncategorized'] else ""),
        "",
    ]
    for dom, data in reg["domains"].items():
        if not any([data["skills"], data["commands"], data["automations"], data["projects"]]):
            continue
        lines.append(f"## {dom}")
        lines.append(f"*{data['description']}*")
        lines.append("")
        if data["projects"]:
            lines.append(f"- **Projects:** {', '.join(data['projects'])}")
        if data["automations"]:
            autos = [f"{a} ({'/'.join(reg['automations_detail'][a]['cadence'])})"
                     for a in data["automations"]]
            lines.append(f"- **Automations:** {', '.join(autos)}")
        if data["commands"]:
            lines.append(f"- **Commands:** {', '.join('/' + c for c in data['commands'])}")
        if data["skills"]:
            lines.append(f"- **Skills ({len(data['skills'])}):** {', '.join(data['skills'])}")
        lines.append("")

    # Automation candidates — the "what to codify next" loop (from dreaming)
    cands = reg.get("automation_candidates", [])
    uncovered = [c for c in cands if not c.get("covered")]
    lines.append("## 🔁 Automation candidates (from dreaming)")
    lines.append("")
    lines.append("> Repeated manual actions detected in your sessions. Chase: "
                 "*repeated task → skill → automation*.")
    lines.append("")
    if cands:
        for c in cands[:8]:
            mark = "✅ covered" if c.get("covered") else "⚠️ **not yet a skill/automation**"
            lines.append(f"- **{c['action']}** ({c['count']}×) — {mark}")
        if uncovered:
            lines.append("")
            lines.append(f"→ {len(uncovered)} uncovered. Consider `skill-creator` for the top ones.")
    else:
        lines.append("_(Run dreaming to populate — `automation_dispatcher.py dreaming`)_")
    lines.append("")

    lines.append("---")
    lines.append(f"*Scheduled: {', '.join(reg['scheduled_tasks']) or 'none detected'}*")
    lines.append(f"*Generated {reg['meta']['generated']}*")
    return "\n".join(lines) + "\n"


def main() -> None:
    try:
        reg = build()
        META.mkdir(parents=True, exist_ok=True)
        REGISTRY_JSON.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
        REGISTRY_MD.write_text(render_md(reg), encoding="utf-8")
        s = reg["stats"]
        print(f"agentic_os_registry: {s['skills']} skills, {s['commands']} commands, "
              f"{s['automations']} automations, {s['active_projects']} projects "
              f"({s['uncategorized']} uncategorized)")
    except Exception as e:
        print(f"agentic_os_registry error (suppressed): {e}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
