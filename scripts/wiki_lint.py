#!/usr/bin/env python3
"""wiki_lint.py — Check active project wikis against the ecosystem "wiki contract".

The contract (active projects only — dormant/missing-wiki are skipped):
    1. nav hub        : index.md OR overview.md
    2. session entry  : COMPACT_SNAPSHOT.md OR POCKET_GUIDE.md  (token-cheap start)
    3. learnings      : wiki/sources/learnings.md OR PLAYBOOK.md
    4. activity log   : wiki/log.md
    5. frontmatter    : nav hub starts with YAML frontmatter

Read-only. Writes a compliance report to ~/.claude/logs/wiki-lint.json and a
compact text summary. Never modifies wikis.

Usage:
    python wiki_lint.py
    python wiki_lint.py --json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

OBSIDIAN = Path(r"{{WIKI_PATH}}")
WIKI_MAP = OBSIDIAN / "_shared" / "wiki-map.json"
LOGS = Path.home() / ".claude" / "logs"


def _exists_any(base: Path, names: list[str]) -> bool:
    return any((base / n).exists() for n in names)


def _exists_rec(base: Path, names: list[str]) -> bool:
    """Exists at root or one level under wiki/."""
    if _exists_any(base, names):
        return True
    return _exists_any(base / "wiki", names)


def _has_frontmatter(base: Path, names: list[str]) -> bool:
    """Return True if ANY of the nav-hub candidates has valid YAML frontmatter.

    Bug fixed: the old implementation returned False immediately on the first
    readable file that lacked frontmatter (e.g. index.md), without checking
    the remaining candidates (e.g. overview.md which *does* have frontmatter).
    Now we check every existing candidate and return True as soon as one passes.
    """
    for n in names:
        for p in (base / n, base / "wiki" / n):
            if p.exists():
                try:
                    if p.read_text(encoding="utf-8", errors="replace").lstrip().startswith("---"):
                        return True
                    # File exists but has no frontmatter — keep looking at
                    # the remaining candidates instead of returning False here.
                except OSError:
                    pass
    return False


def lint_wiki(wiki_name: str) -> dict:
    root = OBSIDIAN / wiki_name
    checks = {
        "nav_hub": _exists_rec(root, ["index.md", "overview.md"]),
        "session_entry": _exists_rec(root, ["COMPACT_SNAPSHOT.md", "POCKET_GUIDE.md"]),
        "learnings": _exists_rec(root, ["PLAYBOOK.md"]) or (root / "wiki" / "sources" / "learnings.md").exists()
                     or (root / "sources" / "learnings.md").exists(),
        "log": (root / "wiki" / "log.md").exists() or (root / "log.md").exists(),
        "frontmatter": _has_frontmatter(root, ["index.md", "overview.md"]),
    }
    missing = [k for k, v in checks.items() if not v]
    score = round(100 * sum(checks.values()) / len(checks))
    return {"wiki": wiki_name, "score": score, "checks": checks, "missing": missing}


def run() -> dict:
    raw = json.loads(WIKI_MAP.read_text(encoding="utf-8", errors="replace"))
    mapping = raw.get("mapping", {})
    metadata = raw.get("metadata", {})

    results = []
    for wiki_name in sorted(set(mapping.values())):
        status = (metadata.get(wiki_name, {}) or {}).get("status", "active")
        if status != "active":
            continue  # only active wikis are held to the contract
        results.append(lint_wiki(wiki_name))

    avg = round(sum(r["score"] for r in results) / len(results)) if results else 0
    payload = {
        "checked": datetime.now().isoformat(timespec="seconds"),
        "active_wikis": len(results),
        "avg_score": avg,
        "results": sorted(results, key=lambda r: r["score"]),
    }
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / "wiki-lint.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    p = argparse.ArgumentParser(description="Lint active wikis against the contract.")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    payload = run()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Wiki contract compliance — avg {payload['avg_score']}% "
              f"across {payload['active_wikis']} active wikis\n")
        for r in payload["results"]:
            miss = ", ".join(r["missing"]) if r["missing"] else "—"
            print(f"  {r['score']:3d}%  {r['wiki']:<14} missing: {miss}")


if __name__ == "__main__":
    main()
