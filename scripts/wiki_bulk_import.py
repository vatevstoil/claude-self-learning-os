#!/usr/bin/env python3
"""Bulk-import any wiki's distilled knowledge into a Pinecone namespace.

Generalized version of ai_video_bulk_import.py.

Usage:
    python wiki_bulk_import.py <wiki_base> <namespace>

    wiki_base: path to wiki root (must have wiki/concepts/ or wiki/summaries/ or knowledge/ or summaries/)
    namespace: Pinecone namespace target

Imports concepts/, summaries/ (and knowledge/ if exists) — anything with .md files.
Type=promoted, ttl=NEVER_EXPIRE. Idempotent SHA256 IDs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Load env
env_path = Path.home() / ".claude" / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

API_KEY = os.environ.get("PINECONE_API_KEY")
HOST = os.environ.get("PINECONE_INDEX_HOST")
MODEL = os.environ.get("PINECONE_EMBED_MODEL", "multilingual-e5-large")
# Promotions feed the cross-project knowledge layer that cross_project_search
# reads — which is now LOCAL. So this importer follows the memory backend too.
MEMORY_BACKEND = os.environ.get("MEMORY_BACKEND", "local").strip().lower()


def _local():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import local_rag
    return local_rag

EXCLUDE_DIRS = {"_archive", ".obsidian"}


def _req(url, body=None, method="GET"):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    r = Request(url, data=data, method=method)
    r.add_header("Api-Key", API_KEY)
    r.add_header("Content-Type", "application/json")
    r.add_header("X-Pinecone-API-Version", "2025-04")
    try:
        return json.loads(urlopen(r, timeout=60).read())
    except (HTTPError, URLError) as e:
        msg = e.read().decode()[:200] if hasattr(e, "read") else str(e)
        sys.exit(f"HTTP error: {msg}")


def embed(text: str, is_query: bool = False) -> list[float]:
    if MEMORY_BACKEND == "local":
        return _local().local_embed(text, is_query=is_query)
    resp = _req(
        "https://api.pinecone.io/embed",
        {
            "model": MODEL,
            "inputs": [{"text": text}],
            "parameters": {
                "input_type": "query" if is_query else "passage",
                "truncate": "END",
            },
        },
        method="POST",
    )
    return resp["data"][0]["values"]


def upsert(namespace: str, vectors: list[dict]) -> None:
    if MEMORY_BACKEND == "local":
        lr = _local()
        for v in vectors:
            md = v.get("metadata", {}) or {}
            lr.upsert_vec(namespace, v["id"], v["values"], md.get("text", ""), md)
        return
    _req(
        f"https://{HOST}/vectors/upsert",
        {"vectors": vectors, "namespace": namespace},
        method="POST",
    )


def make_id(namespace: str, content: str) -> str:
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    safe_ns = "".join(c if c.isascii() else "_" for c in namespace)
    return f"{safe_ns}-promoted-{h}"


def find_files(wiki_base: Path) -> list[Path]:
    """Find all .md files in concepts/, summaries/, knowledge/."""
    candidates = [
        wiki_base / "wiki" / "concepts",
        wiki_base / "wiki" / "summaries",
        wiki_base / "knowledge",
        wiki_base / "summaries",
        wiki_base / "wiki" / "knowledge",
    ]
    files = []
    for d in candidates:
        if not d.exists():
            continue
        # Recurse: nested knowledge dirs (e.g. concepts/blueprints/ with 64 files)
        # were silently skipped by the old flat iterdir() loop.
        for p in d.rglob("*.md"):
            if p.is_file() and not any(part in EXCLUDE_DIRS for part in p.parts):
                files.append(p)
    # De-dup
    return sorted(set(files))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wiki_base", help="Path to wiki root")
    ap.add_argument("namespace", help="Pinecone namespace")
    args = ap.parse_args()

    if not API_KEY or not HOST:
        sys.exit("ERROR: PINECONE_API_KEY/HOST not set in ~/.claude/.env")

    wiki_base = Path(args.wiki_base)
    if not wiki_base.exists():
        sys.exit(f"Wiki not found: {wiki_base}")

    files = find_files(wiki_base)
    if not files:
        print(f"No files found in {wiki_base}/wiki/(concepts|summaries) or {wiki_base}/(knowledge|summaries)")
        return 1

    print(f"Wiki: {wiki_base}")
    print(f"Namespace: {args.namespace}")
    print(f"Files to import: {len(files)}")
    print()

    saved = 0
    failed = 0
    skipped = 0

    for f in files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            print(f"  FAIL read {f.name}: {exc}")
            failed += 1
            continue

        if len(content) < 200:
            print(f"  SKIP {f.name}: too small ({len(content)}B)")
            skipped += 1
            continue

        try:
            entry_id = make_id(args.namespace, content)
            vec = embed(content[:6000], is_query=False)  # truncate for embed but save preview
            metadata = {
                "text": content[:500],
                "project": args.namespace,
                "type": "promoted",
                "date": date.today().isoformat(),
                "ttl_days": 9999,
                "source": f"{f.parent.name}/{f.name}",
            }
            upsert(args.namespace, [{
                "id": entry_id,
                "values": vec,
                "metadata": metadata,
            }])
            print(f"  OK   {f.parent.name}/{f.name} -> {entry_id}")
            saved += 1
        except Exception as exc:
            print(f"  FAIL {f.name}: {exc}")
            failed += 1

    print()
    print(f"Done. saved={saved} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
