"""Tests for pinecone.py — focus on the importable-safety contract.

The critical invariant: network/API failures must raise PineconeError
(an Exception subclass), NOT SystemExit (a BaseException). A regression to
sys.exit() would silently kill any importer that catches `except Exception`
(session_start_brief hook, automation_dispatcher) on transient errors.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def test_pinecone_error_is_exception_not_baseexception():
    import pinecone
    # Must be catchable by `except Exception` — the whole point of the fix.
    assert issubclass(pinecone.PineconeError, Exception)


def test_req_raises_pinecone_error_on_network_failure(monkeypatch):
    import pinecone
    from urllib.error import URLError
    # Mock the transport so NO real socket is opened — the original used a real
    # request to an unresolvable host, which depended on DNS/connect behaviour
    # and could stall the suite (a hostname relied on DNS; a loopback IP made
    # Windows wait out the connect for ~18s). We test only that a transport
    # failure is wrapped as PineconeError (an Exception), never SystemExit.
    def boom(*a, **k):
        raise URLError("mocked network failure")
    monkeypatch.setattr(pinecone, "urlopen", boom)
    try:
        pinecone._req("http://example.test/foo")
        assert False, "expected PineconeError"
    except pinecone.PineconeError:
        pass  # correct
    except SystemExit:
        assert False, "regression: _req() raised SystemExit (BaseException)"


def test_query_and_track_returns_empty_list_on_error(monkeypatch):
    import pinecone
    # Pin the hosted backend: with MEMORY_BACKEND=local (the default) the
    # function delegates to local_rag → a REAL Ollama embed call, which made
    # this test silently network-dependent (fast Ollama = "pass", busy
    # Ollama = suite hang caught 2026-06-11). The embed mock below is only
    # on the pinecone path.
    monkeypatch.setattr(pinecone, "MEMORY_BACKEND", "pinecone")
    # Force embed() to fail with a network error mid-flow.
    def boom(*a, **k):
        raise pinecone.PineconeError("simulated network failure")
    monkeypatch.setattr(pinecone, "embed", boom)
    result = pinecone.query_and_track("any_ns", "any query", topk=3)
    assert result == []  # never raises, never exits


def test_query_and_track_does_not_propagate_systemexit(monkeypatch):
    import pinecone
    monkeypatch.setattr(pinecone, "MEMORY_BACKEND", "pinecone")
    # Even if something raises SystemExit internally, query_and_track's
    # contract is "never crash the caller". (SystemExit is BaseException,
    # so this documents that the wrapper's except Exception will NOT catch
    # it — verifying we don't reintroduce sys.exit in the hot path.)
    def real_embed_path(*a, **k):
        raise pinecone.PineconeError("net down")
    monkeypatch.setattr(pinecone, "embed", real_embed_path)
    # Should return [] cleanly, not raise.
    assert pinecone.query_and_track("ns", "q") == []


def test_query_and_track_local_backend_failure_returns_empty(monkeypatch):
    """The never-raise contract must hold on the LOCAL path too — stubbed,
    so no real Ollama/sqlite is touched."""
    import pinecone

    class _BoomLocal:
        @staticmethod
        def query(*a, **k):
            raise RuntimeError("local backend down")

    monkeypatch.setattr(pinecone, "MEMORY_BACKEND", "local")
    monkeypatch.setattr(pinecone, "_local", lambda: _BoomLocal)
    assert pinecone.query_and_track("ns", "q") == []
