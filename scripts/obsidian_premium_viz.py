#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""obsidian_premium_viz.py — Install the premium visual style into ONE Obsidian vault.

Drops the canonical premium-styles.css snippet, enables it in appearance.json,
and writes a semantic graph color palette derived from the vault's folders
(same folder meaning => same color in EVERY vault). Idempotent; backs up files.

Used standalone (re-style a vault) and by the scaffolders (kb-init / dev-wiki /
four-layer-init) so every NEW wiki is premium from the first open.

    python obsidian_premium_viz.py "<vault path>" [--dry] [--keep-orphans]
"""
from __future__ import annotations
import json, shutil, sys, argparse
from pathlib import Path
from datetime import datetime

TEMPLATE_CSS = Path.home() / ".claude" / "templates" / "obsidian" / "premium-styles.css"
# Fallback (first install before template existed)
FALLBACK_CSS = Path(r"{{RESEARCH_PATH}}\Claude Code Resurch\.obsidian\snippets\premium-styles.css")

# ── Palette ──────────────────────────────────────────────────────────────────
# Stable semantic colors: one folder-meaning => one color across ALL vaults.
SEMANTIC = {
    "concepts": 0x9B7DE0, "esoteric": 0x9B7DE0,
    "summaries": 0x2FB6A8, "knowledge": 0x2FB6A8, "abstracts": 0x2FB6A8,
    "_system": 0xE8765A,
    "sources": 0x4F9BE0, "formulas": 0x4F9BE0, "library": 0x67B7C7,
    "entities": 0x5FBF6B, "output": 0x5FBF6B, "people": 0x5FBF6B,
    "investments": 0xE0B341, "portfolio": 0xE0B341, "financials": 0xE0B341,
    "quotes": 0xC9A86C, "aphorisms": 0xC9A86C,
    "projects": 0xE08A3F, "workflows": 0xE08A3F, "voice": 0xE08A3F,
    "specs": 0xE07DBF, "synthesis": 0xE07DBF,
    "plans": 0xB6D44F, "themes": 0xB6D44F,
    "decisions": 0xD85A7A, "roles": 0xD85A7A, "letters": 0xC9A8E0,
    "templates": 0x8A8276, "schemas": 0x8A8276,
    "audits": 0xC77F5A, "connections": 0x67B7C7, "discovery": 0xD9D055,
    "raw": 0x5A6472, "_raw": 0x5A6472, "archive": 0x5A6472, "docs": 0x5A6472,
    "scratch": 0x5A6472, "en": 0x5A6472, "images": 0x5A6472, "assets": 0x5A6472,
}
CYCLE = [0xE0B341, 0x2FB6A8, 0xE8765A, 0x4F9BE0, 0x5FBF6B, 0x9B7DE0, 0xE07DBF,
         0xB6D44F, 0xE08A3F, 0x67B7C7, 0xD85A7A, 0xC9A8E0, 0x8A8276, 0xD9D055,
         0x7EC8E0, 0xE39A6E, 0xCBD45F, 0x6FD0B0]
HUB_RGB = 0xFFF0C2
HUB_QUERY = "file:index OR file:hot OR file:dashboard OR file:home OR file:MOC OR file:TLDR"
EXCLUDE = {".obsidian", ".trash", ".git", "attachments", "Excalidraw", "graph", ".smart-env"}
ACCENT = "#c9a86c"

DEFAULT_GRAPH = {
    "collapse-filter": True, "search": "", "showTags": False, "showAttachments": False,
    "hideUnresolved": False, "showOrphans": False, "collapse-color-groups": True,
    "colorGroups": [], "collapse-display": False, "showArrow": True,
    "textFadeMultiplier": 0, "nodeSizeMultiplier": 1.6, "lineSizeMultiplier": 0.75,
    "collapse-forces": False, "centerStrength": 0.52, "repelStrength": 16,
    "linkStrength": 1, "linkDistance": 220, "scale": 0.45, "close": False,
}


def assign_colors(names: list[str]) -> dict[str, int]:
    """Pure: map folder names -> rgb int. Semantic first, then collision-free cycle."""
    assigned, used = {}, set()
    for n in names:
        c = SEMANTIC.get(n.lower())
        if c is not None:
            assigned[n] = c
            used.add(c)
    ci = 0
    for n in names:
        if n in assigned:
            continue
        tries = 0
        while CYCLE[ci % len(CYCLE)] in used and tries < len(CYCLE):
            ci += 1; tries += 1
        c = CYCLE[ci % len(CYCLE)]; ci += 1
        assigned[n] = c; used.add(c)
    return assigned


def _q(name: str, prefix: str = "") -> str:
    full = f"{prefix}{name}"
    return f'path:"{full}"' if " " in full else f"path:{full}"


def folder_targets(vault: Path) -> list[tuple[str, str]]:
    """Return [(query, name)] for the vault's content folders (wiki/* or top-level)."""
    wiki = vault / "wiki"
    if wiki.is_dir():
        subs = [d for d in wiki.iterdir() if d.is_dir() and d.name not in EXCLUDE]
        return [(_q(d.name, "wiki/"), d.name) for d in sorted(subs, key=lambda p: p.name)]
    subs = [d for d in vault.iterdir()
            if d.is_dir() and d.name not in EXCLUDE
            and not d.name.startswith(".") and d.name not in ("_meta", "_shared")]
    return [(_q(d.name), d.name) for d in sorted(subs, key=lambda p: p.name)]


