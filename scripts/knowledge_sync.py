#!/usr/bin/env python3
"""knowledge_sync.py — Keep Pinecone L4 continuously in sync with the source-of-truth
learnings/decisions/patterns/meta. Closes the gap where learnings.md edits never
reached Pinecone unless caught in a session transcript.

Runs bulk_files_import.py (incremental — content-hash manifest, only embeds CHANGED
content) for every active project's learnings + decisions, plus _shared and _meta.

Wired into the weekly dispatcher. Never raises.

Usage:
    python knowledge_sync.py            # incremental
    python knowledge_sync.py --force    # re-embed everything
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

CLAUDE = Path.home() / ".claude"
BULK = CLAUDE / "scripts" / "bulk_files_import.py"
WIKI_MAP = Path(r"{{WIKI_PATH}}\_shared\wiki-map.json")
PYEXE = sys.executable or "python"


def _run(ns: str, globs: list[str], force: bool) -> str:
    cmd = [PYEXE, str(BULK), ns] + globs + (["--force"] if force else [])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace")
        line = (p.stdout or "").strip().splitlines()
        return line[-1] if line else f"[{ns}] no output"
    except Exception as e:
        return f"[{ns}] ERROR {e}"


def main() -> None:
    force = "--force" in sys.argv
    try:
        wm = json.loads(WIKI_MAP.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"knowledge_sync: cannot read wiki-map: {e}")
        sys.exit(0)
    base_wiki = wm.get("base_wiki_path", r"{{WIKI_PATH}}")
    base_code = wm.get("base_code_path", r"{{CODE_PATH}}")
    meta = wm.get("metadata", {})

    results = []
    # Per active project: learnings + decisions → project namespace
    for code, wiki in wm.get("mapping", {}).items():
        if (meta.get(wiki, {}) or {}).get("status", "active") != "active":
            continue
        globs = [
            f"{base_wiki}/{wiki}/**/learnings.md",
            f"{base_wiki}/{wiki}/**/DECISIONS.md",
            f"{base_code}/{code}/.ai/learnings.md",
        ]
        results.append(_run(wiki, globs, force))
    # Shared + meta
    results.append(_run("_shared", [f"{base_wiki}/_shared/*.md"], force))
    results.append(_run("_meta", [f"{base_wiki}/_meta/*.md"], force))

    total_saved = sum(int(r.split("saved=")[1].split()[0]) for r in results if "saved=" in r)
    print(f"knowledge_sync: {len(results)} targets, {total_saved} new/changed chunks embedded")
    for r in results:
        print("  " + r)
    sys.exit(0)


if __name__ == "__main__":
    main()
