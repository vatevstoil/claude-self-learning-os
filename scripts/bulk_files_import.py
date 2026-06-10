#!/usr/bin/env python3
"""bulk_files_import.py — Embed flat .md files (not concepts/summaries dirs) into a
Pinecone namespace. Complements wiki_bulk_import.py for learnings/patterns/meta.

Chunks large files by top-level '## ' sections for better recall granularity.
Idempotent SHA256 IDs (re-runnable). type=promoted, NEVER_EXPIRE.

Usage:
    python bulk_files_import.py <namespace> <glob1> [glob2 ...]
Example:
    python bulk_files_import.py Fakturka.bg "{{WIKI_PATH}}/Fakturka.bg/wiki/sources/learnings.md"
"""
from __future__ import annotations

import glob as globmod
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
from pathlib import Path
from urllib.request import Request, urlopen

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
# Promotions feed the cross-project layer that cross_project_search reads — now
# LOCAL. Follow the memory backend.
MEMORY_BACKEND = os.environ.get("MEMORY_BACKEND", "local").strip().lower()


def _local():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import local_rag
    return local_rag
# multilingual-e5-large embeds only ~512 tokens. Cyrillic tokenizes denser (~2 chars/
# token), so cap chunks at ~1400 chars so the WHOLE chunk lands inside the model window
# (larger chunks get truncated → tail content silently lost from the vector).
MAX_CHARS = 1400


def _req(url, body, host=False):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    r = Request(url, data=data, method="POST")
    r.add_header("Api-Key", API_KEY)
    r.add_header("Content-Type", "application/json")
    r.add_header("X-Pinecone-API-Version", "2025-04")
    
    retries = 5
    backoff = 1.0
    for attempt in range(retries):
        try:
            with urlopen(r, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(backoff)
                backoff *= 2.0
                continue
            raise


def embed(text: str) -> list[float]:
    if MEMORY_BACKEND == "local":
        return _local().local_embed(text)
    out = _req("https://api.pinecone.io/embed", {
        "model": MODEL,
        "parameters": {"input_type": "passage", "truncate": "END"},
        "inputs": [{"text": text}],
    })
    return out["data"][0]["values"]


def chunk(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= MAX_CHARS:
        return [text] if text else []
    # split by top-level ## sections
    parts = re.split(r"(?=^##\s)", text, flags=re.MULTILINE)
    chunks, buf = [], ""
    for p in parts:
        if len(buf) + len(p) <= MAX_CHARS:
            buf += p
        else:
            if buf.strip():
                chunks.append(buf.strip())
            buf = p
    if buf.strip():
        chunks.append(buf.strip())
    # hard-split any oversized chunk
    final = []
    for c in chunks:
        while len(c) > MAX_CHARS:
            final.append(c[:MAX_CHARS])
            c = c[MAX_CHARS:]
        if c.strip():
            final.append(c.strip())
    return final


# Incremental manifest — skip embedding chunks whose content hasn't changed.
# Maximum efficiency: weekly re-runs only embed NEW/CHANGED content.
MANIFEST = Path.home() / ".claude" / "logs" / "bulk_import_manifest.json"


def _load_manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_manifest(m: dict) -> None:
    try:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def main():
    if len(sys.argv) < 3:
        print("usage: bulk_files_import.py <namespace> <glob1> [glob2 ...] [--force]")
        sys.exit(1)
    force = "--force" in sys.argv
    argv = [a for a in sys.argv if a != "--force"]
    sys.path.insert(0, str(Path(__file__).parent))
    from ns_util import sanitize_ns
    ns = sanitize_ns(argv[1])
    files = []
    for g in argv[2:]:
        files.extend(f for f in globmod.glob(g, recursive=True)
                     if os.path.isfile(f) and ".git" not in f)
    manifest = _load_manifest()
    saved = skipped = unchanged = failed = 0
    for fp in sorted(set(files)):
        try:
            text = Path(fp).read_text(encoding="utf-8", errors="replace")
        except OSError:
            failed += 1
            continue
        name = Path(fp).name
        for i, ch in enumerate(chunk(text)):
            if len(ch.strip()) < 40:
                skipped += 1
                continue
            cid = ns + "-promoted-" + hashlib.sha256((fp + str(i)).encode()).hexdigest()[:12]
            content_sha = hashlib.sha256(ch.encode("utf-8")).hexdigest()[:16]
            if not force and manifest.get(cid) == content_sha:
                unchanged += 1
                continue  # incremental: content not changed since last embed
            try:
                vec = embed(ch)
                _meta = {"type": "promoted", "ttl_days": 9999,
                         "source": name, "chunk": i, "text": ch[:1500]}
                if MEMORY_BACKEND == "local":
                    _local().upsert_vec(ns, cid, vec, _meta["text"], _meta)
                else:
                    _req(f"https://{HOST}/vectors/upsert", {
                        "namespace": ns,
                        "vectors": [{"id": cid, "values": vec, "metadata": _meta}],
                    })
                manifest[cid] = content_sha
                saved += 1
            except Exception as e:
                print(f"  FAIL {name}#{i}: {e}")
                failed += 1
    _save_manifest(manifest)
    print(f"Done [{ns}]. saved={saved} unchanged={unchanged} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
