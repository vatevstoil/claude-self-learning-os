#!/usr/bin/env python3
"""Cross-project Pinecone search — queries ALL namespaces at once.

Usage:
    python cross_project_search.py --query "JWT auth pattern"
    python cross_project_search.py --query "VAT validation" --top-k 5
    python cross_project_search.py --query "Stripe" --namespaces Fakturka.bg,Cinemind
    python cross_project_search.py --query "pattern" --json

Env loaded from ~/.claude/.env (PINECONE_API_KEY, PINECONE_INDEX_HOST).
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Env loading — same pattern as pinecone.py
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
MODEL: str = os.environ.get("PINECONE_EMBED_MODEL", "multilingual-e5-large")
MEMORY_BACKEND: str = os.environ.get("MEMORY_BACKEND", "local").strip().lower()

if MEMORY_BACKEND != "local" and (not API_KEY or not HOST):
    sys.exit("ERROR: PINECONE_API_KEY or PINECONE_INDEX_HOST not set in ~/.claude/.env")


def _local():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import local_rag
    return local_rag

# ---------------------------------------------------------------------------
# Namespace discovery
# ---------------------------------------------------------------------------
# Static fallback ONLY (used if the live discovery call fails). The previous
# hardcoded list drifted badly: 7 of its entries no longer existed and it
# missed 26 real namespaces — including PetarDanov (30k vectors). Always prefer
# discover_namespaces() so the search self-updates as projects come and go.
_FALLBACK_NAMESPACES: list[str] = [
    "Fakturka.bg", "StroyOffice", "Trading", "Cinemind", "CasinoScore",
    "_shared", "_claude_meta", "PetarDanov", "N8N", "AI-Video",
]

# Junk / drift namespaces to never search (garbage IDs + lowercase meta drift).
_JUNK_NAMESPACES: frozenset[str] = frozenset({
    "RSRSSRRS_RSRRR", "RRSSS-RSRRR", "claude_meta",
})
_MIN_VECTORS = 2  # skip single-vector test noise (e.g. a stray "Blender" ns)


def discover_namespaces() -> list[str]:
    """Return live namespaces from Pinecone, minus junk/near-empty ones.

    Includes _shared and _claude_meta on purpose — that is where cross-project
    lessons live, so a cross-project search SHOULD reach them. Ordered by size
    (largest knowledge bases first).
    """
    if MEMORY_BACKEND == "local":
        names = [n for n in _local().all_namespaces() if n not in _JUNK_NAMESPACES]
        return names or _FALLBACK_NAMESPACES
    try:
        resp = _req(f"https://{HOST}/describe_index_stats", {}, method="POST")
        ns = resp.get("namespaces", {})
        keep = [
            name for name, info in ns.items()
            if name not in _JUNK_NAMESPACES
            and int(info.get("vectorCount", 0)) >= _MIN_VECTORS
        ]
        if not keep:
            return _FALLBACK_NAMESPACES
        return sorted(keep, key=lambda n: -int(ns[n].get("vectorCount", 0)))
    except Exception as exc:  # noqa: BLE001
        _warn(f"namespace discovery failed ({exc}); using static fallback")
        return _FALLBACK_NAMESPACES


# ---------------------------------------------------------------------------
# HTTP helpers — same pattern as pinecone.py
# ---------------------------------------------------------------------------
def _req(url: str, body: dict[str, Any] | None = None, method: str = "GET") -> dict[str, Any]:
    data = json.dumps(body).encode() if body else None
    r = Request(url, data=data, method=method)
    r.add_header("Api-Key", API_KEY)
    r.add_header("Content-Type", "application/json")
    r.add_header("X-Pinecone-API-Version", "2025-04")
    resp = urlopen(r, timeout=30)
    return json.loads(resp.read())


def embed(text: str) -> list[float]:
    resp = _req(
        "https://api.pinecone.io/embed",
        {
            "model": MODEL,
            "inputs": [{"text": text}],
            "parameters": {"input_type": "query", "truncate": "END"},
        },
        method="POST",
    )
    return resp["data"][0]["values"]


# ---------------------------------------------------------------------------
# Per-namespace query
# ---------------------------------------------------------------------------
def query_namespace(vector: list[float], namespace: str, top_k: int) -> list[dict[str, Any]]:
    """Query one namespace; returns empty list on any error."""
    try:
        resp = _req(
            f"https://{HOST}/query",
            {
                "vector": vector,
                "topK": top_k,
                "namespace": namespace,
                "includeMetadata": True,
            },
            method="POST",
        )
        matches = resp.get("matches", [])
        for m in matches:
            m["_namespace"] = namespace
        return matches
    except HTTPError as exc:
        if exc.code == 404:
            return []  # namespace simply doesn't exist yet
        _warn(f"HTTP {exc.code} querying ns={namespace}: {exc.read().decode()[:120]}")
        return []
    except URLError as exc:
        _warn(f"Network error querying ns={namespace}: {exc.reason}")
        return []
    except Exception as exc:  # noqa: BLE001
        _warn(f"Unexpected error querying ns={namespace}: {exc}")
        return []


def _warn(msg: str) -> None:
    print(f"  [warn] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def _preview(metadata: dict[str, Any], length: int = 60) -> str:
    text = metadata.get("text") or metadata.get("summary") or ""
    text = text.replace("\n", " ").strip()
    return text[:length] + ("…" if len(text) > length else "")


def _print_table(hits: list[dict[str, Any]]) -> None:
    if not hits:
        print("No results found across all namespaces.")
        return
    col_ns = max(len(h["_namespace"]) for h in hits)
    col_id = max(len(h["id"]) for h in hits)
    col_ns = max(col_ns, 2)
    col_id = max(col_id, 2)
    header = f"{'NS':<{col_ns}}  {'Score':>6}  {'ID':<{col_id}}  Summary"
    print(header)
    print("-" * len(header))
    for h in hits:
        ns = h["_namespace"]
        score = h.get("score", 0.0)
        doc_id = h["id"]
        preview = _preview(h.get("metadata", {}))
        print(f"{ns:<{col_ns}}  {score:>6.3f}  {doc_id:<{col_id}}  {preview}")


def _print_json(hits: list[dict[str, Any]], empty_ns: list[str]) -> None:
    out: dict[str, Any] = {
        "results": [
            {
                "namespace": h["_namespace"],
                "score": h.get("score", 0.0),
                "id": h["id"],
                "metadata": h.get("metadata", {}),
            }
            for h in hits
        ],
        "empty_namespaces": empty_ns,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cross_project_search",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--query", "-q", required=True, help="Search query text")
    parser.add_argument(
        "--top-k", "-k", type=int, default=3, metavar="N",
        help="Results per namespace (default: 3)",
    )
    parser.add_argument(
        "--namespaces", "-n", default="",
        help="Comma-separated namespace list (default: all known)",
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Machine-readable JSON output",
    )
    parser.add_argument(
        "--include-private", action="store_true",
        help="Override the privacy guard and ALSO search private namespaces",
    )
    args = parser.parse_args()

    namespaces: list[str] = (
        [ns.strip() for ns in args.namespaces.split(",") if ns.strip()]
        if args.namespaces
        else discover_namespaces()
    )

    # Privacy guard — never leak private (personal/legal) knowledge into a
    # cross-project search unless explicitly asked. Fail-safe if the map is gone.
    if not args.include_private:
        try:
            from visibility_guard import filter_namespaces  # noqa: E402
        except Exception:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            try:
                from visibility_guard import filter_namespaces  # noqa: E402
            except Exception:
                filter_namespaces = None
        if filter_namespaces is not None:
            namespaces, skipped = filter_namespaces(namespaces)
            if skipped and not args.as_json:
                print(f"  [guard] skipped {len(skipped)} private namespace(s): "
                      f"{', '.join(skipped)}", file=sys.stderr)

    if not args.as_json:
        eng = "bge-m3 local" if MEMORY_BACKEND == "local" else MODEL
        print(f"Embedding query… ({eng})", file=sys.stderr)

    all_hits: list[dict[str, Any]] = []
    empty_ns: list[str] = []

    if MEMORY_BACKEND == "local":
        lr = _local()
        try:
            vector = lr.local_embed(args.query, is_query=True)
        except Exception as exc:  # noqa: BLE001
            sys.exit(f"ERROR: Failed to embed query locally: {exc}")
        if not args.as_json:
            print(f"Querying {len(namespaces)} namespace(s), top-{args.top_k} each…\n", file=sys.stderr)
        for ns in namespaces:
            hits = lr.query_vec(vector, namespaces=[ns], topk=args.top_k)
            if hits:
                all_hits.extend({"id": h["id"], "score": h["score"],
                                 "metadata": h.get("meta", {}), "_namespace": ns} for h in hits)
            else:
                empty_ns.append(ns)
    else:
        try:
            vector = embed(args.query)
        except Exception as exc:  # noqa: BLE001
            sys.exit(f"ERROR: Failed to embed query: {exc}")
        if not args.as_json:
            print(f"Querying {len(namespaces)} namespace(s), top-{args.top_k} each…\n", file=sys.stderr)
        for ns in namespaces:
            hits = query_namespace(vector, ns, args.top_k)
            if hits:
                all_hits.extend(hits)
            else:
                empty_ns.append(ns)

    # Sort all results by score descending
    all_hits.sort(key=lambda h: h.get("score", 0.0), reverse=True)

    if args.as_json:
        _print_json(all_hits, empty_ns)
    else:
        _print_table(all_hits)
        if empty_ns:
            print(f"\nNo data: {', '.join(empty_ns)}", file=sys.stderr)


if __name__ == "__main__":
    main()
