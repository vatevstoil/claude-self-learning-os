#!/usr/bin/env python3
"""pinecone_cleanup_expired.py — Delete Pinecone vectors past their TTL.

Reads metadata.ttl_days and metadata.date.
If (today - date) > ttl_days, deletes the vector.
ttl_days >= 9999 means NEVER_EXPIRE (skipped).

Usage:
    python pinecone_cleanup_expired.py --namespace <ns>           # dry-run
    python pinecone_cleanup_expired.py --namespace <ns> --apply
    python pinecone_cleanup_expired.py --all-namespaces --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
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
DIM = 1024

KNOWN_NAMESPACES = [
    # Project wikis ({{WIKI_PATH}}\)
    "Cinemind", "Fakturka.bg", "StroyOffice Pro", "CasinoScore",
    "Higgsfield", "Autoagency", "_shared", "Trading", "Reed", "MiroFish",
    "Blender", "Claude",
    # Research wikis ({{RESEARCH_PATH}}\) — ASCII namespaces preferred
    "Claude Trading", "Claude Code Resurch", "Invest",
    "AI Video", "Claude Video", "Resurch", "OpenClaw",
    # Cyrillic folders → ASCII namespaces (see auto_pinecone_save RESEARCH_NS_MAP):
    "PetarDanov",      # Петър Дънов
    "{{PRIVATE_NS}}",   # {{PRIVATE_NS}}
    "{{PRIVATE_NS}}",        # {{PRIVATE_NS}}
    # System meta-work (~/.claude/*)
    "_claude_meta",
]


def _req(url, body=None, method="GET"):
    data = json.dumps(body).encode() if body else None
    r = Request(url, data=data, method=method)
    r.add_header("Api-Key", API_KEY)
    r.add_header("Content-Type", "application/json")
    r.add_header("X-Pinecone-API-Version", "2025-04")
    try:
        return json.loads(urlopen(r, timeout=30).read())
    except HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()[:200]}")
    except URLError as e:
        sys.exit(f"Network error: {e.reason}")
    except Exception as e:
        sys.exit(f"Unexpected error in request: {e}")


def fetch_with_metadata(namespace: str) -> list[dict]:
    """Fetch all vectors with metadata via zero-vector query."""
    zero_vec = [0.0] * DIM
    resp = _req(
        f"https://{HOST}/query",
        {
            "vector": zero_vec,
            "topK": 10000,
            "namespace": namespace,
            "includeMetadata": True,
        },
        method="POST",
    )
    return resp.get("matches", [])


def find_expired(vectors: list[dict]) -> list[dict]:
    """Return vectors whose age (today - date) exceeds ttl_days."""
    today = date.today()
    expired = []
    for v in vectors:
        meta = v.get("metadata", {})
        date_str = meta.get("date")
        ttl_raw = meta.get("ttl_days")
        if not date_str or ttl_raw is None:
            continue
        try:
            ttl = int(float(ttl_raw))  # tolerate "365" or "365.0" or 365
        except (TypeError, ValueError):
            continue
        if ttl >= 9999:
            continue  # NEVER_EXPIRE
        try:
            entry_date = datetime.strptime(str(date_str), "%Y-%m-%d").date()
        except ValueError:
            continue
        age = (today - entry_date).days
        if age > ttl:
            expired.append(v)
    return expired


def delete_ids(namespace: str, ids: list[str]) -> None:
    """Delete vector IDs in batches of 1000."""
    for i in range(0, len(ids), 1000):
        batch = ids[i:i + 1000]
        _req(
            f"https://{HOST}/vectors/delete",
            {"ids": batch, "namespace": namespace},
            method="POST",
        )


def backfill_metadata(namespace: str, default_ttl: int, default_date: str) -> int:
    """Backfill ttl_days/date metadata on vectors that lack them.

    Returns count of vectors updated. Does NOT change text/values.
    """
    # Need values + metadata to upsert
    zero_vec = [0.0] * DIM
    resp = _req(
        f"https://{HOST}/query",
        {
            "vector": zero_vec,
            "topK": 10000,
            "namespace": namespace,
            "includeMetadata": True,
            "includeValues": True,
        },
        method="POST",
    )
    matches = resp.get("matches", [])

    to_update = []
    for v in matches:
        meta = v.get("metadata", {}) or {}
        if meta.get("ttl_days") is None or meta.get("date") is None or meta.get("type") is None:
            new_meta = dict(meta)
            new_meta.setdefault("type", "learning")
            new_meta.setdefault("date", default_date)
            new_meta.setdefault("ttl_days", default_ttl)
            new_meta.setdefault("project", namespace)
            to_update.append({
                "id": v["id"],
                "values": v["values"],
                "metadata": new_meta,
            })

    # Upsert in batches of 100
    for i in range(0, len(to_update), 100):
        batch = to_update[i:i + 100]
        _req(
            f"https://{HOST}/vectors/upsert",
            {"vectors": batch, "namespace": namespace},
            method="POST",
        )
    return len(to_update)


def process_namespace(namespace: str, apply: bool) -> int:
    vectors = fetch_with_metadata(namespace)
    expired = find_expired(vectors)
    print(f"[{namespace}] {len(vectors)} total, {len(expired)} expired")
    if not expired:
        return 0
    for v in expired[:5]:
        meta = v.get("metadata", {})
        print(f"  - {v['id']} (type={meta.get('type', '?')}, "
              f"date={meta.get('date', '?')}, ttl={meta.get('ttl_days', '?')})")
    if len(expired) > 5:
        print(f"  ... and {len(expired) - 5} more")
    if apply:
        delete_ids(namespace, [v["id"] for v in expired])
        print(f"  Deleted {len(expired)}")
    return len(expired)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--namespace")
    p.add_argument("--all-namespaces", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--backfill-ttl", type=int, metavar="DAYS",
                   help="Tag vectors lacking metadata with given ttl_days")
    p.add_argument("--backfill-date", default="2026-04-01",
                   help="Default date for backfill (YYYY-MM-DD)")
    args = p.parse_args()

    # Local backend: the live store is local_rag (sqlite). Run TTL expiry there
    # in one pass (its meta carries ttl_days/date just like Pinecone's did).
    if os.environ.get("MEMORY_BACKEND", "local").strip().lower() == "local":
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import local_rag
        n = local_rag.delete_expired(apply=args.apply)
        print(f"{'DELETED' if args.apply else 'WOULD DELETE'}: {n} expired vectors (local).")
        return

    if not API_KEY or not HOST:
        sys.exit("ERROR: PINECONE_API_KEY or PINECONE_INDEX_HOST not set")

    if args.all_namespaces:
        targets = KNOWN_NAMESPACES
    elif args.namespace:
        targets = [args.namespace]
    else:
        sys.exit("--namespace or --all-namespaces required")

    if args.backfill_ttl is not None:
        if args.backfill_ttl <= 0:
            sys.exit("--backfill-ttl must be positive")
        total = 0
        for ns in targets:
            updated = backfill_metadata(ns, args.backfill_ttl, args.backfill_date)
            print(f"[{ns}] backfilled {updated} vectors")
            total += updated
        print(f"\nBackfilled {total} vectors with ttl_days={args.backfill_ttl}, date={args.backfill_date}")
        return  # don't continue to expired-cleanup in same run

    total = 0
    for ns in targets:
        total += process_namespace(ns, args.apply)
    mode = "DELETED" if args.apply else "WOULD DELETE"
    print(f"\n{mode}: {total} expired vectors total.")


if __name__ == "__main__":
    main()
