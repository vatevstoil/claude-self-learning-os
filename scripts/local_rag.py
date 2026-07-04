#!/usr/bin/env python3
"""local_rag.py — fully local RAG: ingest + query, zero cloud, zero embedding cap.

Closed local loop on the RTX 4080:
    ingest  : chunk text -> bge-m3 embed (Ollama) -> store in sqlite
    query   : bge-m3 embed query -> cosine over stored vectors -> top-k

WHY: the Pinecone bulk importers burned the 10M/mo embedding cap on transcripts.
This keeps the heavy/multilingual research corpora entirely local. Cross-session
Claude memory stays on Pinecone hosted (see pinecone_embed_cap memory).

SELF-CONTAINED: same embedding model on BOTH sides (bge-m3, 1024-dim), so ingest
and query share one semantic space — no mixing, no silent-garbage failure mode.

Storage: sqlite at $LOCAL_RAG_DB (default ~/.claude/local_rag.db). Vectors are
L2-normalized float32 BLOBs, so cosine similarity == dot product. Query loads a
namespace's vectors into a numpy matrix and does one matmul — fast to ~100k chunks.

CLI:
    python local_rag.py index   <namespace> <glob> [glob...]   # ingest .md files
    python local_rag.py query   <text> [--ns a,b] [--topk 5] [--json]
    python local_rag.py stats
    python local_rag.py drop     <namespace>

Importable:
    from local_rag import upsert, upsert_vec, query
"""
from __future__ import annotations

import argparse
import array
import glob as globmod
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np

# Force UTF-8 stdout (Windows cp1251 cannot encode Cyrillic).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from embed_backend import local_embed, EXPECTED_DIM  # noqa: E402

DB_PATH = Path(os.environ.get("LOCAL_RAG_DB", str(Path.home() / ".claude" / "local_rag.db")))
CHUNK_TARGET = 1500   # chars
CHUNK_OVERLAP = 150

# Secret detection — the local sqlite store is PLAINTEXT on disk, so refuse to
# ingest content containing credentials (file-ingest reads arbitrary .md files).
_SECRET_RE = [re.compile(p) for p in (
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+",   # JWT
    r"pcsk_[A-Za-z0-9_-]{20,}", r"sk-ant-[A-Za-z0-9_-]{20,}",
    r"sk-proj-[A-Za-z0-9_-]{20,}", r"sk-[A-Za-z0-9]{20,}",
    r"ghp_[A-Za-z0-9]{36}", r"github_pat_[A-Za-z0-9_]{40,}",
    r"glpat-[A-Za-z0-9_-]{20,}", r"AKIA[0-9A-Z]{16}", r"AIza[0-9A-Za-z_-]{30,}",
    r"xox[abprs]-[A-Za-z0-9-]{10,}", r"-----BEGIN\s+(?:RSA\s+|OPENSSH\s+|EC\s+)?PRIVATE\s+KEY-----",
    r"(?i)\b(?:password|passwd|secret|api[_-]?key)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]",
)]


def contains_secret(text: str) -> bool:
    return bool(text) and any(p.search(text) for p in _SECRET_RE)


def _blocks_pii(text: str) -> bool:
    """True if `text` carries HIGH-confidence personal data (EGN / card / IBAN /
    sealed legal-case marker). Complements contains_secret, which covers only
    credentials. Lazy + tolerant: if pii_scanner is unavailable for any reason,
    fall back to allowing — the secret gate still runs and we never crash ingest."""
    if not text:
        return False
    try:
        import pii_scanner
        return pii_scanner.should_block_ingest(text)
    except Exception:
        return False


