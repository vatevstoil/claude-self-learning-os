#!/usr/bin/env python3
"""knowledge_cleanup.py — Pinecone knowledge-base hygiene.

Two operations, DRY-RUN by default (use --apply to execute irreversible changes):

  1. purge-noise  — delete conversational wrap-up vectors that polluted a
                    knowledge namespace (reuses auto_pinecone_save's noise guard).
                    NEVER touches type=promoted vectors.

  2. consolidate  — merge alias namespaces into a canonical one (Pinecone has no
                    rename; this fetches values+metadata, re-upserts to the
                    canonical namespace, then deletes from the source).

Usage:
    python knowledge_cleanup.py purge-noise <namespace> [--apply]
    python knowledge_cleanup.py consolidate [--apply]
    python knowledge_cleanup.py report                 # full namespace map

Safe by default: prints what WOULD change. Nothing is deleted without --apply.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

_SCRIPTS = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS))

# Load .env
_env = Path.home() / ".claude" / ".env"
if _env.exists():
    for _l in _env.read_text(encoding="utf-8").splitlines():
        if "=" in _l and not _l.startswith("#"):
            _k, _v = _l.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

HOST = os.environ.get("PINECONE_INDEX_HOST")
KEY = os.environ.get("PINECONE_API_KEY")
DIM = int(os.environ.get("PINECONE_DIMENSION", "1024"))
MEMORY_BACKEND = os.environ.get("MEMORY_BACKEND", "local").strip().lower()


def _local():
    import local_rag
    return local_rag
_HDRS = {"Api-Key": KEY or "", "Content-Type": "application/json",
         "X-Pinecone-API-Version": "2025-04"}

# Alias → canonical consolidation map. Only well-established duplicates; meta
# namespaces (_meta vs _claude_meta) and distinct concepts (DCTL vs Davinci) are
# intentionally NOT merged.
CONSOLIDATE = {
    "Trading":        ["Claude Trading", "Claude-Trading"],
    "Fakturka.bg":    ["Facturka.bg"],
    "AI-Video":       ["AI Video"],
    "Davinci-Plugin": ["Davinci Plugin"],
    "CasinoScore":    ["CasinoScore-AI"],
    "WebDesign":      ["Web-Design", "Web-Designe"],
    "PetarDanov":     ["Petar-Danov"],
    "_shared":        ["shared"],
}


def _req(path: str, body: dict, host: str | None = None) -> dict:
    url = f"https://{host or HOST}{path}"
    r = Request(url, data=json.dumps(body).encode(), method="POST")
    for k, v in _HDRS.items():
        r.add_header(k, v)
    return json.loads(urlopen(r, timeout=60).read())


def _stats() -> dict:
    if MEMORY_BACKEND == "local":
        lr = _local()
        return {"namespaces": {n: {"vectorCount": len(lr.existing_ids(n))}
                               for n in lr.all_namespaces()}}
    return _req("/describe_index_stats", {})


def _dump(namespace: str, topk: int = 10000) -> list[dict]:
    """Return (near) all vectors of a namespace (id, score, metadata).

    Uses a NEUTRAL zero-vector query, not a text query. A text query
    (query_and_track) ranks by similarity and silently truncates large
    namespaces to the top-K most-similar vectors — so noise / aliased vectors
    below the cutoff become invisible to purge-noise/consolidate. A zero vector
    retrieves without semantic bias. Pinecone caps topK at 10000 per query, so
    for namespaces larger than that we log the partial coverage (no silent cap).
    """
    if MEMORY_BACKEND == "local":
        return _local().fetch_all(namespace)  # [{id, values, metadata}] — no cap
    try:
        n = int(_stats().get("namespaces", {}).get(namespace, {}).get("vectorCount", 0))
    except Exception:
        n = 0
    k = min(10000, max(topk, n + 100))
    if n > 10000:
        print(f"  ⚠ {namespace}: {n} vectors exceed Pinecone's 10000 query cap — "
              f"scanning top {k} (neutral). type=promoted is never deleted, so "
              f"re-run if needed.", file=sys.stderr)
    resp = _req("/query", {
        "vector": [0.0] * DIM,
        "topK": k,
        "namespace": namespace,
        "includeMetadata": True,
        "includeValues": False,
    })
    return resp.get("matches", []) or []


def _fetch_with_values(namespace: str, ids: list[str]) -> dict:
    """Fetch full vectors (values + metadata) by id for re-upsert."""
    out: dict = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        qs = "&".join(f"ids={pc_quote(x)}" for x in chunk)
        url = f"https://{HOST}/vectors/fetch?{qs}&namespace={pc_quote(namespace)}"
        r = Request(url, method="GET")
        for k, v in _HDRS.items():
            r.add_header(k, v)
        resp = json.loads(urlopen(r, timeout=60).read())
        out.update(resp.get("vectors", {}))
    return out


def pc_quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


def _delete(namespace: str, ids: list[str]) -> None:
    if MEMORY_BACKEND == "local":
        lr = _local()
        for vid in ids:
            lr.delete(namespace, vid)
        return
    for i in range(0, len(ids), 100):
        _req("/vectors/delete", {"ids": ids[i:i + 100], "namespace": namespace})


def _upsert(namespace: str, vectors: list[dict]) -> None:
    if MEMORY_BACKEND == "local":
        lr = _local()
        for v in vectors:
            md = v.get("metadata", {}) or {}
            lr.upsert_vec(namespace, v["id"], v["values"], md.get("text", ""), md)
        return
    for i in range(0, len(vectors), 100):
        _req("/vectors/upsert", {"vectors": vectors[i:i + 100], "namespace": namespace})


# --- purge-noise -----------------------------------------------------------

def purge_noise(namespace: str, apply: bool) -> None:
    from auto_pinecone_save import is_conversational_noise as noise
    rows = _dump(namespace)
    delete_ids, kept_promoted, kept_other = [], 0, 0
    preview = []
    for r in rows:
        meta = r.get("metadata", {}) or {}
        typ = meta.get("type", "?")
        txt = meta.get("text", "") or ""
        if typ == "promoted":
            kept_promoted += 1
            continue
        if noise(txt):
            delete_ids.append(r["id"])
            preview.append(f"  [{typ:10}] {txt[:60].splitlines()[0] if txt else ''}")
        else:
            kept_other += 1
    print(f"=== purge-noise '{namespace}' ===")
    print(f"KEEP: {kept_promoted} promoted + {kept_other} genuine")
    print(f"{'DELETED' if apply else 'WOULD DELETE'}: {len(delete_ids)} noise vectors")
    print("\n".join(preview))
    if apply and delete_ids:
        _delete(namespace, delete_ids)
        print(f"\n✓ Deleted {len(delete_ids)} vectors from '{namespace}'.")
    elif not apply:
        print("\n(dry-run — re-run with --apply to delete)")


# --- consolidate -----------------------------------------------------------

def consolidate(apply: bool) -> None:
    # Local sqlite supports native rename — recanonicalize() merges every ns into
    # its canonical form in one pass (no fetch/re-upsert/delete dance).
    if MEMORY_BACKEND == "local":
        lr = _local()
        if not apply:
            from ns_util import sanitize_ns
            pending = {n: sanitize_ns(n) for n in lr.all_namespaces() if sanitize_ns(n) != n}
            print("=== consolidate (local) ===")
            print(pending if pending else "all namespaces already canonical ✓")
            print("(dry-run — re-run with --apply)" if pending else "")
            return
        moved = lr.recanonicalize()
        print("=== consolidate (local, applied) ===")
        print(moved if moved else "all namespaces already canonical ✓")
        return
    stats = _stats().get("namespaces", {})
    print("=== consolidate alias namespaces ===")
    for canon, aliases in CONSOLIDATE.items():
        for alias in aliases:
            n = stats.get(alias, {}).get("vectorCount", 0)
            if n == 0:
                continue
            print(f"  {alias!r} ({n}) -> {canon!r}", end="")
            if not apply:
                print("  [dry-run]")
                continue
            rows = _dump(alias, topk=max(1000, n + 10))
            ids = [r["id"] for r in rows]
            fetched = _fetch_with_values(alias, ids)
            vectors = []
            for vid, v in fetched.items():
                vectors.append({
                    "id": vid if vid.startswith(canon) else f"{canon}-{vid}",
                    "values": v.get("values", []),
                    "metadata": {**(v.get("metadata") or {}), "migrated_from": alias},
                })
            vectors = [v for v in vectors if v["values"]]
            if vectors:
                _upsert(canon, vectors)
                _delete(alias, ids)
                print(f"  ✓ moved {len(vectors)}, deleted source")
            else:
                print("  ⚠ no values fetched — skipped (no delete)")
    if not apply:
        print("\n(dry-run — re-run with --apply to migrate + delete sources)")


def report() -> None:
    stats = _stats().get("namespaces", {})
    print("=== namespace report (by count) ===")
    for ns, info in sorted(stats.items(), key=lambda x: -x[1].get("vectorCount", 0)):
        print(f"  {ns:32} {info.get('vectorCount', 0):6}")


def main() -> None:
    if MEMORY_BACKEND != "local" and (not KEY or not HOST):
        sys.exit("ERROR: PINECONE_API_KEY / PINECONE_INDEX_HOST not set")
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    pn = sub.add_parser("purge-noise")
    pn.add_argument("namespace")
    pn.add_argument("--apply", action="store_true")
    pc_ = sub.add_parser("consolidate")
    pc_.add_argument("--apply", action="store_true")
    sub.add_parser("report")
    args = p.parse_args()
    if args.cmd == "purge-noise":
        purge_noise(args.namespace, args.apply)
    elif args.cmd == "consolidate":
        consolidate(args.apply)
    elif args.cmd == "report":
        report()


if __name__ == "__main__":
    main()
