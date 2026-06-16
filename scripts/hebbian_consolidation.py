#!/usr/bin/env python3
"""hebbian_consolidation.py -- Hebbian TTL: memories you recall live longer.

"Fire together, wire together" -- if a Pinecone memory is recalled often,
extend its TTL proportionally using a log2 formula so noisy bulk recalls
cannot ratchet TTL to NEVER_EXPIRE in one or two runs.

Formula: ttl = min(ttl_base_for_type * (1 + log2(1 + hits)), NEVER_EXPIRE)
where ttl_base_for_type comes from TYPE_BASE_TTL (never the current
already-inflated ttl_days), stopping compound ratchet.

Quality floor: only recalls with avg_score >= MIN_COSINE_SCORE count.
Recalls below the floor are noise and do not extend TTL.

Called from stage3_dreaming weekly pass. Safe to call multiple times (idempotent).

Usage:
    python hebbian_consolidation.py              # dry-run (print what would change)
    python hebbian_consolidation.py --apply      # apply TTL updates to Pinecone
    python hebbian_consolidation.py --min-count 3 --apply
    python hebbian_consolidation.py --remediate  # one-shot reset of already-inflated TTLs
    python hebbian_consolidation.py --remediate --apply
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOGS_DIR = Path.home() / ".claude" / "logs"
DEFAULT_TRACKER = LOGS_DIR / "recall-tracker.json"
DEFAULT_SALIENCE = LOGS_DIR / "salience.json"
NEVER_EXPIRE = 9999

# Quality floor: recalls with avg_score below this threshold are noise and
# do NOT count toward Hebbian reinforcement (bge-m3 calibrated threshold).
MIN_COSINE_SCORE: float = 0.60

# Canonical TTL base per type — used in formula and remediation.
# These are the AUTHORITATIVE values; TTL logic never reads back
# the current (potentially inflated) ttl_days as the base.
TYPE_BASE_TTL: dict[str, int] = {
    "learning": 90,
    "gotcha": 180,
    "decision": 365,
    "pattern": 365,
    "antipattern": 365,
    "promoted": NEVER_EXPIRE,
}
DEFAULT_BASE_TTL = 90  # fallback for unknown/typeless vectors

# Salience bonus: high-stakes sessions extend TTL more aggressively
# score >= 0.8 (SECURITY+MONEY) -> +2 virtual recalls; >= 0.5 -> +1
_SALIENCE_BONUS_HIGH = 2   # score >= 0.8
_SALIENCE_BONUS_MID = 1    # score >= 0.5

env_path = Path.home() / ".claude" / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

API_KEY = os.environ.get("PINECONE_API_KEY", "")
HOST = os.environ.get("PINECONE_INDEX_HOST", "")
MEMORY_BACKEND = os.environ.get("MEMORY_BACKEND", "local").strip().lower()


def _local():
    import sys as _s, os as _o
    _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
    import local_rag
    return local_rag


def load_salience(path: Path = DEFAULT_SALIENCE) -> dict[str, float]:
    """Return session_id -> salience_score mapping from salience.json.

    Args:
        path: Path to salience.json (written by salience.py).

    Returns:
        Dict mapping session_id to float score in [0.0, 1.0].
        Empty dict if file absent or unreadable.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            sid: float(info.get("score", 0.0))
            for sid, info in data.items()
            if isinstance(info, dict)
        }
    except Exception:
        return {}


def type_base_ttl(vtype: str | None) -> int:
    """Return the canonical base TTL for a given vector type.

    Args:
        vtype: Value of meta['type']; None or unknown types fall back to
               DEFAULT_BASE_TTL (90 days).

    Returns:
        Base TTL in days from TYPE_BASE_TTL, or DEFAULT_BASE_TTL.
    """
    return TYPE_BASE_TTL.get(vtype or "", DEFAULT_BASE_TTL)


def compute_hebbian_ttl(base_ttl: int, recall_count: int) -> int:
    """Compute effective TTL using logarithmic Hebbian reinforcement formula.

    Formula: min(base_ttl * (1 + log2(1 + recall_count)), NEVER_EXPIRE).

    The log2 damping prevents bulk-recall events from ratcheting TTL to
    NEVER_EXPIRE in one or two runs (old linear formula: base * (1+count)
    reached 9999 in a single pass with count=26 at base=365).

    The caller MUST pass the TYPE-CANONICAL base_ttl (from type_base_ttl()),
    never the current stored ttl_days — otherwise compound ratchet resumes.

    Args:
        base_ttl: Canonical TTL base for the vector's type (days).
        recall_count: Number of quality-filtered recall hits (avg_score >= MIN_COSINE_SCORE).

    Returns:
        New TTL in days, capped at NEVER_EXPIRE (9999).
    """
    return min(int(base_ttl * (1 + math.log2(1 + recall_count))), NEVER_EXPIRE)


