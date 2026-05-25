#!/usr/bin/env python3
"""skills_audit.py - Validate ~/.claude/skills/ против agentskills.io spec.

Checks each SKILL.md за:
- Frontmatter present (--- ... ---)
- Required: name + description
- Name regex: [a-z][a-z0-9-]*[a-z0-9] (lowercase, hyphens, no leading/trailing dash)
- Folder name matches frontmatter name
- Description has 2 parts (WHAT + WHEN heuristic)
- Optional: allowed-tools syntax

Output: markdown report → ~/.claude/reports/skills-audit-{date}.md

Usage:
    python skills_audit.py              # Audit all skills
    python skills_audit.py --skill foo  # Audit single
    python skills_audit.py --json       # JSON output
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SKILLS_DIR = Path.home() / ".claude" / "skills"
REPORTS_DIR = Path.home() / ".claude" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

NAME_REGEX = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")
WHEN_KEYWORDS = ["when", "use this", "use when", "use if", "trigger", "когато", "използвай", "ако"]


@dataclass
class SkillReport:
    name: str
    path: Path
    has_skill_md: bool = False
    has_frontmatter: bool = False
    has_name: bool = False
    has_description: bool = False
    name_valid_regex: bool = False
    name_matches_folder: bool = False
    description_has_when: bool = False
    description_length: int = 0
    allowed_tools: str | None = None
    has_scripts_dir: bool = False
    has_references_dir: bool = False
    has_assets_dir: bool = False
    issues: list[str] = field(default_factory=list)

    @property
    def score(self) -> int:
        """0-100 compliance score."""
        checks = [
            self.has_skill_md,
            self.has_frontmatter,
            self.has_name,
            self.has_description,
            self.name_valid_regex,
            self.name_matches_folder,
            self.description_has_when,
        ]
        if not checks:
            return 0
        return int(100 * sum(checks) / len(checks))


def parse_frontmatter(text: str) -> tuple[dict, bool]:
    """Extract YAML frontmatter. Returns (parsed dict, success)."""
    if not text.startswith("---"):
        return {}, False
    lines = text.split("\n")
    if len(lines) < 3:
        return {}, False
    # Find closing ---
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return {}, False

    fm = {}
    current_key = None
    current_value = []
    for line in lines[1:end]:
        stripped = line.rstrip()
        if not stripped:
            continue
        # Multi-line continuation (indented)
        if line.startswith((" ", "\t")) and current_key:
            current_value.append(stripped.strip())
            continue
        # New key
        if current_key and current_value:
            fm[current_key] = " ".join(current_value).strip()
            current_value = []
        if ":" in stripped:
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            current_key = key
            if val == "|" or val == ">":
                current_value = []
            elif val:
                fm[key] = val
                current_key = None
                current_value = []
            else:
                current_value = []
    if current_key and current_value:
        fm[current_key] = " ".join(current_value).strip()

    return fm, True


def audit_skill(skill_dir: Path) -> SkillReport:
    report = SkillReport(name=skill_dir.name, path=skill_dir)

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        report.issues.append("Missing SKILL.md")
        return report
    report.has_skill_md = True

    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception as exc:
        report.issues.append(f"Cannot read SKILL.md: {exc}")
        return report

    fm, ok = parse_frontmatter(text)
    if not ok:
        report.issues.append("Missing or malformed YAML frontmatter (--- ... ---)")
        return report
    report.has_frontmatter = True

    name = fm.get("name", "").strip()
    if name:
        report.has_name = True
        if NAME_REGEX.match(name):
            report.name_valid_regex = True
        else:
            report.issues.append(
                f"name '{name}' does not match agentskills.io regex "
                f"[a-z][a-z0-9-]*[a-z0-9] (lowercase, hyphens, no leading/trailing dash)"
            )
        if name == skill_dir.name:
            report.name_matches_folder = True
        else:
            report.issues.append(
                f"name '{name}' != folder name '{skill_dir.name}'"
            )
    else:
        report.issues.append("Missing required 'name' field in frontmatter")

    desc = fm.get("description", "").strip()
    if desc:
        report.has_description = True
        report.description_length = len(desc)
        # Heuristic: contains WHEN keyword?
        desc_lower = desc.lower()
        if any(kw in desc_lower for kw in WHEN_KEYWORDS):
            report.description_has_when = True
        else:
            report.issues.append(
                "description missing 'when/use this/когато/използвай' part — "
                "description should be WHAT + WHEN per agentskills.io spec"
            )
        if len(desc) < 30:
            report.issues.append(f"description too short ({len(desc)} chars) — be specific")
    else:
        report.issues.append("Missing required 'description' field in frontmatter")

    if "allowed-tools" in fm:
        report.allowed_tools = fm["allowed-tools"]

    report.has_scripts_dir = (skill_dir / "scripts").is_dir()
    report.has_references_dir = (skill_dir / "references").is_dir()
    report.has_assets_dir = (skill_dir / "assets").is_dir()

    return report


def render_markdown(reports: list[SkillReport]) -> str:
    now = datetime.now().isoformat(timespec="seconds")
    total = len(reports)
    if total == 0:
        return f"# Skills Audit — {now}\n\nNo skills found in {SKILLS_DIR}\n"

    avg_score = sum(r.score for r in reports) / total
    perfect = sum(1 for r in reports if r.score == 100)
    with_issues = sum(1 for r in reports if r.issues)

    lines = [
        f"# Skills Audit — {now}",
        f"",
        f"Auditing **{total}** skills in `{SKILLS_DIR}` against agentskills.io spec.",
        f"",
        f"## Summary",
        f"",
        f"- **Total skills:** {total}",
        f"- **Perfect score (100%):** {perfect}",
        f"- **With issues:** {with_issues}",
        f"- **Average compliance:** {avg_score:.1f}%",
        f"",
        f"## Failing Skills (issues found)",
        f"",
    ]

    failing = sorted([r for r in reports if r.issues], key=lambda r: r.score)
    if not failing:
        lines.append("_(All skills pass)_\n")
    else:
        for r in failing:
            lines.append(f"### `{r.name}` — {r.score}% compliance")
            lines.append("")
            for issue in r.issues:
                lines.append(f"- ⚠️ {issue}")
            lines.append("")

    lines.extend([
        f"## Passing Skills",
        f"",
    ])
    passing = sorted([r for r in reports if not r.issues], key=lambda r: r.name)
    if not passing:
        lines.append("_(None)_\n")
    else:
        lines.append("| Skill | Score | allowed-tools | dirs |")
        lines.append("|-------|-------|---------------|------|")
        for r in passing:
            dirs = []
            if r.has_scripts_dir:
                dirs.append("scripts")
            if r.has_references_dir:
                dirs.append("references")
            if r.has_assets_dir:
                dirs.append("assets")
            dirs_str = ", ".join(dirs) if dirs else "-"
            tools = r.allowed_tools if r.allowed_tools else "-"
            lines.append(f"| `{r.name}` | {r.score}% | `{tools}` | {dirs_str} |")

    lines.extend([
        f"",
        f"---",
        f"",
        f"Generated: `python ~/.claude/scripts/skills_audit.py`",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Audit ~/.claude/skills/ for agentskills.io compliance")
    parser.add_argument("--skill", help="Audit single skill")
    parser.add_argument("--json", action="store_true", help="JSON output to stdout")
    parser.add_argument("--out", help="Write markdown report to this path (default: ~/.claude/reports/skills-audit-{date}.md)")
    args = parser.parse_args()

    if not SKILLS_DIR.exists():
        print(f"ERROR: {SKILLS_DIR} does not exist", file=sys.stderr)
        sys.exit(1)

    if args.skill:
        skill_dir = SKILLS_DIR / args.skill
        if not skill_dir.is_dir():
            print(f"ERROR: skill '{args.skill}' not found", file=sys.stderr)
            sys.exit(1)
        reports = [audit_skill(skill_dir)]
    else:
        reports = [audit_skill(d) for d in sorted(SKILLS_DIR.iterdir()) if d.is_dir()]

    if args.json:
        print(json.dumps([{
            "name": r.name,
            "score": r.score,
            "issues": r.issues,
            "allowed_tools": r.allowed_tools,
        } for r in reports], indent=2, ensure_ascii=False))
        return

    md = render_markdown(reports)
    out_path = Path(args.out) if args.out else REPORTS_DIR / f"skills-audit-{datetime.now():%Y-%m-%d}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"Wrote: {out_path}")
    print(f"Audited {len(reports)} skills, {sum(1 for r in reports if r.issues)} with issues")


if __name__ == "__main__":
    main()
