#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wikilink_checker.py — Find broken [[wikilinks]] in an Obsidian vault.

Obsidian resolves links by basename, so this mirrors that: a link `[[X]]`
resolves if `X.md` exists anywhere in the vault; `[[img.png]]` resolves if a
file named `img.png` exists. Reports unresolved links per file.

    python wikilink_checker.py "<vault path>"            # human report
    python wikilink_checker.py "<vault path>" --json       # machine JSON
"""
from __future__ import annotations
import re, sys, json
from pathlib import Path

# [[link]] / [[link|alias]] / [[link#heading]] / [[folder/link]] — NOT ![[embed]]? we include embeds too
LINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]+)?(?:\|[^\]]+)?\]\]")
SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules"}
# Only these count as real file extensions — a dot inside a name (e.g. "3.0",
# "Fakturka.bg") is NOT an extension and must still resolve to <name>.md.
KNOWN_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf", ".json",
             ".canvas", ".base", ".mp3", ".mp4", ".mov", ".excalidraw", ".md", ".txt"}


def extract_links(text: str) -> list[str]:
    """Pure: return raw link targets (before # and |) from wikilinks in text.
    Strips a trailing backslash from table-escaped pipes ([[x\\|alias]])."""
    return [m.group(1).strip().rstrip("\\").strip() for m in LINK_RE.finditer(text)]


def resolves(target: str, md_stems: set[str], file_names: set[str]) -> bool:
    """Pure: does a link target resolve against the vault's files?"""
    # strip trailing backslash FIRST (Obsidian table-escaped pipe: [[note\|alias]]),
    # then normalise separators and take the basename.
    base = target.strip().rstrip("\\").replace("\\", "/").split("/")[-1].strip()
    if not base:
        return False
    # Obsidian resolves wikilinks case-INSENSITIVELY.
    base_l = base.lower()
    md_l = {s.lower() for s in md_stems}
    names_l = {f.lower() for f in file_names}
    dot = base.rfind(".")
    ext = base[dot:].lower() if dot != -1 else ""
    if ext in KNOWN_EXT and ext != ".md":   # real asset extension -> match a filename
        return base_l in names_l
    if ext == ".md":                          # explicit .md -> match stem
        return base_l[:-3] in md_l
    return base_l in md_l                       # bare name (dots allowed) -> <name>.md


def scan(vault: Path) -> dict:
    files = [f for f in vault.rglob("*")
             if f.is_file() and not any(p in SKIP_DIRS for p in f.parts)]
    md_stems = {f.stem for f in files if f.suffix == ".md"}
    file_names = {f.name for f in files}
    broken: dict[str, list[str]] = {}
    for f in files:
        if f.suffix != ".md":
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        bad = [t for t in extract_links(text) if not resolves(t, md_stems, file_names)]
        if bad:
            broken[str(f.relative_to(vault))] = sorted(set(bad))
    return broken


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: wikilink_checker.py <vault path> [--json]")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    vault = Path(sys.argv[1])
    broken = scan(vault)
    total = sum(len(v) for v in broken.values())
    if "--json" in sys.argv:
        print(json.dumps({"vault": str(vault), "broken_total": total, "files": broken},
                         ensure_ascii=False, indent=2))
        return
    print(f"Vault: {vault.name} — {total} broken link(s) in {len(broken)} file(s)")
    for f, links in sorted(broken.items()):
        print(f"\n  {f}")
        for l in links:
            print(f"    ✗ [[{l}]]")


if __name__ == "__main__":
    main()
