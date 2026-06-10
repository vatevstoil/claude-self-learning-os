#!/usr/bin/env python3
"""Local embedding backend — RTX 4080 via Ollama, for high-volume bulk ingest.

WHY THIS EXISTS
    Bulk transcript / wiki imports consumed the entire 10M/month Pinecone
    embedding cap (multilingual-e5-large). Those workloads are large,
    multilingual, and quality-tolerant — ideal to run free & unlimited on the
    local GPU. Cross-session Claude memory (pinecone.py / auto_pinecone_save.py)
    is tiny and Bulgarian-sensitive — it STAYS on Pinecone hosted. Do NOT route
    that through here.

MODEL
    bge-m3 — multilingual (strong Bulgarian), 1024-dim (same width as
    multilingual-e5-large, so an upsert into a 1024-dim index won't error).

⚠️ INCOMPATIBLE SEMANTIC SPACES
    bge-m3 vectors and multilingual-e5-large vectors are NOT interchangeable
    even though both are 1024-dim. Querying an e5-built namespace with a bge-m3
    vector returns garbage — silently, no error (dimensions match, meaning does
    not). Therefore: anything embedded locally must live in its OWN index or
    namespace and be QUERIED with the same local backend. Re-embed legacy
    transcripts locally before querying them with bge-m3.

USAGE
    Set EMBED_BACKEND=ollama before running a bulk importer. The importer's
    embed function delegates here. Optional overrides:
        OLLAMA_HOST         (default http://localhost:11434)
        OLLAMA_EMBED_MODEL  (default bge-m3)

    Start the server once per boot:  ollama serve
    Pull the model once:             ollama pull bge-m3
"""
import json
import os
import sys
from urllib.request import Request, urlopen

def _normalize_ollama_host(v: str) -> str:
    """Resolve a CLIENT-connectable Ollama URL from OLLAMA_HOST.

    OLLAMA_HOST doubles as the SERVER bind address (e.g. "0.0.0.0" to expose to
    WSL2) — but 0.0.0.0 is NOT connectable from a client, so we map it to the
    loopback. Also accepts a bare host[:port] and fills scheme/port.
    """
    v = (v or "").strip() or "http://localhost:11434"
    if "://" not in v:
        v = "http://" + v                      # bare host[:port] → URL
    v = v.replace("://0.0.0.0", "://127.0.0.1")  # bind-all → loopback for client
    v = v.rstrip("/")
    tail = v.split("://", 1)[1]
    if ":" not in tail:                        # no port → default
        v = v + ":11434"
    return v


OLLAMA_HOST = _normalize_ollama_host(os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "bge-m3")
EXPECTED_DIM = 1024  # bge-m3 and multilingual-e5-large both emit 1024-dim
# Keep bge-m3 resident in VRAM to avoid the ~2.5s cold reload on sporadic queries
# (default Ollama eviction is 5min). Override via OLLAMA_KEEP_ALIVE env.
# Ollama accepts EITHER a number (seconds; -1 = forever) OR a duration string
# ("30m"). A string "-1" is rejected ("missing unit") — so coerce numerics to int.
_ka = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")
try:
    OLLAMA_KEEP_ALIVE = int(_ka)          # e.g. -1 (forever) or 1800 (seconds)
except (TypeError, ValueError):
    OLLAMA_KEEP_ALIVE = _ka               # duration string e.g. "30m"


def active_backend() -> str:
    """Return the configured backend: 'ollama' or 'pinecone' (default)."""
    return os.environ.get("EMBED_BACKEND", "pinecone").strip().lower()


_HEAL_TRIED = False