def get_high_recall_ids(
    tracker_path: Path = DEFAULT_TRACKER,
    min_count: int = 2,
    min_score: float = MIN_COSINE_SCORE,
) -> dict[str, dict]:
    """Return vectors recalled >= min_count times AND above quality floor.

    Vectors whose avg_score is below min_score are passive noise — bulk
    embedding collisions that should not extend TTL. Only quality recalls count.

    Args:
        tracker_path: Path to the recall-tracker.json file.
        min_count: Minimum hit_count threshold to include a vector.
        min_score: Minimum avg_score (cosine similarity) to consider a recall
            meaningful. Defaults to MIN_COSINE_SCORE (0.60 for bge-m3).

    Returns:
        Dict mapping vector_id -> tracker entry for qualifying vectors.
        Returns empty dict if the file does not exist or cannot be parsed.
    """
    if not tracker_path.exists():
        return {}
    try:
        data = json.loads(tracker_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {
        vid: info
        for vid, info in data.items()
        if isinstance(info, dict)
        and info.get("hit_count", 0) >= min_count
        and float(info.get("avg_score", 1.0)) >= min_score
    }


def _req(url: str, body: dict | None = None, method: str = "GET") -> dict:
    """Execute a Pinecone REST API request.

    Args:
        url: Full URL to request.
        body: Optional JSON body (for POST/PATCH).
        method: HTTP method string.

    Returns:
        Parsed JSON response dict.

    Raises:
        RuntimeError: On missing credentials, HTTP errors, or network failures.
    """
    if not API_KEY or not HOST:
        raise RuntimeError("PINECONE credentials not set")
    data_bytes = json.dumps(body).encode() if body else None
    r = Request(url, data=data_bytes, method=method)
    r.add_header("Api-Key", API_KEY)
    r.add_header("Content-Type", "application/json")
    r.add_header("X-Pinecone-API-Version", "2025-04")
    try:
        return json.loads(urlopen(r, timeout=30).read())
    except HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:200]}")
    except URLError as e:
        raise RuntimeError(f"Network error: {e.reason}")


def fetch_vectors(namespace: str, ids: list[str]) -> list[dict]:
    """Fetch vectors by ID from Pinecone (returns values + metadata for upsert-back).

    Args:
        namespace: Pinecone namespace to query.
        ids: List of vector IDs to fetch (max 100 per call).

    Returns:
        List of vector dicts with ``id``, ``values``, and ``metadata`` fields.
    """
    if not ids:
        return []
    if MEMORY_BACKEND == "local":
        return _local().fetch(namespace, ids[:100])
    ids_param = "&".join(f"ids={i}" for i in ids[:100])
    url = f"https://{HOST}/vectors/fetch?namespace={namespace}&{ids_param}"
    resp = _req(url)
    return list((resp.get("vectors") or {}).values())


def apply_hebbian_ttls(
    high_recall: dict[str, dict],
    apply: bool = False,
    salience: dict[str, float] | None = None,
) -> list[str]:
    """Fetch high-recall vectors, compute new TTLs, and optionally upsert back.

    Groups vectors by namespace for efficient batch fetching. In dry-run mode
    (apply=False) only logs what would change without writing to Pinecone.

    Salience bonus: if a vector's metadata contains a ``session_id`` that
    appears in *salience* with score >= 0.5, virtual recall bonus is added
    (+1 for score >= 0.5, +2 for score >= 0.8). High-stakes events persist longer.

    Args:
        high_recall: Dict of {vector_id: tracker_entry} from get_high_recall_ids.
        apply: If True, write TTL updates back to Pinecone. If False, dry-run only.
        salience: Optional dict of session_id -> salience_score from load_salience().

    Returns:
        List of human-readable log lines describing actions taken or planned.
    """
    logs: list[str] = []

    # Group entries by namespace for batched fetches
    by_namespace: dict[str, list[tuple[str, dict]]] = {}
    for vid, info in high_recall.items():
        ns = info.get("namespace", "_claude_meta")
        by_namespace.setdefault(ns, []).append((vid, info))

    for ns, entries in by_namespace.items():
        ids = [vid for vid, _ in entries]
        try:
            vectors = fetch_vectors(ns, ids)
        except Exception as e:
            logs.append(f"SKIP ns={ns}: fetch failed ({e})")
            continue

        upsert_batch: list[dict] = []
        for vec in vectors:
            vid = vec.get("id", "")
            meta = vec.get("metadata") or {}
            values = vec.get("values") or []
            if not values:
                logs.append(f"SKIP {vid}: no values (sparse vector?)")
                continue
            hit_info = next((i for v, i in entries if v == vid), None)
            if not hit_info:
                continue
            # IMPORTANT: use the TYPE-canonical base, never the current stored
            # ttl_days (which may already be inflated by previous runs).
            # This stops compound ratchet.
            vtype = meta.get("type") or ""
            base_ttl = type_base_ttl(vtype)
            recall_count = hit_info.get("hit_count", 0)
            # Salience bonus: high-stakes sessions extend TTL more aggressively
            if salience:
                sal_score = salience.get(meta.get("session_id", ""), 0.0)
                if sal_score >= 0.8:
                    recall_count += _SALIENCE_BONUS_HIGH
                elif sal_score >= 0.5:
                    recall_count += _SALIENCE_BONUS_MID
            new_ttl = compute_hebbian_ttl(base_ttl, recall_count)
            if new_ttl <= base_ttl:
                logs.append(f"NOOP {vid}: ttl already {base_ttl}")
                continue
            logs.append(
                f"UPDATE {vid}: ttl {base_ttl} -> {new_ttl} (recall={recall_count})"
            )
            if apply:
                new_meta = {
                    **meta,
                    "ttl_days": new_ttl,
                    "hebbian_updated": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                }
                upsert_batch.append(
                    {"id": vid, "values": values, "metadata": new_meta}
                )

        if apply and upsert_batch:
            try:
                if MEMORY_BACKEND == "local":
                    lr = _local()
                    for item in upsert_batch:
                        lr.update_meta(ns, item["id"], item["metadata"])
                else:
                    _req(
                        f"https://{HOST}/vectors/upsert",
                        {"vectors": upsert_batch, "namespace": ns},
                        method="POST",
                    )
                logs.append(f"UPSERTED {len(upsert_batch)} in ns={ns}")
            except Exception as e:
                logs.append(f"UPSERT FAILED ns={ns}: {e}")

    return logs


