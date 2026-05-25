#!/usr/bin/env python3
"""configure.py — Fill the {{PLACEHOLDERS}} in scripts/ and docs/ with your paths,
and write your Pinecone secrets to .env (git-ignored). Run this once after cloning.

Usage:
    python configure.py            # interactive
    python configure.py --defaults # use sensible defaults (vault = ~/Obsidian etc.)
"""
from __future__ import annotations
import os, re, sys
from pathlib import Path

ROOT = Path(__file__).parent
HOME = str(Path.home())

PLACEHOLDERS = {
    "{{HOME}}": ("Home directory", HOME),
    "{{WIKI_PATH}}": ("Obsidian vault for project wikis", str(Path.home() / "Obsidian")),
    "{{RESEARCH_PATH}}": ("Vault for research/knowledge wikis", str(Path.home() / "Obsidian" / "Research")),
    "{{CODE_PATH}}": ("Where your code projects live", str(Path.home() / "Projects")),
    "{{USER_EMAIL}}": ("Your email (used in some doc templates)", ""),
}
SECRETS = {
    "PINECONE_API_KEY": "Your Pinecone API key (console.pinecone.io)",
    "PINECONE_INDEX_HOST": "Your Pinecone index host (e.g. memory-xxxx.svc.region.pinecone.io)",
}


def ask(prompt: str, default: str) -> str:
    if "--defaults" in sys.argv:
        return default
    val = input(f"{prompt}\n  [{default or 'required'}]: ").strip()
    return val or default


def main() -> None:
    print("=== Claude Self-Learning OS — configuration ===\n")
    values = {}
    for ph, (desc, default) in PLACEHOLDERS.items():
        values[ph] = ask(desc, default)

    # Substitute placeholders across scripts/ and docs/
    targets = list((ROOT / "scripts").rglob("*.py")) + list((ROOT / "docs").rglob("*.md")) \
        + list((ROOT / "skills").rglob("*.md"))
    n = 0
    for f in targets:
        txt = f.read_text(encoding="utf-8", errors="replace")
        new = txt
        for ph, val in values.items():
            new = new.replace(ph, val)
        # remaining secret placeholders → read from env at runtime
        new = new.replace("{{PINECONE_API_KEY}}", "")
        new = new.replace("{{PINECONE_INDEX_HOST}}", "")
        if new != txt:
            f.write_text(new, encoding="utf-8")
            n += 1
    print(f"\nSubstituted placeholders in {n} files.")

    # Secrets → .env (never committed)
    print("\n--- Pinecone secrets (written to .env, git-ignored) ---")
    env_lines = []
    for key, desc in SECRETS.items():
        env_lines.append(f"{key}={ask(desc, '')}")
    (ROOT / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    print(f"\nWrote .env. Next: run install.ps1 (Windows) or install.sh (macOS/Linux).")


if __name__ == "__main__":
    main()
