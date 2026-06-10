#!/usr/bin/env python3
"""cleanup_pinecone.py — De-duplicate Pinecone vectors by content hash.

Usage:
    python cleanup_pinecone.py --namespace <ns>           # dry-run (default)
    python cleanup_pinecone.py --namespace <ns> --apply   # actually delete
    python cleanup_pinecone.py --restore <backup-file>   # undo

Strategy:
    1. Query namespace with topK=10000 (with values + metadata) to fetch all vectors.
    2. Group by SHA256 of metadata.text.
    3. For each group with >1 entry, keep newest (by metadata.date), delete rest.
    4. Backup deleted vectors to ~/.claude/logs/pinecone-backup-YYYYMMDD-HHMMSS-NS.jsonl.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
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
LOGS_DIR = Path.home() / ".claude" / "logs"
DIM = 1024
MEMORY_BACKEND = os.environ.get("MEMORY_BACKEND", "local").strip().lower()


def _local():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import local_rag
    return local_rag


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


def fetch_all_vectors(namespace: str, include_values: bool = False) -> list[dict]:
    """Fetch all vectors in a namespace via a zero-vector query."""
    if MEMORY_BACKEND == "local":
        return _local().fetch_all(namespace)  # [{id, values, metadata}]
    zero_vec = [0.0] * DIM
    body = {
        "vector": zero_vec,
        "topK": 10000,
        "namespace": namespace,
        "includeMetadata": True,
    }
    if include_values:
        body["includeValues"] = True
    resp = _req(
        f"https://{HOST}/query",
        body,
        method="POST",
    )
    return resp.get("matches", [])


def group_duplicates(vectors: list[dict]) -> dict[str, list[dict]]:
    """Group vectors whose metadata.text hashes to the same SHA256 prefix."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for v in vectors:
        text = v.get("metadata", {}).get("text", "")
        if not text:
            continue
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        groups[h].append(v)
    return {k: g for k, g in groups.items() if len(g) > 1}


def pick_keeper(group: list[dict]) -> dict:
    """Keep the newest vector by metadata.date; tie-break by id (deterministic)."""
    def sort_key(v):
        return (v.get("metadata", {}).get("date", "1970-01-01"), v.get("id", ""))
    return max(group, key=sort_key)


def backup_vectors(namespace: str, vectors: list[dict]) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_ns = namespace.replace("/", "_").replace("\\", "_")
    out = LOGS_DIR / f"pinecone-backup-{ts}-{safe_ns}.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for v in vectors:
            f.write(json.dumps({"namespace": namespace, "vector": v}, ensure_ascii=False) + "\n")
    return out


def delete_ids(namespace: str, ids: list[str]) -> None:
    """Delete vector IDs in batches of up to 1000 (Pinecone limit)."""
    if MEMORY_BACKEND == "local":
        lr = _local()
        for vid in ids:
            lr.delete(namespace, vid)
        return
    for i in range(0, len(ids), 1000):
        batch = ids[i:i + 1000]
        _req(
            f"https://{HOST}/vectors/delete",
            {"ids": batch, "namespace": namespace},
            method="POST",
        )


def restore_from_backup(backup_path: Path) -> None:
    """Re-upsert vectors from a backup JSONL file (no embedding regen)."""
    by_ns: dict[str, list[dict]] = defaultdict(list)
    with backup_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            ns = entry["namespace"]
            v = entry["vector"]
            by_ns[ns].append({
                "id": v["id"],
                "values": v["values"],
                "metadata": v.get("metadata", {}),
            })
    for ns, vecs in by_ns.items():
        for i in range(0, len(vecs), 100):
            batch = vecs[i:i + 100]
            _req(
                f"https://{HOST}/vectors/upsert",
                {"vectors": batch, "namespace": ns},
                method="POST",
            )
            print(f"Restored {len(batch)} vectors to ns={ns}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--namespace", help="Namespace to clean (omit for --restore)")
    p.add_argument("--apply", action="store_true",
                   help="Actually delete (default is dry-run)")
    p.add_argument("--restore", type=Path,
                   help="Restore from a backup file instead of cleaning")
    args = p.parse_args()

    if MEMORY_BACKEND != "local" and (not API_KEY or not HOST):
        sys.exit("ERROR: PINECONE_API_KEY or PINECONE_INDEX_HOST not set")

    if args.restore:
        restore_from_backup(args.restore)
        return

    if not args.namespace:
        sys.exit("--namespace required (or use --restore)")

    print(f"Fetching vectors from ns={args.namespace}...")
    # First pass: metadata only, fast
    vectors = fetch_all_vectors(args.namespace, include_values=False)
    print(f"Found {len(vectors)} vectors.")

    dup_groups = group_duplicates(vectors)
    if not dup_groups:
        print("No duplicates found. Namespace is clean.")
        return

    # Second pass: re-fetch WITH values for backup (only if applying)
    if args.apply:
        vectors_with_values = fetch_all_vectors(args.namespace, include_values=True)
        # Re-group to ensure same set (vectors_with_values has same IDs)
        by_id = {v["id"]: v for v in vectors_with_values}
        dup_groups_full: dict[str, list[dict]] = defaultdict(list)
        for h, group in dup_groups.items():
            for v in group:
                full = by_id.get(v["id"])
                if full:
                    dup_groups_full[h].append(full)
        dup_groups = dict(dup_groups_full)

    to_delete: list[dict] = []
    for h, group in dup_groups.items():
        keeper = pick_keeper(group)
        keeper_id = keeper["id"]
        for v in group:
            if v["id"] != keeper_id:
                to_delete.append(v)
        text = group[0].get("metadata", {}).get("text", "")[:80]
        print(f"  Group {h[:8]}: {len(group)} copies, keep {keeper_id} | text='{text}...'")

    print(f"\nTotal: {len(to_delete)} vectors would be deleted across {len(dup_groups)} groups.")

    if not args.apply:
        print("DRY RUN. Re-run with --apply to execute.")
        return

    backup_path = backup_vectors(args.namespace, to_delete)
    print(f"Backed up to {backup_path}")

    ids = [v["id"] for v in to_delete]
    delete_ids(args.namespace, ids)
    print(f"Deleted {len(ids)} vectors. Done.")


if __name__ == "__main__":
    main()
