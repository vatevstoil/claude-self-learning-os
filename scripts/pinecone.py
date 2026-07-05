#!/usr/bin/env python3
"""Memory CLI — save / query cross-session recall.

Routes to the active backend (MEMORY_BACKEND env var):
  - "local"   → bge-m3 + SQLite via local_rag (default, no quota)
  - "pinecone" → Pinecone cloud index (dormant fallback)

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

# Force UTF-8 stdout — Windows default cp1251 cannot encode Cyrillic/emojis
# in cmd_query/cmd_list output. Importers (hooks, dispatcher) silently swallow
# UnicodeEncodeError → blind spot; CLI users see crashes mid-response.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    # stderr too: guard SKIP/BLOCKED notes go to stderr and are captured by
    # promote_to_pinecone (utf-8 pipe) — locale-encoded bytes would mojibake.
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


class PineconeError(Exception):
    """Raised by _req() on any network/API failure.

    Critical: must inherit from Exception (not BaseException). Previously
    _req() called sys.exit() which raises SystemExit (BaseException), bypassing
    `except Exception` in callers like query_and_track() → killed the calling
    process (hooks, dispatcher) on transient network errors.
    """


class QuotaExhaustedError(PineconeError):
    """Raised when the embedding API returns 429 RESOURCE_EXHAUSTED — the monthly
    token cap is hit. Distinct from a transient/generic error so callers can QUEUE
    the save locally and replay it after the quota resets, instead of silently
    losing the learning. Stays a PineconeError so existing `except PineconeError`
    paths (query_and_track) still degrade gracefully (recall → []).
    """


class GuardBlockedError(Exception):
    """Raised when the local store's secret/PII guard refuses the text.

    Deliberately NOT a PineconeError: guard-refused content must never enter the
    plaintext fallback queue (the queue would leak exactly what the guard blocks)
    and must never be retried — it fails loudly instead (cmd_save → exit 4).
    str(exc) is the guard name ('secret' / 'pii'), never the text itself.
    """


# Failed-save fallback queue: when embedding quota is exhausted, saves are
# appended here (never lost) and replayed by `pinecone.py replay-queue` — wired
# into the daily dispatcher so it self-drains once the monthly quota resets.
PENDING_QUEUE = Path.home() / ".claude" / "logs" / "pending-saves.jsonl"


def _is_quota_error(code, body) -> bool:
    """True only for the monthly embedding-quota 429 (RESOURCE_EXHAUSTED /
    'token limit'), NOT a plain transient rate-limit 429 — those should retry,
    not queue-and-wait-for-month-reset."""
    if code != 429:
        return False
    b = (body or "").lower()
    return ("resource_exhausted" in b) or ("token limit" in b) or (
        "monthly" in b and "limit" in b)


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

# Backend: "local" (bge-m3 + sqlite via local_rag) is the DEFAULT — fully
# decoupled from Pinecone, no embedding cap. Set MEMORY_BACKEND=pinecone to use
# the hosted index (dormant fallback). All callers (auto_pinecone_save Stop hook,
# /recall, session_start_brief) inherit this transparently.
MEMORY_BACKEND = os.environ.get("MEMORY_BACKEND", "local").strip().lower()


def _local():
    sys.path.insert(0, str(Path(__file__).parent))
    import local_rag
    return local_rag


def _to_match(h: dict) -> dict:
    """Adapt a local_rag hit -> Pinecone-style match dict (id/score/metadata)."""
    return {"id": h["id"], "score": h["score"],
            "metadata": h.get("meta", {}), "_namespace": h.get("ns", "")}


def _req(url, body=None, method="GET"):
    data = json.dumps(body).encode() if body else None
    r = Request(url, data=data, method=method)
    r.add_header("Api-Key", API_KEY)
    r.add_header("Content-Type", "application/json")
    r.add_header("X-Pinecone-API-Version", "2025-04")
    try:
        return json.loads(urlopen(r, timeout=30).read())
    except HTTPError as e:
        body = e.read().decode(errors="replace")
        if _is_quota_error(e.code, body):
            raise QuotaExhaustedError(f"HTTP {e.code}: {body[:200]}") from e
        raise PineconeError(f"HTTP {e.code}: {body[:200]}") from e
    except URLError as e:
        raise PineconeError(f"Network error: {e.reason}") from e
    except Exception as e:
        raise PineconeError(f"Unexpected error in request: {e}") from e


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


def _do_save(namespace, entry_id, text, meta: dict) -> None:
    """Embed `text` and upsert one vector. Raises QuotaExhaustedError on quota,
    PineconeError on other API failure. Shared by cmd_save and replay_queue."""
    if MEMORY_BACKEND == "local":
        lr = _local()
        try:
            stored = lr.upsert(namespace, entry_id, text, meta)
        except Exception as e:  # local store/embed failure → PineconeError so
            raise PineconeError(f"local save failed: {e}") from e  # callers degrade
        if not stored:
            # upsert() returned False = a guard refused the text. Silent exit 0
            # here made promote_to_pinecone print a false "Promoted" while the
            # row never reached local_rag.db.
            try:
                reason = lr.guard_reason(text) or "unknown"
            except Exception:
                reason = "unknown"
            raise GuardBlockedError(reason)
        return
    # Storage-layer guard on the Pinecone path too (design intent: see
    # local_rag.guard_reason docstring "so callers (pinecone.py save) can report
    # WHICH guard refused"). Without this, MEMORY_BACKEND=pinecone embeds+upserts
    # credential/PII content straight to the cloud, bypassing the guard the local
    # backend enforces — a privacy fail-open.
    reason = None
    try:
        from local_rag import guard_reason
        reason = guard_reason(text)
    except Exception:
        try:
            from pii_scanner import should_block_ingest  # fail-closed fallback
            reason = "pii" if should_block_ingest(text) else None
        except Exception:
            reason = None
    if reason:
        raise GuardBlockedError(reason)
    vec = embed(text, is_query=False)
    _req(
        f"https://{HOST}/vectors/upsert",
        {"vectors": [{"id": entry_id, "values": vec, "metadata": meta}],
         "namespace": namespace},
        method="POST",
    )


def _queue_pending(rec: dict) -> None:
    """Append one failed save to the local fallback queue (never lose a learning).

    EXCEPT private namespaces: their content must never sit in a plaintext queue
    file. A failed private save is dropped (accepted loss) — these are session
    wrap-ups, not irreplaceable learnings, and privacy outranks durability.
    """
    ns = rec.get("namespace", "")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from visibility_guard import private_namespaces
        if ns in private_namespaces():
            print(f"[pinecone] private namespace '{ns}' NOT queued — plaintext-leak guard",
                  file=sys.stderr)
            return
    except Exception:
        pass  # guard is best-effort; never block a legitimate queue write
    PENDING_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with PENDING_QUEUE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def replay_queue() -> dict:
    """Re-attempt every queued save. Drains the queue on success; on quota again
    it STOPS (no point hammering an exhausted month) and keeps the rest. Other
    transient errors keep the item for the next run. Returns {replayed, pending}."""
    # Atomically CLAIM the queue: rename it aside so concurrent _queue_pending()
    # appends land in a fresh PENDING_QUEUE and can't be clobbered by our rewrite
    # (fixes the read-modify-write race vs a session ending mid-replay).
    proc = PENDING_QUEUE.with_suffix(".processing")
    if not PENDING_QUEUE.exists() and not proc.exists():
        return {"replayed": 0, "pending": 0}
    try:
        PENDING_QUEUE.rename(proc)        # atomic on same volume
    except FileExistsError:
        pass                              # leftover from a crashed run — process it
    except FileNotFoundError:
        if not proc.exists():
            return {"replayed": 0, "pending": 0}
    rows = [l for l in proc.read_text(encoding="utf-8").splitlines() if l.strip()]
    # Warm bge-m3 before draining. Per-item saves embed with a hook-safe 6s timeout
    # (local_rag.upsert), but a COLD model loads in ~11.5s → without a warm-up every
    # item times out and re-queues forever. Replay is a background task (not a hook),
    # so pre-load the model once with a generous timeout. Best-effort: on failure,
    # drain anyway (items that still time out simply retry next run, as before).
    if rows and MEMORY_BACKEND == "local":
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from embed_backend import local_embed as _warm_embed
            _warm_embed("warmup", timeout=30)
        except Exception:
            pass
    replayed = 0
    remaining: list[str] = []
    stopped = False
    for line in rows:
        if stopped:
            remaining.append(line)
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue  # drop malformed line
        try:
            _do_save(rec["namespace"], rec["id"], rec["text"], rec.get("meta") or {})
            replayed += 1
        except GuardBlockedError:
            continue  # guard-refused: drop — retrying can never succeed, and the
            # line must not keep cycling through the plaintext queue file
        except QuotaExhaustedError:
            stopped = True
            remaining.append(line)
        except Exception:
            remaining.append(line)  # transient — retry next run, keep trying others
    # Re-queue failures by APPENDING (never overwrite — preserves concurrent adds).
    for line in remaining:
        try:
            _queue_pending(json.loads(line))
        except Exception:
            pass
    try:
        proc.unlink()
    except Exception:
        pass
    return {"replayed": replayed, "pending": len(remaining)}


def cmd_save(args):
    meta = {"text": args.text[:500]}
    if args.meta:
        for kv in args.meta.split(","):
            k, v = kv.split("=", 1)
            meta[k.strip()] = v.strip()
    try:
        _do_save(args.namespace, args.id, args.text, meta)
    except GuardBlockedError as e:
        # Guard-refused content: fail LOUDLY (exit 4), name the guard, never
        # queue it, never echo the text. Exit codes: 0=saved, 3=queued, 4=blocked.
        label = {
            "secret": "secret-guard (credential-like content)",
            "pii": "pii-guard (EGN/card/IBAN/legal/private-marker)",
        }.get(str(e), f"ingest guard ({e})")
        print(f"BLOCKED by {label}: ns={args.namespace} id={args.id} — NOT saved",
              file=sys.stderr)
        sys.exit(4)
    except QuotaExhaustedError:
        # Quota exhausted — queue locally instead of losing the learning. Exit 3
        # signals callers (auto_pinecone_save) that it was QUEUED, not lost.
        _queue_pending({"namespace": args.namespace, "id": args.id,
                        "text": args.text, "meta": meta})
        print(f"QUEUED (memory backend quota exhausted — replays after heal): ns={args.namespace} "
              f"id={args.id} → {PENDING_QUEUE.name} (replay: pinecone.py replay-queue)")
        sys.exit(3)
    except PineconeError as e:
        # LOCAL backend: any save failure (Ollama down/cold) must NOT lose the
        # learning — queue it for replay (drained by `pinecone.py replay-queue`,
        # wired into the daily dispatcher). Mirrors the quota-queue behavior.
        if MEMORY_BACKEND == "local":
            _queue_pending({"namespace": args.namespace, "id": args.id,
                            "text": args.text, "meta": meta})
            print(f"QUEUED (memory backend unavailable: {e}): ns={args.namespace} "
                  f"id={args.id} → {PENDING_QUEUE.name} (replay: pinecone.py replay-queue)")
            sys.exit(3)
        raise
    print(f"Saved: id={args.id} ns={args.namespace} (1 upserted)")


def cmd_replay(args):
    res = replay_queue()
    if not res["replayed"] and not res["pending"]:
        print("replay-queue: nothing pending.")
        return
    print(f"replay-queue: replayed {res['replayed']}, still pending {res['pending']}.")
    if res["pending"]:
        print("  (memory backend may still be unavailable — retries on next run)")


def cmd_query(args):
    types = [t.strip() for t in args.type.split(",") if t.strip()] if args.type else None
    if MEMORY_BACKEND == "local":
        hits = _local().query(args.text, namespaces=[args.namespace],
                              topk=args.topk, type_filter=types)
        matches = [_to_match(h) for h in hits]
    else:
        vec = embed(args.text, is_query=True)
        body = {
            "vector": vec,
            "topK": args.topk,
            "namespace": args.namespace,
            "includeMetadata": True,
        }
        if types:
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
        types = ([t.strip() for t in type_filter.split(",") if t.strip()]
                 if type_filter else None)
        if MEMORY_BACKEND == "local":
            hits = _local().query(text, namespaces=[namespace], topk=topk, type_filter=types)
            matches = [_to_match(h) for h in hits]
        else:
            vec = embed(text, is_query=True)
            body: dict = {
                "vector": vec,
                "topK": topk,
                "namespace": namespace,
                "includeMetadata": True,
            }
            if types:
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
    if MEMORY_BACKEND == "local":
        ids = _local().list_ids(args.namespace, limit=100)
        print(f"Namespace '{args.namespace}': {len(ids)} vectors")
        for vid in ids[:20]:
            print(f"  - {vid}")
        return
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
    if MEMORY_BACKEND == "local":
        n = _local().delete(args.namespace, args.id)
        print(f"Deleted: id={args.id} ns={args.namespace} (local, {n} removed)")
        return
    resp = _req(
        f"https://{HOST}/vectors/delete",
        {"ids": [args.id], "namespace": args.namespace},
        method="POST",
    )
    print(f"Deleted: id={args.id} ns={args.namespace}")


def main():
    if MEMORY_BACKEND != "local" and (not API_KEY or not HOST):
        sys.exit("ERROR: PINECONE_API_KEY or PINECONE_INDEX_HOST not set in ~/.claude/.env")
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

    prp = sub.add_parser("replay-queue",
                         help="Replay saves queued while embedding quota was exhausted")
    prp.set_defaults(func=cmd_replay)

    args = p.parse_args()
    # Memory namespaces must be ASCII — transliterate Cyrillic so saves & recall
    # always hit the same namespace (e.g. "Клошар" -> "Kloshar").
    if hasattr(args, "namespace"):
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from ns_util import sanitize_ns
            args.namespace = sanitize_ns(args.namespace)
        except Exception:
            pass
    try:
        args.func(args)
    except PineconeError as e:
        # Only convert to process exit when used as CLI; importers catch
        # PineconeError directly without losing control of the process.
        sys.exit(str(e))


if __name__ == "__main__":
    main()
