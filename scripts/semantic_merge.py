#!/usr/bin/env python3
"""semantic_merge.py -- Semantic de-duplication via cosine similarity.

Fetches all vectors in a Pinecone namespace, computes pairwise cosine similarity,
and for pairs above a threshold keeps the strongest (by recall hit_count, tiebreak
newer date) and deletes the rest.

Usage:
    python semantic_merge.py --namespace _claude_meta              # dry-run (default)
    python semantic_merge.py --namespace _claude_meta --apply      # actually delete
    python semantic_merge.py --all-namespaces                      # dry-run all ns
    python semantic_merge.py --all-namespaces --apply              # delete all ns
    python semantic_merge.py --namespace Fakturka.bg --threshold 0.90

Strategy:
    1. Query namespace with zero-vector topK=10000 (includeValues + includeMetadata).
    2. Compute O(n²) pairwise cosine similarity in pure Python.
    3. For each pair > threshold, keep the vector with higher recall hit_count
       (from recall-tracker.json); tiebreak by newer metadata.date.
    4. Backup all losers to ~/.claude/logs/semantic-merge-backup-{ts}-{ns}.jsonl.
    5. Delete losers via Pinecone delete API (only if --apply).

Namespace enumeration for --all-namespaces:
    Reads unique namespaces from ~/.claude/logs/recall-tracker.json values'
    "namespace" field, then adds a small default set (_claude_meta).
    If the file is missing or empty, only the default set is used.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import numpy as _np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

# ---------------------------------------------------------------------------
# Environment / config
# ---------------------------------------------------------------------------

_env_path = Path.home() / ".claude" / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

API_KEY: str = os.environ.get("PINECONE_API_KEY", "")
HOST: str = os.environ.get("PINECONE_INDEX_HOST", "")
MEMORY_BACKEND: str = os.environ.get("MEMORY_BACKEND", "local").strip().lower()
LOGS_DIR: Path = Path.home() / ".claude" / "logs"


def _local():
    import sys as _s, os as _o
    _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
    import local_rag
    return local_rag
DEFAULT_TRACKER: Path = LOGS_DIR / "recall-tracker.json"
DIM: int = 1024
DEFAULT_NAMESPACES: list[str] = ["_claude_meta"]

# ---------------------------------------------------------------------------
# Pure functions (covered by unit tests — no network, no I/O)
# ---------------------------------------------------------------------------


def cosine(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors in pure Python.

    Args:
        a: First vector as a list of floats.
        b: Second vector as a list of floats (must match length of ``a``).

    Returns:
        Cosine similarity in [-1, 1], or 0.0 if either vector has zero norm.
    """
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def find_duplicates(
    vectors: list[dict],
    threshold: float = 0.95,
) -> list[tuple[str, str]]:
    """Find near-duplicate vector pairs whose cosine similarity exceeds threshold.

    Uses numpy vectorized matrix multiply when available (~100x faster than pure
    Python for n>200), falling back to the O(n²) pure-Python path otherwise.

    Args:
        vectors: List of dicts, each with ``"id"`` (str) and ``"values"``
            (list[float]) keys.
        threshold: Cosine similarity threshold above which a pair is considered
            a near-duplicate. Default 0.95.

    Returns:
        List of (id_a, id_b) tuples — each unordered pair appears exactly once.
        Only pairs with cosine > threshold are included.
    """
    if _HAS_NUMPY and len(vectors) > 1:
        mat = _np.array([v["values"] for v in vectors], dtype=_np.float32)
        norms = _np.linalg.norm(mat, axis=1, keepdims=True)
        norms = _np.where(norms == 0, 1.0, norms)
        mat_norm = mat / norms
        # Upper-triangle mask avoids double-counting (i < j only)
        sim = mat_norm @ mat_norm.T
        i_idx, j_idx = _np.where(
            (sim > threshold)
            & (_np.arange(len(vectors))[:, None] < _np.arange(len(vectors))[None, :])
        )
        return [(vectors[int(i)]["id"], vectors[int(j)]["id"])
                for i, j in zip(i_idx.tolist(), j_idx.tolist())]

    pairs: list[tuple[str, str]] = []
    n = len(vectors)
    for i in range(n):
        for j in range(i + 1, n):
            sim = cosine(vectors[i]["values"], vectors[j]["values"])
            if sim > threshold:
                pairs.append((vectors[i]["id"], vectors[j]["id"]))
    return pairs