def _trigger_self_heal() -> None:
    """On embed failure, fire ollama_doctor in the background ONCE per process
    (non-blocking). It starts/restarts Ollama so the next op succeeds. Saves that
    fail meanwhile are queued by pinecone.py, so nothing is lost."""
    global _HEAL_TRIED
    if _HEAL_TRIED:
        return
    _HEAL_TRIED = True
    try:
        import subprocess as _sp
        from pathlib import Path as _P
        doctor = _P(__file__).resolve().parent / "ollama_doctor.py"
        flags = (0x00000008 | 0x00000200) if os.name == "nt" else 0
        _sp.Popen([sys.executable, str(doctor), "--ensure", "--quiet"],
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                  creationflags=flags, close_fds=True)
    except Exception:
        pass


def local_embed_batch(texts: list) -> list:
    """Embed many texts in one Ollama call (bge-m3 accepts an array input).
    Returns a list of vectors in the same order. Used for bulk migration."""
    if not texts:
        return []
    body = json.dumps({"model": OLLAMA_EMBED_MODEL, "input": texts,
                       "keep_alive": OLLAMA_KEEP_ALIVE}).encode("utf-8")
    r = Request(f"{OLLAMA_HOST}/api/embed", data=body, method="POST")
    r.add_header("Content-Type", "application/json")
    try:
        resp = json.loads(urlopen(r, timeout=60).read())
    except Exception as e:
        raise RuntimeError(f"Local batch embed failed ({OLLAMA_HOST}, {OLLAMA_EMBED_MODEL}): {e}") from e
    embs = resp.get("embeddings") or []
    if len(embs) != len(texts):
        raise RuntimeError(f"Batch embed count mismatch: {len(embs)} != {len(texts)}")
    for v in embs:
        if len(v) != EXPECTED_DIM:
            raise RuntimeError(f"Batch dim mismatch: {len(v)} != {EXPECTED_DIM}")
    return embs


def local_embed(text: str, is_query: bool = False, timeout: float = 120) -> list:
    """Embed `text` via local Ollama (bge-m3).

    `is_query` is accepted only for API parity with the Pinecone embed() — bge-m3
    needs no query/passage prefixes (unlike e5), so it is ignored.
    `timeout` (seconds) bounds the HTTP wait — pass a short value (e.g. 8) from
    latency-sensitive callers like session_start_brief so a down/hung Ollama can't
    block them; default 120 suits bulk/ingest.

    Raises RuntimeError on any failure. Importers must NOT silently fall back to
    Pinecone on error — that would re-burn the very cap this module avoids.
    """
    body = json.dumps({"model": OLLAMA_EMBED_MODEL, "input": text,
                       "keep_alive": OLLAMA_KEEP_ALIVE}).encode("utf-8")
    r = Request(f"{OLLAMA_HOST}/api/embed", data=body, method="POST")
    r.add_header("Content-Type", "application/json")
    try:
        resp = json.loads(urlopen(r, timeout=timeout).read())
    except Exception as e:
        _trigger_self_heal()  # fire-and-forget; fixes Ollama for the NEXT op
        raise RuntimeError(
            f"Local embed failed ({OLLAMA_HOST}, model={OLLAMA_EMBED_MODEL}): {e}. "
            f"Auto-repair triggered (ollama_doctor). Retry shortly; saves are queued."
        ) from e
    embs = resp.get("embeddings") or []
    if not embs or not embs[0]:
        raise RuntimeError(
            f"Ollama returned no embedding (model={OLLAMA_EMBED_MODEL})."
        )
    vec = embs[0]
    if len(vec) != EXPECTED_DIM:
        raise RuntimeError(
            f"Embedding dim mismatch: got {len(vec)}, expected {EXPECTED_DIM} "
            f"for {OLLAMA_EMBED_MODEL}. Wrong model? Don't mix into a 1024-dim index."
        )
    return vec


if __name__ == "__main__":
    # Smoke test: prints backend + a sample embedding dimension.
    import sys
    txt = sys.argv[1] if len(sys.argv) > 1 else "тест на български език"
    print(f"backend={active_backend()} host={OLLAMA_HOST} model={OLLAMA_EMBED_MODEL}")
    v = local_embed(txt)
    print(f"OK — dim={len(v)} first3={v[:3]}")
