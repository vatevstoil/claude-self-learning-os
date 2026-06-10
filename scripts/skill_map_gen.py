#!/usr/bin/env python3
"""skill_map_gen.py — Generate ~/.claude/SKILL_MAP.md: a compact "which skill,
when" index, inspired by Nick Milo's AIOS Skill Map.

The map is GENERATED from each skill's own frontmatter `description` (which
already states when to use it) — so it never drifts from reality. Scans:
  • ~/.claude/skills/*/SKILL.md   (name + description)
  • ~/.claude/commands/*.md       (slash commands; name = filename)

Pure-local, no LLM/embedding. Safe on the scheduler. Never non-zero exit.

Usage:
    python skill_map_gen.py            # write SKILL_MAP.md
    python skill_map_gen.py --print
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

HOME = Path.home()
SKILLS_DIR = HOME / ".claude" / "skills"
COMMANDS_DIR = HOME / ".claude" / "commands"
OUT = HOME / ".claude" / "SKILL_MAP.md"


def parse_frontmatter(text: str) -> dict:
    """Extract a flat dict from the leading ``---`` YAML block. Handles quoted,
    multi-line-folded values well enough for name/description. Pure function."""
    m = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
    if not m:
        return {}
    block = m.group(1)
    out: dict[str, str] = {}
    key = None
    for line in block.splitlines():
        km = re.match(r"^([A-Za-z_][\w-]*):\s?(.*)$", line)
        if km:
            key = km.group(1).strip()
            out[key] = km.group(2).strip()
        elif key and line.strip():  # continuation of a folded value
            out[key] += " " + line.strip()
    # strip wrapping quotes
    for k, v in out.items():
        v = v.strip()
        if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
            v = v[1:-1]
        out[k] = v.replace('\\"', '"').replace("\\\\", "\\").strip()
    return out


def condense(desc: str, limit: int = 200) -> str:
    """Trim a description to a scannable one-liner at a word boundary.

    Keeps the trigger phrase: if the text has a 'Use when'/'Triggers'/'Use this'
    clause, anchors there. Always tries to keep a trailing 'NOT for' note (the
    negative trigger is gold for routing). Pure function."""
    desc = re.sub(r"\s+", " ", (desc or "").strip())
    if not desc:
        return ""
    not_for = ""
    nm = re.search(r"(NOT for|Do not use|NOT:|Skip:)\s*(.+)$", desc, flags=re.IGNORECASE)
    if nm:
        not_for = nm.group(2).strip()
        desc = desc[:nm.start()].strip()
    head = desc
    if len(head) > limit:
        head = head[:limit].rsplit(" ", 1)[0] + "…"
    if not_for:
        nf = not_for if len(not_for) <= 80 else not_for[:80].rsplit(" ", 1)[0] + "…"
        head += f"  ⛔ {nf}"
    return head


def collect_skills() -> list[tuple[str, str]]:
    rows = []
    if SKILLS_DIR.is_dir():
        for d in sorted(SKILLS_DIR.iterdir(), key=lambda p: p.name.lower()):
            sk = d / "SKILL.md"
            if not sk.is_file():
                continue
            fm = parse_frontmatter(sk.read_text(encoding="utf-8", errors="replace"))
            name = fm.get("name") or d.name
            rows.append((name, condense(fm.get("description", ""))))
    return rows


def collect_commands() -> list[tuple[str, str]]:
    rows = []
    if COMMANDS_DIR.is_dir():
        for f in sorted(COMMANDS_DIR.glob("*.md"), key=lambda p: p.name.lower()):
            fm = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
            rows.append(("/" + f.stem, condense(fm.get("description", ""))))
    return rows


def build_map(now: datetime, skills: list, commands: list) -> str:
    lines = [
        "# SKILL MAP — кой skill кога",
        f"> Генериран {now.strftime('%Y-%m-%d %H:%M')} · {len(skills)} skills · "
        f"{len(commands)} commands · regen: `skill_map_gen.py` (НЕ редактирай ръчно)",
        "",
        "Канонична карта «кога да ползвам кое». Източник = description на всеки "
        "skill/command, затова не дрифтва. Символ ⛔ = кога да НЕ се ползва.",
        "",
        f"## Skills ({len(skills)})",
    ]
    for name, desc in skills:
        lines.append(f"- **{name}** — {desc}" if desc else f"- **{name}**")
    lines += ["", f"## Slash commands ({len(commands)})"]
    for name, desc in commands:
        lines.append(f"- **{name}** — {desc}" if desc else f"- **{name}**")
    lines += ["", f"<!-- skill_map_gen.py · {now.isoformat(timespec='seconds')} -->"]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", dest="do_print", action="store_true")
    args = ap.parse_args()
    now = datetime.now(timezone.utc).astimezone()
    skills = collect_skills()
    commands = collect_commands()
    md = build_map(now, skills, commands)
    try:
        OUT.write_text(md, encoding="utf-8")
        print(f"skill_map_gen: wrote {OUT} ({len(skills)} skills, {len(commands)} commands)")
    except Exception as exc:
        print(f"skill_map_gen: cannot write {OUT}: {exc}")
    if args.do_print:
        print("\n" + md)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"skill_map_gen: {e}")
        sys.exit(0)