def backup(dest: str | None = None) -> str:
    """Consistent online snapshot of the DB via the sqlite backup API (safe even
    while other processes are writing). Returns the backup path."""
    d = Path(dest) if dest else (Path.home() / ".claude" / "backups" /
                                 f"local_rag-{DB_PATH.stat().st_mtime_ns}.db")
    d.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(DB_PATH))
    try:
        dst = sqlite3.connect(str(d))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return str(d)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    # Multiple processes write concurrently (Stop hook + SessionStart + manual +
    # dispatcher). WAL allows 1 writer + N readers; busy_timeout makes a blocked
    # writer wait instead of instantly raising "database is locked".
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute(
        """CREATE TABLE IF NOT EXISTS vectors (
            ns    TEXT NOT NULL,
            id    TEXT NOT NULL,
            text  TEXT NOT NULL,
            meta  TEXT NOT NULL,
            vec   BLOB NOT NULL,
            PRIMARY KEY (ns, id)
        )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_ns ON vectors(ns)")
    return c


def _normalize(vec: list) -> bytes:
    a = np.asarray(vec, dtype=np.float32)
    n = np.linalg.norm(a)
    if n > 0:
        a = a / n
    return a.astype(np.float32).tobytes()


def upsert_vec(ns: str, vid: str, vec: list, text: str, meta: dict | None = None) -> None:
    """Store an already-embedded vector (used by retrofitted Pinecone importers).

    The secret and PII gates apply to `text` here too — bulk importers call this
    directly (bypassing upsert()), so the guards must live at the storage layer.
    Triggering content is silently skipped (stderr note), never persisted.
    """
    if len(vec) != EXPECTED_DIM:
        raise ValueError(f"vec dim {len(vec)} != {EXPECTED_DIM}")
    if contains_secret(text or ""):
        print(f"  [secret] SKIP {ns}/{vid} — credential-like content not stored", file=sys.stderr)
        return
    if _blocks_pii(text or ""):
        print(f"  [pii] SKIP {ns}/{vid} — personal data (EGN/card/IBAN/legal) not stored", file=sys.stderr)
        return
    c = _conn()
    try:
        c.execute(
            "INSERT OR REPLACE INTO vectors (ns,id,text,meta,vec) VALUES (?,?,?,?,?)",
            (ns, vid, (text or "")[:2000], json.dumps(meta or {}, ensure_ascii=False), _normalize(vec)),
        )
        c.commit()
    finally:
        c.close()


def guard_reason(text: str) -> str | None:
    """Name of the ingest guard that blocks `text` ('secret' / 'pii'), or None.

    Single source of truth for upsert()'s gate, so callers (pinecone.py save) can
    report WHICH guard refused a save without ever echoing the text itself."""
    if contains_secret(text or ""):
        return "secret"
    if _blocks_pii(text or ""):
        return "pii"
    return None


def upsert(ns: str, vid: str, text: str, meta: dict | None = None) -> bool:
    """Embed `text` locally (bge-m3) and store it. Returns False (skipped) if the
    text contains a detected secret — never persists credentials to the plaintext
    sqlite store."""
    reason = guard_reason(text)
    if reason == "secret":
        print(f"  [secret] SKIP {ns}/{vid} — credential-like content not stored", file=sys.stderr)
        return False
    if reason == "pii":
        print(f"  [pii] SKIP {ns}/{vid} — personal data (EGN/card/IBAN/legal) not stored", file=sys.stderr)
        return False
    # timeout=6: Ollama slow → socket.timeout → PineconeError → cmd_save queues to
    # pending-saves.jsonl → exit 3. Hook completes in <8s instead of hanging 60s+.
    upsert_vec(ns, vid, local_embed(text, timeout=6), text, meta)
    return True


def upsert_many(rows: list) -> int:
    """Store many pre-embedded records in one transaction.
    rows = list of (ns, id, vec, text, meta_dict). Returns count STORED.

    Applies the SAME secret + PII gates as upsert()/upsert_vec() at the storage
    layer, so bulk paths (e.g. migrate_pinecone_to_local) cannot bypass the guard:
    a row whose text looks like a credential or carries blocked PII is skipped
    (never written to the plaintext store), not silently bulk-inserted."""
    safe_rows = []
    for (ns, vid, vec, text, meta) in rows:
        t = text or ""
        if contains_secret(t):
            print(f"  [secret] SKIP {ns}/{vid} — credential-like content not stored", file=sys.stderr)
            continue
        if _blocks_pii(t):
            print(f"  [pii] SKIP {ns}/{vid} — blocked PII not stored", file=sys.stderr)
            continue
        safe_rows.append((ns, vid, vec, text, meta))
    if not safe_rows:
        return 0
    c = _conn()
    try:
        c.executemany(
            "INSERT OR REPLACE INTO vectors (ns,id,text,meta,vec) VALUES (?,?,?,?,?)",
            [(ns, vid, (text or "")[:2000], json.dumps(meta or {}, ensure_ascii=False), _normalize(vec))
             for (ns, vid, vec, text, meta) in safe_rows],
        )
        c.commit()
        return len(safe_rows)
    finally:
        c.close()


def drop_ns(ns: str) -> int:
    c = _conn()
    try:
        n = c.execute("DELETE FROM vectors WHERE ns=?", (ns,)).rowcount
        c.commit()
        return n
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------
def query(text: str, namespaces: list | None = None, topk: int = 5,
          type_filter: list | None = None) -> list:
    """Embed `text` locally and return top-k matches across the given namespaces
    (or all). Optional type_filter = list of meta['type'] values to keep.
    Each result: {ns, id, score, text, meta}."""
    return query_vec(local_embed(text, is_query=True), namespaces, topk, type_filter)


def query_vec(vector: list, namespaces: list | None = None, topk: int = 5,
              type_filter: list | None = None) -> list:
    """Like query() but takes an already-computed embedding (embed once, query
    many namespaces). Returns the same {ns,id,score,text,meta} shape."""
    qv = np.asarray(vector, dtype=np.float32)
    qn = np.linalg.norm(qv)
    if qn > 0:
        qv = qv / qn
    c = _conn()
    try:
        # PHASE 1: pull only what the matmul needs (id+vec, +meta if filtering by
        # type) — NOT the big `text` column. For 30k-vector namespaces this avoids
        # transferring tens of MB of text per query.
        cols = "ns,id,meta,vec" if type_filter else "ns,id,vec"
        if namespaces:
            ph = ",".join("?" * len(namespaces))
            rows = c.execute(f"SELECT {cols} FROM vectors WHERE ns IN ({ph})", namespaces).fetchall()
        else:
            rows = c.execute(f"SELECT {cols} FROM vectors").fetchall()
        if type_filter:
            tset = set(type_filter)
            rows = [r for r in rows if (json.loads(r[2]) if r[2] else {}).get("type") in tset]
        if not rows:
            return []
        vi = 3 if type_filter else 2  # vec column index
        mat = np.frombuffer(b"".join(r[vi] for r in rows), dtype=np.float32).reshape(len(rows), -1)
        scores = mat @ qv  # cosine (both normalized)
        k = min(topk, len(rows))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        # PHASE 2: fetch text+meta only for the k winners.
        out = []
        for i in top:
            ns, vid = rows[int(i)][0], rows[int(i)][1]
            r2 = c.execute("SELECT text,meta FROM vectors WHERE ns=? AND id=?", (ns, vid)).fetchone()
            out.append({
                "ns": ns, "id": vid, "score": float(scores[int(i)]),
                "text": r2[0] if r2 else "",
                "meta": json.loads(r2[1]) if (r2 and r2[1]) else {},
            })
        return out
    finally:
        c.close()


def all_namespaces() -> list:
    """Return all namespace names with >=1 vector (largest first)."""
    c = _conn()
    try:
        rows = c.execute("SELECT ns, COUNT(*) FROM vectors GROUP BY ns ORDER BY 2 DESC").fetchall()
    finally:
        c.close()
    return [r[0] for r in rows]


def list_ids(ns: str, limit: int = 100) -> list:
    """Return up to `limit` ids in a namespace."""
    c = _conn()
    try:
        rows = c.execute("SELECT id FROM vectors WHERE ns=? LIMIT ?", (ns, limit)).fetchall()
    finally:
        c.close()
    return [r[0] for r in rows]


def existing_ids(ns: str) -> set:
    """Return the full set of ids already stored in a namespace (for resume)."""
    c = _conn()
    try:
        rows = c.execute("SELECT id FROM vectors WHERE ns=?", (ns,)).fetchall()
    finally:
        c.close()
    return {r[0] for r in rows}


def fetch(ns: str, ids: list) -> list:
    """Return [{id, values, metadata}] for the given ids (Pinecone-fetch shape)."""
    c = _conn()
    try:
        out = []
        for vid in ids:
            r = c.execute("SELECT id,meta,vec FROM vectors WHERE ns=? AND id=?", (ns, vid)).fetchone()
            if r:
                out.append({"id": r[0],
                            "values": np.frombuffer(r[2], dtype=np.float32).tolist(),
                            "metadata": json.loads(r[1]) if r[1] else {}})
        return out
    finally:
        c.close()


def fetch_all(ns: str) -> list:
    """Return [{id, values, metadata}] for every vector in a namespace."""
    c = _conn()
    try:
        rows = c.execute("SELECT id,meta,vec FROM vectors WHERE ns=?", (ns,)).fetchall()
    finally:
        c.close()
    return [{"id": r[0], "values": np.frombuffer(r[2], dtype=np.float32).tolist(),
             "metadata": json.loads(r[1]) if r[1] else {}} for r in rows]


def update_meta(ns: str, vid: str, meta: dict) -> int:
    """Replace the metadata JSON of one vector (vec/text untouched)."""
    c = _conn()
    try:
        n = c.execute("UPDATE vectors SET meta=? WHERE ns=? AND id=?",
                      (json.dumps(meta or {}, ensure_ascii=False), ns, vid)).rowcount
        c.commit()
        return n
    finally:
        c.close()


def delete(ns: str, vid: str) -> int:
    """Delete one vector by id. Returns rows removed."""
    c = _conn()
    try:
        n = c.execute("DELETE FROM vectors WHERE ns=? AND id=?", (ns, vid)).rowcount
        c.commit()
        return n
    finally:
        c.close()


def delete_expired(apply: bool = True) -> int:
    """Count (and, if apply, delete) vectors whose finite ttl has elapsed since
    meta['date']. ttl_days>=9999 or missing date = never expire. Returns count."""
    from datetime import date as _date
    c = _conn()
    n = 0
    try:
        rows = c.execute("SELECT ns,id,meta FROM vectors").fetchall()
        today = _date.today()
        for ns, vid, mj in rows:
            try:
                m = json.loads(mj) if mj else {}
                ttl = int(float(m.get("ttl_days", 9999)))
                d = m.get("date", "")
                if ttl >= 9999 or not d:
                    continue
                y, mo, da = (int(x) for x in d.split("-")[:3])
                if (today - _date(y, mo, da)).days > ttl:
                    if apply:
                        c.execute("DELETE FROM vectors WHERE ns=? AND id=?", (ns, vid))
                    n += 1
            except Exception:
                continue
        if apply:
            c.commit()
    finally:
        c.close()
    return n


# ---------------------------------------------------------------------------
# Chunking + file ingest
# ---------------------------------------------------------------------------
def split_into_chunks(text: str, target: int = CHUNK_TARGET, overlap: int = CHUNK_OVERLAP) -> list:
    text = text.strip()
    if len(text) <= target:
        return [text] if text else []
    paras = re.split(r"\n\s*\n", text)
    chunks, cur = [], ""
    for p in paras:
        p = p.strip()
        if not p:
            continue
        if len(cur) + len(p) + 2 <= target:
            cur = f"{cur}\n\n{p}" if cur else p
        else:
            if cur:
                chunks.append(cur)
                tail = cur[-overlap:] if overlap else ""
                cur = f"{tail}\n\n{p}" if tail else p
            else:  # single oversized paragraph: hard-split
                for j in range(0, len(p), target):
                    chunks.append(p[j:j + target])
                cur = ""
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def _canon(ns: str) -> str:
    """Canonical namespace (shared resolver) — keeps ingest + recall on the SAME
    ns. Falls back to the raw name if ns_util is unavailable."""
    try:
        from ns_util import sanitize_ns
        return sanitize_ns(ns)
    except Exception:
        return ns


def recanonicalize() -> dict:
    """One-time: rename every stored namespace to its canonical form (merges per
    ns_util aliases). Safe — ids differ across corpora so no PK collision."""
    c = _conn()
    moved = {}
    try:
        nss = [r[0] for r in c.execute("SELECT DISTINCT ns FROM vectors").fetchall()]
        for ns in nss:
            tgt = _canon(ns)
            if tgt != ns:
                c.execute("UPDATE OR REPLACE vectors SET ns=? WHERE ns=?", (tgt, ns))
                moved[ns] = tgt
        c.commit()
    finally:
        c.close()
    return moved


def index_files(ns: str, patterns: list) -> dict:
    """Ingest .md files: chunk → batch-embed → batch-store. Skips secret-bearing
    chunks. Batches embed+write (one Ollama call + one sqlite txn per batch) so a
    large file isn't 500 separate connections/commits."""
    from embed_backend import local_embed_batch
    ns = _canon(ns)  # ingest + recall must share one canonical namespace
    files: list[Path] = []
    for pat in patterns:
        files.extend(Path(f) for f in globmod.glob(pat, recursive=True))
    files = [f for f in files if f.is_file()]
    n_chunks, skipped, skipped_pii = 0, 0, 0
    BATCH = 32
    pending = []  # (id, text, meta)

    def _flush():
        nonlocal n_chunks
        if not pending:
            return
        vecs = local_embed_batch([t for (_, t, _) in pending])
        upsert_many([(ns, vid, vecs[j], txt, meta) for j, (vid, txt, meta) in enumerate(pending)])
        n_chunks += len(pending)
        pending.clear()
        print(f"  …{n_chunks} chunks", file=sys.stderr)

    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        for ci, chunk in enumerate(split_into_chunks(content)):
            if contains_secret(chunk):
                skipped += 1
                print(f"  [secret] SKIP {f.name}#{ci}", file=sys.stderr)
                continue
            if _blocks_pii(chunk):
                skipped_pii += 1
                print(f"  [pii] SKIP {f.name}#{ci} — personal data not stored", file=sys.stderr)
                continue
            h = hashlib.sha256(f"{f}|{ci}|{chunk[:64]}".encode("utf-8")).hexdigest()[:16]
            pending.append((h, chunk, {"text": chunk[:2000], "source": str(f), "chunk": ci}))
            if len(pending) >= BATCH:
                _flush()
    _flush()
    return {"files": len(files), "chunks": n_chunks,
            "skipped_secret": skipped, "skipped_pii": skipped_pii}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(prog="local_rag", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index", help="Ingest .md files into a namespace")
    pi.add_argument("namespace")
    pi.add_argument("globs", nargs="+")

    pq = sub.add_parser("query", help="Semantic search")
    pq.add_argument("text")
    pq.add_argument("--ns", default="", help="Comma-separated namespaces (default: all)")
    pq.add_argument("--topk", type=int, default=5)
    pq.add_argument("--json", dest="as_json", action="store_true")

    sub.add_parser("stats", help="Show namespaces + vector counts")
    pd = sub.add_parser("drop", help="Delete a namespace")
    pd.add_argument("namespace")

    args = p.parse_args()

    if args.cmd == "index":
        print(f"Indexing into ns='{args.namespace}' (db={DB_PATH})…", file=sys.stderr)
        res = index_files(args.namespace, args.globs)
        print(f"Done: {res['files']} files, {res['chunks']} chunks → ns='{args.namespace}'")
    elif args.cmd == "query":
        nss = [s.strip() for s in args.ns.split(",") if s.strip()] or None
        hits = query(args.text, namespaces=nss, topk=args.topk)
        if args.as_json:
            print(json.dumps(hits, ensure_ascii=False, indent=2))
        elif not hits:
            print("No results.")
        else:
            for h in hits:
                preview = h["text"].replace("\n", " ")[:80]
                print(f"[{h['score']:.3f}] [{h['ns']}] {h['id']}  {preview}")
    elif args.cmd == "stats":
        c = _conn()
        try:
            rows = c.execute("SELECT ns, COUNT(*) FROM vectors GROUP BY ns ORDER BY 2 DESC").fetchall()
        finally:
            c.close()
        if not rows:
            print(f"Empty (db={DB_PATH}).")
        else:
            total = sum(r[1] for r in rows)
            print(f"db={DB_PATH}  total={total} vectors across {len(rows)} ns")
            for ns, n in rows:
                print(f"  {n:>7}  {ns}")
    elif args.cmd == "drop":
        n = drop_ns(args.namespace)
        print(f"Dropped {n} vectors from ns='{args.namespace}'")


if __name__ == "__main__":
    main()
