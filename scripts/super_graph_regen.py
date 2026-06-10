#!/usr/bin/env python3
"""super_graph_regen.py — Regenerate the mechanical inventory of super_graph.json
from ground truth, while PRESERVING hand-curated semantic sections.

Refreshes (from disk):
    - meta (counts, last_updated, source_graphs)
    - projects        (graph_exists, status, wiki_file_count — merged with curated fields)
    - research_wikis  (inventory of {{RESEARCH_PATH}} — keeps existing descriptions)
    - shared_patterns (inventory of _shared/*.md with frontmatter applies_to)

Preserves verbatim (never auto-edited — they are hand-curated knowledge):
    - bridges
    - cross_project_edges
    - concept_index
    - research_summaries

Always backs up the previous file before writing. Idempotent.

Usage:
    python super_graph_regen.py            # regenerate in place (with backup)
    python super_graph_regen.py --dry-run  # print what would change, write nothing
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path

OBSIDIAN = Path(r"{{WIKI_PATH}}")
RESURCH = Path(r"{{RESEARCH_PATH}}")
META_DIR = OBSIDIAN / "_meta"
SHARED_DIR = OBSIDIAN / "_shared"
SUPER_GRAPH = META_DIR / "super_graph.json"
WIKI_MAP = SHARED_DIR / "wiki-map.json"

PRESERVE_KEYS = ("bridges", "cross_project_edges", "concept_index", "research_summaries")

_FM_FIELD_RE = {
    "type": re.compile(r"^type\s*:\s*(.+)$", re.MULTILINE | re.IGNORECASE),
    "applies_to": re.compile(r"^applies_to\s*:\s*(.+)$", re.MULTILINE | re.IGNORECASE),
    "tags": re.compile(r"^tags\s*:\s*(.+)$", re.MULTILINE | re.IGNORECASE),
}


def _frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    fm = text[3:end]
    out: dict = {}
    for key, rx in _FM_FIELD_RE.items():
        m = rx.search(fm)
        if m:
            val = m.group(1).strip()
            if val.startswith("["):
                val = [x.strip().strip('"').strip("'") for x in val.strip("[]").split(",") if x.strip()]
            out[key] = val
    return out


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def _count_md(wiki_root: Path) -> int:
    if not wiki_root.is_dir():
        return 0
    return sum(1 for _ in wiki_root.rglob("*.md"))


def scan_projects(existing: dict, mapping: dict, metadata: dict) -> dict:
    """Merge curated project entries with fresh mechanical fields."""
    projects: dict = {}
    for _code, wiki_name in mapping.items():
        meta = metadata.get(wiki_name, {}) or {}
        status = meta.get("status", "active")
        wiki_root = OBSIDIAN / wiki_name
        graph_file = wiki_root / "graph" / "knowledge_graph.json"
        curated = existing.get(wiki_name, {})  # keep hand-written fields

        entry = dict(curated)  # start from curated, overlay mechanical
        entry["status"] = status
        entry["stack"] = meta.get("stack", curated.get("stack", "?"))
        entry["graph_exists"] = graph_file.exists()
        entry["wiki_exists"] = wiki_root.is_dir()
        entry["wiki_file_count"] = _count_md(wiki_root)
        projects[wiki_name] = entry
    return projects


def scan_research_wikis(existing: dict) -> dict:
    """Inventory {{RESEARCH_PATH}}, keep curated descriptions."""
    wikis: dict = dict(existing)  # preserve existing entries/descriptions
    if RESURCH.is_dir():
        for d in sorted(RESURCH.iterdir()):
            if not d.is_dir():
                continue
            name = d.name
            entry = dict(wikis.get(name, {}))
            entry["path"] = str(d)
            entry["file_count"] = _count_md(d)
            wikis[name] = entry
    return wikis


def scan_shared_patterns() -> list:
    """Inventory _shared/*.md pattern files from frontmatter."""
    patterns: list = []
    if not SHARED_DIR.is_dir():
        return patterns
    for f in sorted(SHARED_DIR.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _frontmatter(text)
        ftype = (fm.get("type") or "pattern")
        patterns.append({
            "id": f.stem,
            "file": f"_shared/{f.name}",
            "type": ftype,
            "applies_to": fm.get("applies_to", []),
            "tags": fm.get("tags", []),
            "has_frontmatter": bool(fm),
        })
    return patterns


def regenerate(dry_run: bool = False) -> dict:
    existing = _load_json(SUPER_GRAPH) if SUPER_GRAPH.exists() else {}
    wmap = _load_json(WIKI_MAP)
    mapping = wmap.get("mapping", {})
    metadata = wmap.get("metadata", {})

    projects = scan_projects(existing.get("projects", {}), mapping, metadata)
    research_wikis = scan_research_wikis(existing.get("research_wikis", {}))
    shared_patterns = scan_shared_patterns()

    source_graphs = sorted(n for n, e in projects.items() if e.get("graph_exists"))

    new_graph: dict = {}
    # meta first
    old_meta = existing.get("meta", {})
    new_graph["meta"] = {
        **old_meta,
        "version": old_meta.get("version", "1.1"),
        "last_updated": date.today().isoformat(),
        "regenerated_by": "super_graph_regen.py",
        "regenerated_at": datetime.now().isoformat(timespec="seconds"),
        "source_graphs": source_graphs,
        "project_count": len(projects),
        "active_project_count": sum(1 for e in projects.values() if e.get("status") == "active"),
        "dormant_project_count": sum(1 for e in projects.values() if e.get("status") == "dormant"),
        "missing_wiki_count": sum(1 for e in projects.values() if e.get("status") == "missing-wiki"),
        "shared_pattern_count": len(shared_patterns),
        "research_wiki_count": len(research_wikis),
    }
    new_graph["projects"] = projects
    new_graph["research_wikis"] = research_wikis
    new_graph["shared_patterns"] = shared_patterns
    # Preserve curated sections verbatim
    for key in PRESERVE_KEYS:
        if key in existing:
            new_graph[key] = existing[key]

    if dry_run:
        print("DRY RUN — no file written")
        print(f"  projects:        {len(projects)} ({new_graph['meta']['active_project_count']} active, "
              f"{new_graph['meta']['dormant_project_count']} dormant, "
              f"{new_graph['meta']['missing_wiki_count']} missing-wiki)")
        print(f"  source_graphs:   {len(source_graphs)} -> {source_graphs}")
        print(f"  research_wikis:  {len(research_wikis)}")
        print(f"  shared_patterns: {len(shared_patterns)}")
        for key in PRESERVE_KEYS:
            n = len(existing.get(key, [])) if isinstance(existing.get(key), (list, dict)) else 0
            print(f"  preserved {key}: {n}")
        return new_graph

    # Backup then write
    if SUPER_GRAPH.exists():
        backup = SUPER_GRAPH.with_suffix(f".json.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(SUPER_GRAPH, backup)
    SUPER_GRAPH.write_text(
        json.dumps(new_graph, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"super_graph.json regenerated: {len(projects)} projects, "
          f"{len(shared_patterns)} patterns, {len(research_wikis)} research wikis. "
          f"Preserved: {', '.join(PRESERVE_KEYS)}.")
    return new_graph


def main() -> None:
    p = argparse.ArgumentParser(description="Regenerate super_graph.json inventory from ground truth.")
    p.add_argument("--dry-run", action="store_true", help="Show changes, write nothing")
    args = p.parse_args()
    if not WIKI_MAP.exists():
        print(f"ERROR: wiki-map.json not found at {WIKI_MAP}")
        raise SystemExit(2)
    regenerate(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
