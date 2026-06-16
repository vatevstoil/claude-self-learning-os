"""Suite-wide safety net.

Tests must never reach a live endpoint — they mock the network. If one slips
through (e.g. an un-stubbed path reaching the local Ollama or a Pinecone host),
a bounded default socket timeout makes it fail fast instead of stalling. The
per-test timeout in pytest.ini (thread method) is the harder backstop; this is
the gentler first line so an accidental call surfaces in seconds, not at 60s.
"""
from __future__ import annotations

import logging
import socket
import sys
from pathlib import Path

import pytest

# 10s is far above any legitimate (there are none) in-test socket op, yet well
# under the pytest-timeout ceiling — an accidental real call fails, not hangs.
socket.setdefaulttimeout(10)

# ---------------------------------------------------------------------------
# Canonical real log path — must match automation_dispatcher.LOG_FILE exactly.
# We resolve it here once so the fixture never imports the module prematurely.
# ---------------------------------------------------------------------------
_REAL_LOG_FILE: Path = Path.home() / ".claude" / "logs" / "automation.log"


def _iter_all_loggers() -> list[logging.Logger]:
    """Yield root logger + every named logger currently registered."""
    loggers: list[logging.Logger] = [logging.getLogger()]  # root
    manager = logging.Logger.manager
    for name, obj in list(manager.loggerDict.items()):
        # loggerDict values can be PlaceHolder instances (not real loggers)
        if isinstance(obj, logging.Logger):
            loggers.append(obj)
    return loggers


def _find_live_handlers() -> list[tuple[logging.Logger, logging.FileHandler]]:
    """Return (logger, handler) pairs whose FileHandler targets the real log."""
    hits: list[tuple[logging.Logger, logging.FileHandler]] = []
    real = str(_REAL_LOG_FILE.resolve())
    for lgr in _iter_all_loggers():
        for h in list(lgr.handlers):
            if isinstance(h, logging.FileHandler):
                try:
                    target = str(Path(h.baseFilename).resolve())
                except Exception:
                    continue
                if target == real:
                    hits.append((lgr, h))
    return hits


@pytest.fixture(autouse=True)
def _isolate_dispatcher_log(tmp_path: Path) -> "Iterator[Path]":
    """Redirect every FileHandler targeting the real automation.log.

    Two-layer strategy (handles both pre-existing AND newly created handlers):

    Layer 1 — pre-existing: at fixture setup, scan all loggers and detach any
    FileHandler whose resolved baseFilename is the real log, replacing it with
    a redirect handler writing to a tmp file.

    Layer 2 — future handlers: monkey-patch ``logging.Logger.addHandler`` for
    the duration of the test.  Any call that tries to attach a FileHandler
    pointing to the real log is silently intercepted — the real handler is
    dropped and the redirect handler is attached instead.  This catches the
    re-import pattern (``del sys.modules[...]; import automation_dispatcher``)
    where module-level code runs ``log.addHandler(_handler)`` during the test
    body.

    Idempotent: if no matching handlers exist (module not imported), both
    layers are no-ops.  Restored cleanly on teardown.
    """
    from typing import Iterator

    real = str(_REAL_LOG_FILE.resolve())
    tmp_log = tmp_path / "dispatcher_captured.log"

    redirect_handler = logging.FileHandler(str(tmp_log), encoding="utf-8")
    redirect_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    redirect_handler.setLevel(logging.DEBUG)

    # Loggers that received the redirect handler (to restore on teardown).
    redirected_loggers: dict[logging.Logger, list[logging.FileHandler]] = {}

    def _is_real_log_handler(h: logging.Handler) -> bool:
        if not isinstance(h, logging.FileHandler):
            return False
        try:
            return str(Path(h.baseFilename).resolve()) == real
        except Exception:
            return False

    def _redirect_existing() -> None:
        """Detach any already-attached real-log handlers on all loggers."""
        for lgr in _iter_all_loggers():
            for h in list(lgr.handlers):
                if _is_real_log_handler(h):
                    lgr.removeHandler(h)
                    redirected_loggers.setdefault(lgr, []).append(h)
                    if redirect_handler not in lgr.handlers:
                        lgr.addHandler(redirect_handler)

    # Layer 1: detach pre-existing real handlers.
    _redirect_existing()

    # Layer 2: intercept future addHandler calls.
    _original_add_handler = logging.Logger.addHandler

    def _guarded_add_handler(self: logging.Logger, hdlr: logging.Handler) -> None:
        if _is_real_log_handler(hdlr):
            # Drop the real-log handler; ensure redirect is attached instead.
            redirected_loggers.setdefault(self, []).append(hdlr)
            if redirect_handler not in self.handlers:
                _original_add_handler(self, redirect_handler)
        else:
            _original_add_handler(self, hdlr)

    logging.Logger.addHandler = _guarded_add_handler  # type: ignore[method-assign]

    yield tmp_log  # test body runs here

    # --- Teardown -----------------------------------------------------------
    # Restore the original addHandler method first.
    logging.Logger.addHandler = _original_add_handler  # type: ignore[method-assign]

    # Re-scan: if the test imported the module after yield but before teardown
    # (unlikely with yield but defensive) catch any stragglers.
    _redirect_existing()

    # Remove redirect handler and restore original handlers on each logger.
    # Exception-safe: a single broken handler must downgrade restoration, not
    # turn the test into an ERROR (the addHandler patch is already restored).
    for lgr, originals in redirected_loggers.items():
        try:
            if redirect_handler in lgr.handlers:
                lgr.removeHandler(redirect_handler)
            for orig_h in originals:
                if orig_h not in lgr.handlers:
                    lgr.addHandler(orig_h)
        except Exception:
            pass

    try:
        redirect_handler.close()
    except Exception:
        pass
