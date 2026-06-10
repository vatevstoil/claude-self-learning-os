#!/usr/bin/env python3
"""auto_graphify.py — Keep active-project knowledge graphs fresh, safely.

Graphify's full quality (critical_rules, semantic clusters) is an LLM task.
This script does the SAFE, deterministic part automatically:

  • MISSING graph (active project) → build a STRUCTURAL graph from the code
    (stack + entry point + router/dir clusters). Pure gain: something vs nothing.
    critical_rules left empty with an enrichment note — never fabricated.

  • STALE existing graph → compare graph date vs newest source file. If code is
    newer, QUEUE it for LLM refresh. NEVER overwrite a hand-crafted graph
    (that would destroy quality — "don't break").

  • FRESH → skip.

Outputs:
    knowledge_graph.json for missing-graph projects (with backup if any existed)
    ~/.claude/logs/graphify-queue.json — projects needing LLM refresh/enrichment

Never raises. Safe for the scheduler.

Usage:
    python auto_graphify.py            # build missing, queue stale
    python auto_graphify.py --dry-run  # report only
    python auto_graphify.py --max 3    # cap builds per run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

WIKI_MAP = Path(r"{{WIKI_PATH}}\_shared\wiki-map.json")
LOGS = Path.home() / ".claude" / "logs"
QUEUE_OUT = LOGS / "graphify-queue.json"
LOG_FILE = LOGS / "auto-graphify.log"

CODE_NOISE = {"node_modules", ".venv", "venv", ".git", "dist", "build",
              "__pycache__", ".next", ".cache", "coverage", ".pytest_cache"}
SOURCE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx"}

ENTRY_CANDIDATES = [
    "backend/main.py", "main.py", "app/main.py", "src/main.py",
    "src/app.js", "app.js", "server.js", "src/server.js", "index.js",
    "speak.py", "src/index.ts", "app/__init__.py",
]


def _log(msg: str) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass
    print(msg)


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


# --------------------------------------------------------------- detection
def find_entry(code_root: Path) -> str | None:
    for rel in ENTRY_CANDIDATES:
        if (code_root / rel).exists():
            return rel
    return None


def detect_stack(code_root: Path, entry: str | None) -> str:
    bits = []
    if (code_root / "requirements.txt").exists() or (code_root / "pyproject.toml").exists():
        text = ""
        for f in ("requirements.txt", "pyproject.toml"):
            p = code_root / f
            if p.exists():
                text += p.read_text(encoding="utf-8", errors="replace").lower()
        if "fastapi" in text:
            bits.append("FastAPI")
        if "flask" in text:
            bits.append("Flask")
        if "sqlalchemy" in text:
            bits.append("SQLAlchemy")
        if not bits:
            bits.append("Python")
    pkg = code_root / "package.json"
    if pkg.exists():
        deps = _load(pkg)
        all_deps = {**deps.get("dependencies", {}), **deps.get("devDependencies", {})}
        if "fastify" in all_deps:
            bits.append("Fastify")
        elif "express" in all_deps:
            bits.append("Express")
        if "react" in all_deps:
            bits.append("React")
        if "vite" in all_deps:
            bits.append("Vite")
        if not bits:
            bits.append("Node.js")
    # Last-resort fallback: detect by source-file presence at top levels
    if not bits:
        for root, dirs, files in os.walk(code_root):
            dirs[:] = [d for d in dirs if d not in CODE_NOISE and not d.startswith(".")]
            exts = {Path(f).suffix for f in files}
            if {".py", ".pyw"} & exts:
                bits.append("Python")
                break
            if {".ts", ".tsx", ".js", ".jsx"} & exts:
                bits.append("Node.js")
                break
    return " + ".join(dict.fromkeys(bits)) or "unknown"


def newest_source_mtime(code_root: Path) -> date | None:
    """Walk source files, pruning noise dirs (node_modules etc.) during descent."""
    newest = 0.0
    for root, dirs, files in os.walk(code_root):
        dirs[:] = [d for d in dirs if d not in CODE_NOISE and not d.startswith(".")]
        for fn in files:
            if Path(fn).suffix in SOURCE_EXT:
                try:
                    newest = max(newest, (Path(root) / fn).stat().st_mtime)
                except OSError:
                    pass
    return date.fromtimestamp(newest) if newest else None


# --------------------------------------------------------------- clusters
_FASTAPI_RE = re.compile(r"include_router\(\s*([A-Za-z_][\w.]*)")
_FASTAPI_IMPORT_RE = re.compile(r"from\s+[\w.]*routers?[\w.]*\s+import\s+(.+)")
_EXPRESS_USE_RE = re.compile(r"app\.use\(\s*['\"]([^'\"]+)['\"]")
_FASTIFY_REG_RE = re.compile(r"register\(\s*require\(['\"][^'\"]*?([\w-]+)['\"]")


def extract_clusters(code_root: Path, entry: str | None) -> dict:
    clusters: dict = {}
    if entry:
        text = (code_root / entry).read_text(encoding="utf-8", errors="replace")
        # FastAPI routers
        for m in _FASTAPI_RE.finditer(text):
            name = m.group(1).split(".")[0].replace("_router", "").replace("router", "") or m.group(1)
            name = name.strip("_") or m.group(1)
            clusters.setdefault(name, {"description": f"{name} domain (auto-detected router)",
                                       "files": [entry], "obsidian_page": f"[[graph/{name}]]"})
        # Express/Fastify mounts
        for m in _EXPRESS_USE_RE.finditer(text):
            path = m.group(1).strip("/").split("/")[0]
            if path and path not in clusters:
                clusters[path] = {"description": f"{path} routes (auto-detected mount)",
                                  "files": [entry], "obsidian_page": f"[[graph/{path}]]"}
    # Fallback / supplement: top-level source dirs
    if len(clusters) < 3:
        for d in sorted(code_root.iterdir()):
            if d.is_dir() and d.name not in CODE_NOISE and not d.name.startswith("."):
                has_src = False
                for root, dirs, files in os.walk(d):
                    dirs[:] = [x for x in dirs if x not in CODE_NOISE and not x.startswith(".")]
                    if any(Path(f).suffix in SOURCE_EXT for f in files):
                        has_src = True
                        break
                if has_src:
                    clusters.setdefault(d.name, {
                        "description": f"{d.name} module (auto-detected directory)",
                        "files": [f"{d.name}/"], "obsidian_page": f"[[graph/{d.name}]]"})
    # Final fallback: root-level source files become module clusters (script projects)
    if len(clusters) < 2:
        for f in sorted(code_root.iterdir()):
            if f.is_file() and f.suffix in SOURCE_EXT and not f.name.startswith("_"):
                name = f.stem
                clusters.setdefault(name, {
                    "description": f"{f.name} module (root script)",
                    "files": [f.name], "obsidian_page": f"[[graph/{name}]]"})
    return clusters


def build_structural_graph(project: str, wiki_name: str, code_root: Path, stack_hint: str) -> dict:
    entry = find_entry(code_root)
    stack = detect_stack(code_root, entry) or stack_hint
    clusters = extract_clusters(code_root, entry)
    return {
        "meta": {
            "project": project,
            "description": f"{project} — structural graph (auto-generated; enrich critical_rules via LLM)",
            "stack": stack,
            "entry_point": entry or "unknown",
            "graph_updated": date.today().isoformat(),
            "graph_source": "auto_graphify_structural",
            "obsidian_path": f"{{WIKI_PATH}}\\{wiki_name}\\graph\\",
        },
        "architecture": {
            "type": "monorepo" if (code_root / "package.json").exists() and (code_root / "backend").exists() else "monolith",
            "backend": stack,
            "frontend": "React" if "React" in stack else "none",
            "databases": [],
            "external_services": [],
            "workers": "none",
            "auth": "unknown",
        },
        "critical_rules": [],
        "_enrichment_note": "Auto-generated structural graph. Run graphify skill (LLM) to add critical_rules + common_patterns.",
        "clusters": clusters,
        "common_patterns": {},
    }


# --------------------------------------------------------------- main
def run(dry_run: bool, max_builds: int) -> dict:
    wmap = _load(WIKI_MAP)
    mapping = wmap.get("mapping", {})
    metadata = wmap.get("metadata", {})
    base_code = Path(wmap.get("base_code_path", r"{{CODE_PATH}}"))
    base_wiki = Path(wmap.get("base_wiki_path", r"{{WIKI_PATH}}"))

    built, queued, skipped = [], [], []
    builds_done = 0

    for code_name, wiki_name in mapping.items():
        status = (metadata.get(wiki_name, {}) or {}).get("status", "active")
        if status != "active":
            continue
        code_root = base_code / code_name
        if not code_root.is_dir():
            queued.append({"project": wiki_name, "reason": "code dir missing", "code": str(code_root)})
            continue
        graph_file = base_wiki / wiki_name / "graph" / "knowledge_graph.json"

        if not graph_file.exists():
            # MISSING → build structural graph (safe, additive)
            if builds_done >= max_builds:
                queued.append({"project": wiki_name, "reason": "missing graph (build cap reached)"})
                continue
            if dry_run:
                _log(f"[DRY] WOULD BUILD structural graph: {wiki_name}")
                built.append(wiki_name)
                builds_done += 1
                continue
            try:
                stack_hint = (metadata.get(wiki_name, {}) or {}).get("stack", "")
                graph = build_structural_graph(wiki_name, wiki_name, code_root, stack_hint)
                graph_file.parent.mkdir(parents=True, exist_ok=True)
                graph_file.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
                n_clusters = len(graph["clusters"])
                _log(f"BUILT structural graph: {wiki_name} "
                     f"(stack={graph['meta']['stack']}, clusters={n_clusters})")
                built.append(wiki_name)
                builds_done += 1
                # Thin structural graph → always needs LLM enrichment (critical_rules etc.)
                thin = graph["meta"]["stack"] == "unknown" or n_clusters < 2
                queued.append({"project": wiki_name,
                               "reason": "newly built structural graph needs LLM enrichment"
                                         + (" (THIN — review clusters/stack)" if thin else ""),
                               "enrich": True})
            except Exception as e:
                _log(f"FAIL build {wiki_name}: {e}")
                queued.append({"project": wiki_name, "reason": f"build error: {e}"})
        else:
            # EXISTS → only queue if code is newer than graph (never overwrite quality)
            graph = _load(graph_file)
            gm = graph.get("meta", {})
            # Accept any date field convention: hand-made graphs use 'generated',
            # auto/structural use 'graph_updated', some use 'last_updated'.
            g_date = (gm.get("graph_updated") or gm.get("last_updated")
                      or gm.get("generated") or gm.get("graph_generated"))
            try:
                gd = date.fromisoformat(g_date[:10]) if g_date else None
            except ValueError:
                gd = None
            code_date = newest_source_mtime(code_root)
            is_auto = graph.get("meta", {}).get("graph_source") == "auto_graphify_structural"
            # Only flag SIGNIFICANT drift (>14d). Active projects change code daily —
            # a 1-day "drift" is noise, not architectural staleness.
            drift_days = (code_date - gd).days if (gd and code_date) else 0
            if drift_days > 14:
                queued.append({"project": wiki_name,
                               "reason": f"graph {drift_days}d behind code (significant drift)",
                               "graph_date": str(gd), "code_date": str(code_date),
                               "drift_days": drift_days, "enrich": True})
            elif is_auto:
                # structural graph that's still current but lacks critical_rules
                queued.append({"project": wiki_name, "reason": "structural graph needs LLM enrichment",
                               "enrich": True})
            else:
                skipped.append(wiki_name)

    payload = {
        "checked": datetime.now().isoformat(timespec="seconds"),
        "built": built,
        "queued_for_llm": queued,
        "skipped_fresh": skipped,
    }
    if not dry_run:
        QUEUE_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"auto_graphify: built {len(built)}, queued {len(queued)} for LLM, {len(skipped)} fresh")
    return payload


def main() -> None:
    p = argparse.ArgumentParser(description="Keep active-project knowledge graphs fresh, safely.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max", type=int, default=5, help="Max structural builds per run")
    args = p.parse_args()
    try:
        result = run(args.dry_run, args.max)
        if args.dry_run:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        _log(f"auto_graphify FATAL (suppressed): {e}")
    sys.exit(0)


if __name__ == "__main__":
    main()