def pick_survivor(
    id_a: str,
    id_b: str,
    recall_data: dict,
    meta_a: dict,
    meta_b: dict,
) -> str:
    """Choose which vector survives a near-duplicate merge.

    Decision order:
        1. Higher ``hit_count`` in recall_data wins.
        2. Tiebreak: newer ``date`` field in metadata wins.
        3. Tiebreak: lexicographically greater id (deterministic fallback).

    Args:
        id_a: ID of the first vector.
        id_b: ID of the second vector.
        recall_data: Dict mapping vector IDs to dicts with an optional
            ``"hit_count"`` int key (from recall-tracker.json).
        meta_a: Metadata dict for vector ``id_a``.
        meta_b: Metadata dict for vector ``id_b``.

    Returns:
        The ID of the vector that should be kept.
    """
    count_a = recall_data.get(id_a, {}).get("hit_count", 0)
    count_b = recall_data.get(id_b, {}).get("hit_count", 0)
    if count_a != count_b:
        return id_a if count_a > count_b else id_b

    # Tiebreak by newer date (ISO string comparison is valid for YYYY-MM-DD)
    date_a = meta_a.get("date", "1970-01-01")
    date_b = meta_b.get("date", "1970-01-01")
    if date_a != date_b:
        return id_a if date_a > date_b else id_b

    # Final deterministic tiebreak
    return id_a if id_a >= id_b else id_b


# ---------------------------------------------------------------------------
# Network helpers (never crash — raise RuntimeError on failure)
# ---------------------------------------------------------------------------


def _req(url: str, body: dict | None = None, method: str = "GET") -> dict:
    """Execute a Pinecone REST API request.

    Args:
        url: Full URL to request.
        body: Optional JSON body for POST/PATCH requests.
        method: HTTP method string.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        RuntimeError: On missing credentials, HTTP errors, or network failures.
    """
    if not API_KEY or not HOST:
        raise RuntimeError("PINECONE_API_KEY or PINECONE_INDEX_HOST not set")
    data_bytes = json.dumps(body).encode() if body else None
    req = Request(url, data=data_bytes, method=method)
    req.add_header("Api-Key", API_KEY)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Pinecone-API-Version", "2025-04")
    try:
        return json.loads(urlopen(req, timeout=30).read())
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode()[:200]}")
    except URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}")
    except Exception as exc:
        raise RuntimeError(f"Unexpected request error: {exc}")


def fetch_all_vectors(namespace: str) -> list[dict]:
    """Fetch all vectors in a namespace using a zero-vector similarity query.

    Uses topK=10000 with includeValues and includeMetadata so that cosine
    similarity can be computed locally and metadata (date, text) is available
    for survivor selection.

    Args:
        namespace: Pinecone namespace to query.

    Returns:
        List of vector match dicts from the Pinecone query response.

    Raises:
        RuntimeError: Propagated from ``_req`` on network/API failure.
    """
    if MEMORY_BACKEND == "local":
        return _local().fetch_all(namespace)
    zero_vec = [0.0] * DIM
    body = {
        "vector": zero_vec,
        "topK": 10000,
        "namespace": namespace,
        "includeValues": True,
        "includeMetadata": True,
    }
    resp = _req(f"https://{HOST}/query", body, method="POST")
    return resp.get("matches", [])


