#!/usr/bin/env python3
"""Pinecone memory CLI — save / query cross-session recall.

Usage:
    python pinecone.py save <namespace> <id> <text> [--meta key=val,key=val]
    python pinecone.py query <namespace> <text> [--topk 5]
    python pinecone.py list <namespace>
    python pinecone.py delete <namespace> <id>

Auto-loads credentials from ~/.claude/.env.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# Load .env
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

if not API_KEY or not HOST:
    sys.exit("ERROR: PINECONE_API_KEY or PINECONE_INDEX_HOST not set in ~/.claude/.env")


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


def embed(text, is_query=False):
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


def cmd_save(args):
    meta = {"text": args.text[:500]}
    if args.meta:
        for kv in args.meta.split(","):
            k, v = kv.split("=", 1)
            meta[k.strip()] = v.strip()
    vec = embed(args.text, is_query=False)
    resp = _req(
        f"https://{HOST}/vectors/upsert",
        {
            "vectors": [{"id": args.id, "values": vec, "metadata": meta}],
            "namespace": args.namespace,
        },
        method="POST",
    )
    print(f"Saved: id={args.id} ns={args.namespace} ({resp.get('upsertedCount')} upserted)")


def cmd_query(args):
    vec = embed(args.text, is_query=True)
    body = {
        "vector": vec,
        "topK": args.topk,
        "namespace": args.namespace,
        "includeMetadata": True,
    }
    if args.type:
        types = [t.strip() for t in args.type.split(",") if t.strip()]
        body["filter"] = {"type": {"$in": types}}
    resp = _req(
        f"https://{HOST}/query",
        body,
        method="POST",
    )
    matches = resp.get("matches", [])
    if not matches:
        print(f"No matches in ns={args.namespace}")
        return
    for m in matches:
        meta = m.get("metadata", {})
        type_label = meta.get("type", "?")
        print(f"[{m['score']:.3f}] [{type_label}] {m['id']}")
        txt = meta.get("text", "")
        if txt:
            print(f"   {txt[:150]}")

    # Hebbian recall tracking
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from recall_tracker import log_hits
        log_hits(args.namespace, matches)
    except Exception:
        pass


def query_and_track(
    namespace: str,
    text: str,
    topk: int = 7,
    type_filter: str | None = None,
) -> list[dict]:
    """Query Pinecone and automatically track recall hits for Hebbian reinforcement.

    Importable alternative to ``cmd_query`` for use in Python scripts that need
    recall tracking without going through the CLI.  All hits are logged to
    ``recall-tracker.json`` exactly as the CLI does.

    Args:
        namespace: Pinecone namespace to query.
        text: Query text (will be embedded).
        topk: Number of results to return (default 7).
        type_filter: Optional comma-separated type filter (e.g. ``"learning,pattern"``).

    Returns:
        List of match dicts from Pinecone (each has ``id``, ``score``, ``metadata``).
        Returns empty list on any error — never raises.
    """
    try:
        vec = embed(text, is_query=True)
        body: dict = {
            "vector": vec,
            "topK": topk,
            "namespace": namespace,
            "includeMetadata": True,
        }
        if type_filter:
            types = [t.strip() for t in type_filter.split(",") if t.strip()]
            body["filter"] = {"type": {"$in": types}}
        resp = _req(f"https://{HOST}/query", body, method="POST")
        matches = resp.get("matches", [])
        if matches:
            try:
                import sys as _sys
                _sys.path.insert(0, str(Path(__file__).parent))
                from recall_tracker import log_hits
                log_hits(namespace, matches)
            except Exception:
                pass
        return matches
    except Exception:
        return []


def cmd_list(args):
    ns_encoded = quote(args.namespace, safe="")
    resp = _req(
        f"https://{HOST}/vectors/list?namespace={ns_encoded}&limit=100",
        method="GET",
    )
    vecs = resp.get("vectors", [])
    print(f"Namespace '{args.namespace}': {len(vecs)} vectors")
    for v in vecs[:20]:
        print(f"  - {v['id']}")


def cmd_delete(args):
    resp = _req(
        f"https://{HOST}/vectors/delete",
        {"ids": [args.id], "namespace": args.namespace},
        method="POST",
    )
    print(f"Deleted: id={args.id} ns={args.namespace}")


def main():
    p = argparse.ArgumentParser(prog="pinecone", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("save")
    ps.add_argument("namespace")
    ps.add_argument("id")
    ps.add_argument("text")
    ps.add_argument("--meta", default="")
    ps.set_defaults(func=cmd_save)

    pq = sub.add_parser("query")
    pq.add_argument("namespace")
    pq.add_argument("text")
    pq.add_argument("--topk", type=int, default=5)
    pq.add_argument("--type", default="",
                    help="Comma-separated types to filter (e.g., gotcha,decision)")
    pq.set_defaults(func=cmd_query)

    pl = sub.add_parser("list")
    pl.add_argument("namespace")
    pl.set_defaults(func=cmd_list)

    pd = sub.add_parser("delete")
    pd.add_argument("namespace")
    pd.add_argument("id")
    pd.set_defaults(func=cmd_delete)

    args = p.parse_args()
    # Pinecone namespaces must be ASCII — transliterate Cyrillic so saves & recall
    # always hit the same namespace (e.g. "Клошар" -> "Kloshar").
    if hasattr(args, "namespace"):
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from ns_util import sanitize_ns
            args.namespace = sanitize_ns(args.namespace)
        except Exception:
            pass
    args.func(args)


if __name__ == "__main__":
    main()
