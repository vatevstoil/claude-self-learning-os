#!/usr/bin/env python3
"""session_handoff.py — Prepare a clean session handoff with minimal user work.

Run at end-of-session (via the /handoff command). Deterministic, judgment-free
parts only — the /handoff command layers the Pinecone wrap-up on top.

Does:
  1. Detect the active project + type (code / research-wiki / meta) from cwd.
  2. Flag whether the knowledge graph is STALE (source newer than graph_updated).
  3. Pull the current task from wiki/SPRINT.md ("В работа").
  4. Write a ready-to-paste NEXT-SESSION PROMPT to logs/next-session-prompt.md
     and print it. The prompt is short + self-restoring because context is
     already persisted (graph + Pinecone + wiki).

Usage:
    python session_handoff.py [--cwd <path>]

Never raises.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

HOME = Path.home()
LOGS = HOME / ".claude" / "logs"
WIKI_MAP = Path(r"{{WIKI_PATH}}\_shared\wiki-map.json")
OUT = LOGS / "next-session-prompt.md"
SCRIPTS = HOME / ".claude" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _sanitize_ns(name: str) -> str:
    try:
        from ns_util import sanitize_ns
        return sanitize_ns(name)
    except Exception:
        return name


def detect(cwd: str) -> dict:
    """Return {project, namespace, kind, wiki_dir, graph_dir, code_dir}."""
    cwd = cwd.replace("\\", "/").rstrip("/")
    wm = _load(WIKI_MAP)
    mapping = wm.get("mapping", {})
    meta = wm.get("metadata", {})
    info = {"project": None, "namespace": None, "kind": "meta",
            "wiki_dir": None, "graph_dir": None, "code_dir": None}

    for base, kind in (("{{CODE_PATH}}/", "code"),
                       ("{{RESEARCH_PATH}}/", "research"),
                       ("{{WIKI_PATH}}/", "wiki")):
        if cwd.startswith(base):
            proj = cwd[len(base):].split("/", 1)[0]
            if not proj:
                continue
            info["project"] = proj
            ns = mapping.get(proj, proj)
            info["namespace"] = _sanitize_ns(ns)
            info["kind"] = kind
            if kind == "code":
                info["code_dir"] = f"{base}{proj}"
                info["graph_dir"] = f"{{WIKI_PATH}}/{mapping.get(proj, proj)}/graph"
            elif kind == "research":
                info["wiki_dir"] = f"{base}{proj}/wiki"
            else:
                info["wiki_dir"] = f"{base}{proj}/wiki"
                info["graph_dir"] = f"{base}{proj}/graph"
            break
    return info


def _source_roots(info: dict) -> list[Path]:
    """Directories whose *.py mtimes define 'is the graph stale?'.

    The Claude self-learning OS is special: its source of truth lives in
    ~/.claude/{scripts,skills,commands}, NOT under the project dir
    ({{CODE_PATH}}/Claude has 0 .py files). Without this, staleness for the
    meta-project is permanently False and /handoff never refreshes the graph.
    """
    code_dir = info.get("code_dir")
    if info.get("project") == "Claude":
        cl = HOME / ".claude"
        return [cl / "scripts", cl / "skills", cl / "commands"]
    if code_dir:
        return [Path(code_dir)]
    return [HOME / ".claude" / "scripts"]


def graph_stale(info: dict) -> tuple[bool, str]:
    """True if the knowledge graph is older than the newest source file."""
    gdir = info.get("graph_dir")
    if not gdir:
        return False, "no graph for this project type"
    kg = Path(gdir) / "knowledge_graph.json"
    if not kg.exists():
        return True, "no graph yet — run /graphify"
    try:
        graph_mtime = kg.stat().st_mtime
        newest = 0.0
        for src in _source_roots(info):
            if src.exists():
                for f in src.rglob("*.py"):
                    if "__pycache__" in str(f):
                        continue
                    newest = max(newest, f.stat().st_mtime)
        if newest > graph_mtime:
            days = (newest - graph_mtime) / 86400
            return True, f"source changed {days:.1f}d after graph — refresh with /graphify"
        return False, "graph current"
    except Exception as e:
        return False, f"staleness check failed: {e}"


def current_task(info: dict) -> str:
    """Extract the active task from wiki/SPRINT.md 'В работа' table."""
    wdir = info.get("wiki_dir")
    if not wdir:
        return ""
    sprint = Path(wdir) / "SPRINT.md"
    if not sprint.exists():
        return ""
    try:
        txt = sprint.read_text(encoding="utf-8")
        m = re.search(r"##\s*В работа(.*?)(?=\n##|\Z)", txt, re.DOTALL)
        if not m:
            return ""
        rows = [r.strip() for r in m.group(1).splitlines()
                if r.strip().startswith("|") and "Задача" not in r and "---" not in r and "—" not in r.split("|")[1]]
        tasks = []
        for r in rows[:3]:
            cells = [c.strip() for c in r.strip("|").split("|")]
            if cells and cells[0] and cells[0] != "—":
                tasks.append(cells[0])
        return "; ".join(tasks)
    except Exception:
        return ""


def build_prompt(info: dict, stale: bool, stale_reason: str, task: str) -> str:
    proj = info.get("project") or "(unknown)"
    ns = info.get("namespace") or "_claude_meta"
    kind = info.get("kind")
    reads = []
    if info.get("graph_dir") and Path(info["graph_dir"], "knowledge_graph.json").exists():
        reads.append(f"1. Граф (преди grep!): `{info['graph_dir']}/knowledge_graph.json`")
    if info.get("wiki_dir"):
        reads.append(f"{len(reads)+1}. `{info['wiki_dir']}/SPRINT.md` + последни 5 от `log.md`")
    reads.append(f"{len(reads)+1}. Recall: `python ~/.claude/scripts/pinecone.py query {ns} \"<тема>\" --topk 5`")
    reads_block = "\n".join(reads)

    role_hint = {"code": "engineer (влез в най-подходящата експертна роля)",
                 "research": "knowledge curator / domain expert за темата",
                 "wiki": "domain expert"}.get(kind, "expert")

    task_line = task or "(виж SPRINT.md 'В работа')"
    return f"""# Next-session prompt — {proj}
<!-- generated {date.today().isoformat()} by /handoff. Paste into a fresh session after /clear. -->

Продължавам **{proj}**. Роля: {role_hint}.

Прочети първо (контекстът е персистиран — това го възстановява):
{reads_block}

**Следваща задача:** {task_line}

Спазвай: expert-role-first + обяви я · Verify→RE-verify · брутална честност.
В края: `/handoff` отново.
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", default=os.getcwd())
    args = ap.parse_args()

    info = detect(args.cwd)
    stale, reason = graph_stale(info)
    task = current_task(info)
    prompt = build_prompt(info, stale, reason, task)

    LOGS.mkdir(parents=True, exist_ok=True)
    try:
        OUT.write_text(prompt, encoding="utf-8")
    except Exception:
        pass

    # Machine-readable summary for the /handoff command to act on
    print("=== HANDOFF SUMMARY ===")
    print(f"project: {info.get('project')}  namespace: {info.get('namespace')}  kind: {info.get('kind')}")
    print(f"graph_stale: {stale}  ({reason})")
    print(f"research_wiki: {info.get('kind') == 'research'}  (→ run knowledge_sync to embed latest)")
    print(f"current_task: {task or '(none in SPRINT)'}")
    print(f"prompt_written: {OUT}")
    print()
    print("=== NEXT-SESSION PROMPT ===")
    print(prompt)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"session_handoff: {e}")
        sys.exit(0)
