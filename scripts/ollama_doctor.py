#!/usr/bin/env python3
"""ollama_doctor.py — health-check & auto-repair for the local memory backend.

The whole memory ecosystem (MEMORY_BACKEND=local) depends on Ollama serving the
bge-m3 embedding model. This tool detects and fixes the three failure modes:

  1. NOT RUNNING   (after reboot / crash)         → start `ollama serve`
  2. STUCK GPU     (serving but embeds hang/fail)  → kill + restart
  3. MODEL MISSING (bge-m3 not pulled)             → `ollama pull bge-m3`

Usage:
    python ollama_doctor.py            # --ensure: check + auto-repair (default)
    python ollama_doctor.py --check    # report only, exit 0=healthy / 1=unhealthy
    python ollama_doctor.py --quiet    # no stdout (for hooks)

ALWAYS exits 0 in --ensure mode unless it truly cannot heal (then 1). Safe to
wire into hooks/schedulers. Importable: `from ollama_doctor import ensure, healthy`.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
except Exception:
    pass

try:
    # OLLAMA_HOST doubles as the SERVER bind address (the user runs 0.0.0.0 to
    # expose Ollama to WSL2) — 0.0.0.0 is NOT client-connectable. Reading it
    # raw made serving() fail against a perfectly healthy server, which kept
    # the daily run DEGRADED for weeks. embed_backend already normalizes this.
    from embed_backend import _normalize_ollama_host
except Exception:
    def _normalize_ollama_host(v: str) -> str:
        v = (v or "").strip() or "http://localhost:11434"
        if "://" not in v:
            v = "http://" + v
        v = v.replace("://0.0.0.0", "://127.0.0.1").rstrip("/")
        if ":" not in v.split("://", 1)[1]:
            v = v + ":11434"
        return v

HOST = _normalize_ollama_host(os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "bge-m3")

_DIAG_PATH = Path.home() / ".claude" / "logs" / "ollama-doctor-last.json"


def _write_diag(ok: bool, stage: str = "", reason: str = "", detail: str = "",
                attempts: int = 0, path: Path | None = None) -> None:
    """Write structured diagnostics to file (atomic) and stderr on failure."""
    path = path or _DIAG_PATH  # resolved at call time so tests can repoint it
    ts = datetime.now(timezone.utc).isoformat()
    record: dict = {"ts": ts, "ok": ok, "stage": stage, "reason": reason,
                    "detail": detail, "attempts": attempts}
    if not ok:
        print(f"DIAG: stage={stage} reason={reason} detail={detail}", file=sys.stderr, flush=True)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _ollama_exe() -> str:
    """Locate ollama.exe (LOCALAPPDATA install is the Windows default)."""
    cand = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    if cand.exists():
        return str(cand)
    # PATH fallback
    from shutil import which
    return which("ollama") or "ollama"


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------
def serving(timeout: float = 3) -> bool:
    try:
        urlopen(Request(f"{HOST}/api/tags"), timeout=timeout).read()
        return True
    except Exception:
        return False


def model_present(timeout: float = 5) -> bool:
    try:
        d = json.loads(urlopen(Request(f"{HOST}/api/tags"), timeout=timeout).read())
        return any(MODEL in m.get("name", "") for m in d.get("models", []))
    except Exception:
        return False


def embed_ok(timeout: float = 20) -> bool:
    """The definitive health check — a real embed. Catches the stuck-GPU state
    that /api/tags (which still answers) would miss."""
    try:
        body = json.dumps({"model": MODEL, "input": "health"}).encode("utf-8")
        r = Request(f"{HOST}/api/embed", data=body, method="POST")
        r.add_header("Content-Type", "application/json")
        d = json.loads(urlopen(r, timeout=timeout).read())
        embs = d.get("embeddings") or []
        return bool(embs and embs[0])
    except Exception:
        return False


def healthy(embed_timeout: float = 20) -> bool:
    return serving() and embed_ok(embed_timeout)


# --------------------------------------------------------------------------
# Repairs
# --------------------------------------------------------------------------
_CREATE_NO_WINDOW = 0x08000000  # suppress the console flash when spawned from a hook
_DETACHED = _CREATE_NO_WINDOW | 0x00000008 | 0x00000200  # + DETACHED_PROCESS | NEW_PROCESS_GROUP


def _start_serve() -> tuple[bool, str]:
    """Spawn `ollama serve` detached. Returns (ok, err) — the spawn error must
    NEVER be swallowed: a DENY-Execute ACE once appeared on ollama.exe and the
    silent failure here made every repair report a misleading
    'connection_refused' for days."""
    exe = _ollama_exe()
    try:
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=_DETACHED if os.name == "nt" else 0,
                         close_fds=True)
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e} (exe={exe})"


def _access_denied(err: str) -> bool:
    return ("WinError 5" in err or "Access is denied" in err
            or "PermissionError" in err)


def _repair_exec_acl(log=lambda m: None) -> bool:
    """Remove DENY ACEs for the current user from the Ollama executables.
    Origin of the ACE is unknown (appeared around an Ollama self-update); the
    user holds WRITE_DAC on the files so no elevation is needed. Returns True
    if icacls succeeded on every existing exe."""
    if os.name != "nt":
        return False
    user = os.environ.get("USERNAME", "")
    if not user:
        return False
    exe_dir = Path(_ollama_exe()).parent
    ok = True
    for name in ("ollama.exe", "ollama app.exe"):
        target = exe_dir / name
        if not target.exists():
            continue
        try:
            r = subprocess.run(["icacls", str(target), "/remove:d", user],
                               capture_output=True, timeout=30,
                               creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0)
            ok = ok and r.returncode == 0
        except Exception:
            ok = False
    log(f"ACL repair (remove DENY for {user}): {'ok' if ok else 'FAILED'}")
    return ok


def _kill() -> None:
    for name in ("ollama.exe", "ollama app.exe"):
        try:
            subprocess.run(["taskkill", "/F", "/IM", name],
                           capture_output=True, timeout=15,
                           creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0)
        except Exception:
            pass


def _wait_serving(secs: int = 25) -> bool:
    for _ in range(secs * 2):
        if serving(2):
            return True
        time.sleep(0.5)
    return False


def _pull_model(log) -> None:
    exe = _ollama_exe()
    log(f"pulling {MODEL} (one-time)…")
    try:
        subprocess.run([exe, "pull", MODEL], capture_output=True, timeout=900,
                       creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0)
    except Exception as e:
        log(f"pull failed: {e}")


_LOCK = Path.home() / ".claude" / "ollama_doctor.lock"
_LOCK_TTL = 60  # seconds


def ensure(log=lambda m: None) -> bool:
    """Bring Ollama+bge-m3 to a healthy state. Returns True if healthy after."""
    # Fast path — already healthy.
    if serving() and embed_ok():
        log("healthy (already serving + embed OK)")
        _write_diag(ok=True, stage="embed", reason="", detail="fast-path healthy", attempts=1)
        return True

    # SINGLE-FLIGHT lock: many short-lived processes (each `pinecone.py query`,
    # the SessionStart hook, embed_backend self-heal) could otherwise spawn
    # concurrent doctors that kill/restart each other in a loop. If a fresh lock
    # exists, another doctor is already repairing — defer to it.
    try:
        if _LOCK.exists():
            import time as _t
            age = _t.time() - _LOCK.stat().st_mtime
            if age < _LOCK_TTL:
                log("another doctor is already repairing (lock fresh) — skipping")
                # wait briefly for the other to finish, then report its outcome
                for _ in range(20):
                    if healthy(5):
                        return True
                    time.sleep(1)
                result = healthy(5)
                if not result:
                    _write_diag(ok=False, stage="server", reason="timeout",
                                detail="deferred to other doctor — still unhealthy", attempts=0)
                return result
        _LOCK.parent.mkdir(parents=True, exist_ok=True)
        _LOCK.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    try:
        # 1) Not running → start.
        if not serving():
            log("not serving → starting `ollama serve`…")
            started, err = _start_serve()
            attempts = 1
            if not started and _access_denied(err):
                # Known failure mode: DENY-Execute ACE on the exe → repair + retry once.
                log(f"spawn failed ({err}) → repairing exec ACL and retrying…")
                _repair_exec_acl(log)
                started, err = _start_serve()
                attempts = 2
            if not started:
                # Fail fast — no point waiting 25s for a process that never spawned.
                _write_diag(ok=False, stage="spawn",
                            reason="access_denied" if _access_denied(err) else "spawn_failed",
                            detail=err, attempts=attempts)
                log(f"RESULT: cannot spawn ollama serve ({err})")
                return False
            if not _wait_serving(25):
                _write_diag(ok=False, stage="server", reason="connection_refused",
                            detail="ollama serve started but did not respond within 25s", attempts=1)
                return False

        # 2) Model missing → pull.
        if serving() and not model_present():
            _write_diag(ok=False, stage="model", reason="model_missing",
                        detail=f"model {MODEL!r} not in /api/tags; pulling now", attempts=1)
            _pull_model(log)

        # 3) Serving but embed broken → confirm it's really stuck (re-check once
        #    after a pause; avoids killing the user's `ollama run` on a transient
        #    slow first-load) then hard restart.
        if serving() and not embed_ok():
            time.sleep(3)
            if not embed_ok():
                log("serving but embed FAILS twice (stuck) → restart…")
                _kill()
                time.sleep(2)
                started, err = _start_serve()
                if not started and _access_denied(err):
                    _repair_exec_acl(log)
                    _start_serve()
                _wait_serving(25)

        # After a restart bge-m3 needs time to load into GPU — retry with increasing
        # timeouts before giving up (cold-start can take 30-60s on first load).
        for _attempt, _timeout in enumerate((20, 30, 45), 1):
            ok = healthy(_timeout)
            if ok:
                log(f"RESULT: healthy ✓ (attempt {_attempt})")
                _write_diag(ok=True, stage="embed", reason="", detail="healthy after repair",
                            attempts=_attempt)
                return True
            if _attempt < 3:
                log(f"post-repair check {_attempt} failed (embed timeout {_timeout}s) — retrying…")
                time.sleep(5)

        # Determine which stage failed for the final diagnostic
        _srv = serving()
        if not _srv:
            _stage, _reason, _detail = "server", "connection_refused", "not responding after restart"
        elif not model_present():
            _stage, _reason, _detail = "model", "model_missing", f"{MODEL!r} absent after pull attempt"
        else:
            _stage, _reason, _detail = "embed", "embed_failed", "embed returned empty/error after 3 attempts (20/30/45s)"
        _write_diag(ok=False, stage=_stage, reason=_reason, detail=_detail, attempts=3)
        log("RESULT: STILL UNHEALTHY ✗")
        return False
    finally:
        try:
            _LOCK.unlink()
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--ensure", action="store_true", help="check + auto-repair (default)")
    g.add_argument("--check", action="store_true", help="report only; exit 1 if unhealthy")
    ap.add_argument("--quiet", action="store_true", help="suppress stdout (for hooks)")
    ap.add_argument("--background", action="store_true",
                    help="re-spawn self detached and exit immediately (non-blocking, for SessionStart hook)")
    args = ap.parse_args()

    # Fire-and-forget: detach a copy and return instantly so the hook never blocks
    # session start. The detached child does the real (possibly slow) repair.
    if args.background:
        try:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--ensure", "--quiet"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=_DETACHED if os.name == "nt" else 0, close_fds=True)
        except Exception:
            pass
        sys.exit(0)

    log = (lambda m: None) if args.quiet else (lambda m: print(f"[ollama-doctor] {m}", flush=True))

    if args.check:
        srv = serving()
        mdl = model_present() if srv else False
        emb = embed_ok() if srv else False
        log(f"serving={srv} model={mdl} embed_ok={emb}")
        ok = srv and emb
        if not ok:
            if not srv:
                _write_diag(ok=False, stage="server", reason="connection_refused",
                            detail="ollama not responding on /api/tags", attempts=1)
            elif not mdl:
                _write_diag(ok=False, stage="model", reason="model_missing",
                            detail=f"{MODEL!r} not found in /api/tags", attempts=1)
            else:
                _write_diag(ok=False, stage="embed", reason="embed_failed",
                            detail="embed returned empty/error", attempts=1)
        else:
            _write_diag(ok=True, stage="embed", reason="", detail="check passed", attempts=1)
        sys.exit(0 if ok else 1)

    # default: ensure (never hard-fail a hook — exit 0 unless unrecoverable)
    ok = ensure(log)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