def build_groups(vault: Path) -> list[dict]:
    tgs = folder_targets(vault)
    colors = assign_colors([n for _q, n in tgs])
    groups = [{"query": qy, "color": {"a": 1, "rgb": colors[n]}} for qy, n in tgs]
    groups.append({"query": HUB_QUERY, "color": {"a": 1, "rgb": HUB_RGB}})
    return groups


def _backup(f: Path, backup_dir: Path | None):
    if backup_dir and f.exists():
        dest = backup_dir / f.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)


def install(vault: Path, hide_orphans: bool = True, dry: bool = False,
            backup_dir: Path | None = None) -> dict:
    """Install premium viz into one vault. Returns a summary dict."""
    vault = Path(vault)
    obs = vault / ".obsidian"
    src_css = TEMPLATE_CSS if TEMPLATE_CSS.exists() else FALLBACK_CSS
    result = {"vault": vault.name, "css": False, "appearance": False, "graph": 0}

    # 1) CSS snippet
    css_dst = obs / "snippets" / "premium-styles.css"
    if not dry:
        _backup(css_dst, backup_dir)
        css_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_css, css_dst)
    result["css"] = True

    # 2) appearance.json — enable snippet + accent (+ Minimal theme if installed)
    app_p = obs / "appearance.json"
    app = {}
    if app_p.exists():
        try:
            app = json.loads(app_p.read_text(encoding="utf-8"))
        except Exception:
            app = {}
    snips = app.get("enabledCssSnippets", [])
    if "premium-styles" not in snips:
        snips.append("premium-styles")
    app["enabledCssSnippets"] = snips
    app.setdefault("accentColor", ACCENT)
    app.setdefault("theme", "obsidian")  # dark base — where the premium look shines
    if (obs / "themes" / "Minimal").is_dir():
        app["cssTheme"] = "Minimal"
    if not dry:
        _backup(app_p, backup_dir)
        obs.mkdir(parents=True, exist_ok=True)
        app_p.write_text(json.dumps(app, indent=2, ensure_ascii=False), encoding="utf-8")
    result["appearance"] = True

    # 3) graph.json — semantic palette, preserve physics
    gp = obs / "graph.json"
    data = dict(DEFAULT_GRAPH)
    if gp.exists():
        try:
            data = json.loads(gp.read_text(encoding="utf-8"))
        except Exception:
            data = dict(DEFAULT_GRAPH)
    groups = build_groups(vault)
    data["colorGroups"] = groups
    data["showOrphans"] = not hide_orphans
    if not dry:
        _backup(gp, backup_dir)
        gp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    result["graph"] = len(groups)
    return result


def main():
    ap = argparse.ArgumentParser(description="Install premium Obsidian visual into a vault")
    ap.add_argument("vault", help="Path to the vault root")
    ap.add_argument("--dry", action="store_true", help="Preview only")
    ap.add_argument("--keep-orphans", action="store_true", help="Do not hide orphan nodes")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    bdir = None
    if not args.dry:
        bdir = Path.home() / ".claude" / "backups" / f"viz-{datetime.now():%Y%m%d-%H%M%S}" / Path(args.vault).name
    r = install(Path(args.vault), hide_orphans=not args.keep_orphans, dry=args.dry, backup_dir=bdir)
    mode = "DRY" if args.dry else "OK"
    print(f"[{mode}] {r['vault']}: CSS={r['css']} appearance={r['appearance']} graph_groups={r['graph']}")
    if bdir:
        print(f"      backup -> {bdir}")


if __name__ == "__main__":
    main()