def remediate_inflated_ttls(apply: bool = False) -> list[str]:
    """One-shot reset of already-inflated TTLs back to their type canonical base.

    Finds all local vectors that have ``hebbian_updated`` in metadata AND whose
    type is not 'promoted', then resets their ttl_days to TYPE_BASE_TTL for that
    type. This reverses the compound ratchet accumulated by the old linear formula.

    Only operates on the LOCAL backend (local_rag). Pinecone cloud backend is
    not touched by remediation — run against each backend separately.

    Args:
        apply: If True, write resets via local_rag.update_meta(). If False,
            only count and describe what would be changed (dry-run).

    Returns:
        List of human-readable log lines.
    """
    logs: list[str] = []
    try:
        lr = _local()
    except Exception as exc:
        logs.append(f"SKIP remediation: local_rag unavailable ({exc})")
        return logs

    try:
        all_ns = lr.all_namespaces()
    except Exception as exc:
        logs.append(f"SKIP remediation: cannot list namespaces ({exc})")
        return logs

    reset_count = 0
    for ns in all_ns:
        try:
            rows = lr.fetch_all(ns)
        except Exception as exc:
            logs.append(f"SKIP ns={ns}: fetch_all failed ({exc})")
            continue
        for row in rows:
            meta = row.get("metadata") or {}
            if not meta.get("hebbian_updated"):
                continue
            vtype = meta.get("type") or ""
            if vtype == "promoted":
                continue  # promoted = intentionally immortal, do not touch
            base = type_base_ttl(vtype)
            current = int(meta.get("ttl_days", base))
            if current == base:
                continue  # already at base, nothing to do
            vid = row["id"]
            logs.append(
                f"REMEDIATE ns={ns} id={vid} type={vtype or '?'}: "
                f"ttl {current} -> {base}"
            )
            if apply:
                new_meta = {**meta, "ttl_days": base,
                            "hebbian_remediated": datetime.now(timezone.utc).isoformat(
                                timespec="seconds")}
                try:
                    lr.update_meta(ns, vid, new_meta)
                    reset_count += 1
                except Exception as exc:
                    logs.append(f"  ERROR updating {vid}: {exc}")
            else:
                reset_count += 1

    action = "Would reset" if not apply else "Reset"
    logs.append(f"[remediate] {action} {reset_count} inflated vector(s)")
    return logs


def main() -> None:
    """CLI entry point for Hebbian TTL consolidation."""
    parser = argparse.ArgumentParser(description="Hebbian TTL consolidation")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply TTL updates to Pinecone (default: dry-run)",
    )
    parser.add_argument(
        "--min-count",
        type=int,
        default=2,
        help="Minimum recall count to trigger TTL extension",
    )
    parser.add_argument(
        "--remediate",
        action="store_true",
        help=(
            "One-shot reset of already-inflated TTLs back to type-canonical base. "
            "Use with --apply to actually write. Safe to run multiple times."
        ),
    )
    args = parser.parse_args()

    if args.remediate:
        logs = remediate_inflated_ttls(apply=args.apply)
        for line in logs:
            print(f"  {line}", file=sys.stderr)
        return

    high_recall = get_high_recall_ids(min_count=args.min_count)
    if not high_recall:
        print("[hebbian] No high-recall vectors to process", file=sys.stderr)
        return

    salience = load_salience()
    print(
        f"[hebbian] {len(high_recall)} high-recall vectors (apply={args.apply},"
        f" salience_sessions={len(salience)})",
        file=sys.stderr,
    )
    logs = apply_hebbian_ttls(high_recall, apply=args.apply, salience=salience)
    for line in logs:
        print(f"  {line}", file=sys.stderr)


if __name__ == "__main__":
    main()