def _load_recall_data(recall_path: Path) -> dict:
    """Load recall-tracker.json safely, returning empty dict on any error.

    Args:
        recall_path: Path to the recall-tracker.json file.

    Returns:
        Dict of {vector_id: {hit_count: int, ...}} or empty dict on failure.
    """
    if not recall_path.exists():
        return {}
    try:
        return json.loads(recall_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _backup_vectors(namespace: str, vectors: list[dict], ts: str) -> Path:
    """Write loser vectors to a JSONL backup file before deletion.

    Args:
        namespace: Pinecone namespace (used in filename).
        vectors: List of full vector dicts (id + values + metadata).
        ts: Timestamp string for the filename.

    Returns:
        Path of the created backup file.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    safe_ns = namespace.replace("/", "_").replace("\\", "_")
    out = LOGS_DIR / f"semantic-merge-backup-{ts}-{safe_ns}.jsonl"
    with out.open("w", encoding="utf-8") as fh:
        for v in vectors:
            fh.write(
                json.dumps({"namespace": namespace, "vector": v}, ensure_ascii=False) + "\n"
            )
    return out


def _delete_ids(namespace: str, ids: list[str]) -> None:
    """Delete vector IDs from Pinecone in batches of 1000.

    Args:
        namespace: Pinecone namespace.
        ids: List of vector IDs to delete.

    Raises:
        RuntimeError: Propagated from ``_req`` on failure.
    """
    if MEMORY_BACKEND == "local":
        lr = _local()
        for vid in ids:
            lr.delete(namespace, vid)
        return
    for i in range(0, len(ids), 1000):
        batch = ids[i : i + 1000]
        _req(
            f"https://{HOST}/vectors/delete",
            {"ids": batch, "namespace": namespace},
            method="POST",
        )


def _namespaces_from_tracker(recall_path: Path) -> list[str]:
    """Extract unique namespace names from recall-tracker.json.

    Falls back gracefully to an empty list if the file is missing or malformed.

    Args:
        recall_path: Path to recall-tracker.json.

    Returns:
        Sorted list of unique namespace strings found in the tracker.
    """
    data = _load_recall_data(recall_path)
    seen: set[str] = set()
    for entry in data.values():
        if isinstance(entry, dict):
            ns = entry.get("namespace")
            if ns:
                seen.add(ns)
    return sorted(seen)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def merge_namespace(
    namespace: str,
    threshold: float = 0.95,
    apply: bool = False,
    recall_path: Path = DEFAULT_TRACKER,
) -> list[str]:
    """Detect and optionally merge near-duplicate vectors in one namespace.

    Workflow:
        1. Fetch all vectors (with values + metadata).
        2. Compute pairwise cosine similarity.
        3. For each near-duplicate pair pick the survivor.
        4. Collect losers (de-duplicated so each loser is counted once).
        5. Backup losers to JSONL.
        6. If apply=True, delete losers from Pinecone.

    Args:
        namespace: Pinecone namespace to process.
        threshold: Cosine similarity cutoff. Default 0.95.
        apply: If True, perform deletions. If False (default), dry-run only.
        recall_path: Path to recall-tracker.json for hit_count data.

    Returns:
        List of human-readable log strings describing what was (or would be)
        merged.
    """
    logs: list[str] = []

    # Fetch
    try:
        vectors = fetch_all_vectors(namespace)
    except RuntimeError as exc:
        logs.append(f"ERROR fetching ns={namespace}: {exc}")
        return logs

    logs.append(f"Fetched {len(vectors)} vectors from ns={namespace}")

    if len(vectors) < 2:
        logs.append("  Too few vectors to compare — skipping.")
        return logs

    # Filter out vectors without values (sparse/metadata-only)
    usable = [v for v in vectors if v.get("values")]
    skipped = len(vectors) - len(usable)
    if skipped:
        logs.append(f"  Skipped {skipped} vectors with no values (sparse/metadata-only).")

    if len(usable) < 2:
        logs.append("  Too few usable vectors — skipping.")
        return logs

    # Size guard: find_duplicates is O(n²) pure-Python cosine. Beyond a few
    # thousand vectors it cannot finish in a sane time (the weekly run timed out
    # after the vault was fully indexed: PetarDanov 30k, N8N 4k, etc.). Those are
    # source corpora, not memory to dedup. Skip with guidance — exact-hash dedup
    # (cleanup_pinecone.py, O(n)) is the right tool for large namespaces.
    _MAX_PAIRWISE = int(os.environ.get("SEMANTIC_MERGE_MAX", "1500"))
    if len(usable) > _MAX_PAIRWISE:
        logs.append(
            f"  SKIP: {len(usable)} vectors > {_MAX_PAIRWISE} cap — O(n²) pairwise "
            f"infeasible. Use exact-hash dedup: python cleanup_pinecone.py "
            f"--namespace {namespace} --apply"
        )
        return logs

    # Find near-duplicate pairs
    pairs = find_duplicates(usable, threshold=threshold)
    if not pairs:
        logs.append(f"  No near-duplicates found above threshold={threshold}.")
        return logs

    logs.append(f"  Found {len(pairs)} near-duplicate pair(s) (threshold={threshold}).")

    # Build lookup maps
    recall_data = _load_recall_data(recall_path)
    by_id = {v["id"]: v for v in usable}

    # Resolve pairs into losers (one pass, skip already-condemned ids)
    survivor_set: set[str] = set()
    loser_set: set[str] = set()

    for id_a, id_b in pairs:
        # If one side is already a confirmed loser, the survivor is the other
        if id_a in loser_set:
            # id_a is already gone; id_b survives (unless it's also a loser)
            if id_b not in loser_set:
                survivor_set.add(id_b)
            continue
        if id_b in loser_set:
            if id_a not in loser_set:
                survivor_set.add(id_a)
            continue

        meta_a = by_id[id_a].get("metadata") or {}
        meta_b = by_id[id_b].get("metadata") or {}
        survivor = pick_survivor(id_a, id_b, recall_data, meta_a, meta_b)
        loser = id_b if survivor == id_a else id_a

        survivor_set.add(survivor)
        loser_set.add(loser)

        # Build a short description for logging
        recall_a = recall_data.get(id_a, {}).get("hit_count", 0)
        recall_b = recall_data.get(id_b, {}).get("hit_count", 0)
        sim = cosine(by_id[id_a]["values"], by_id[id_b]["values"])
        text_snippet = (
            by_id[survivor].get("metadata", {}).get("text", "")[:60].replace("\n", " ")
        )
        logs.append(
            f"  MERGE {id_a}(rc={recall_a}) + {id_b}(rc={recall_b})"
            f" sim={sim:.4f} -> keep={survivor} | '{text_snippet}...'"
        )

    if not loser_set:
        logs.append("  All pairs resolved with no net deletions needed.")
        return logs

    loser_vectors = [by_id[lid] for lid in loser_set if lid in by_id]
    logs.append(f"  Total losers: {len(loser_vectors)}")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    if not apply:
        logs.append(
            f"  DRY RUN — {len(loser_vectors)} vector(s) would be deleted. "
            "Re-run with --apply to execute."
        )
        return logs

    # Backup
    try:
        backup_path = _backup_vectors(namespace, loser_vectors, ts)
        logs.append(f"  Backed up losers to {backup_path}")
    except Exception as exc:
        logs.append(f"  BACKUP FAILED: {exc} — aborting deletion for safety.")
        return logs

    # Delete
    try:
        _delete_ids(namespace, list(loser_set))
        logs.append(f"  Deleted {len(loser_set)} vector(s) from ns={namespace}.")
    except RuntimeError as exc:
        logs.append(f"  DELETE FAILED: {exc}")

    return logs


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for semantic merge."""
    parser = argparse.ArgumentParser(
        description="Semantic de-duplication of Pinecone vectors via cosine similarity.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--namespace", help="Single namespace to process.")
    parser.add_argument(
        "--all-namespaces",
        action="store_true",
        help=(
            "Process all namespaces discovered from recall-tracker.json "
            "plus the default set (_claude_meta). "
            "Mutually exclusive with --namespace."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete losers (default is dry-run).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.95,
        help="Cosine similarity threshold for near-duplicates (default: 0.95).",
    )
    parser.add_argument(
        "--recall-tracker",
        type=Path,
        default=DEFAULT_TRACKER,
        help=f"Path to recall-tracker.json (default: {DEFAULT_TRACKER}).",
    )
    args = parser.parse_args()

    if args.namespace and args.all_namespaces:
        parser.error("--namespace and --all-namespaces are mutually exclusive.")

    if not args.namespace and not args.all_namespaces:
        parser.error("Specify --namespace <ns> or --all-namespaces.")

    if MEMORY_BACKEND != "local" and (not API_KEY or not HOST):
        sys.exit("ERROR: PINECONE_API_KEY or PINECONE_INDEX_HOST not set in env or ~/.claude/.env")

    # Resolve namespace list
    if args.all_namespaces:
        tracker_ns = _namespaces_from_tracker(args.recall_tracker)
        namespaces = sorted(set(DEFAULT_NAMESPACES) | set(tracker_ns))
        print(f"[semantic-merge] Processing {len(namespaces)} namespace(s): {namespaces}")
    else:
        namespaces = [args.namespace]

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[semantic-merge] Mode={mode} threshold={args.threshold}\n")

    for ns in namespaces:
        print(f"--- namespace: {ns} ---")
        log_lines = merge_namespace(
            namespace=ns,
            threshold=args.threshold,
            apply=args.apply,
            recall_path=args.recall_tracker,
        )
        for line in log_lines:
            print(line)
        print()

    print("[semantic-merge] Done.")


if __name__ == "__main__":
    main()
