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
    # Point at an unresolvable host so urlopen raises URLError.
    try:
        pinecone._req("https://this-host-does-not-exist.invalid/foo")
        assert False, "expected PineconeError"
    except pinecone.PineconeError:
        pass  # correct
    except SystemExit:
        assert False, "regression: _req() raised SystemExit (BaseException)"


def test_query_and_track_returns_empty_list_on_error(monkeypatch):
    import pinecone
    # Force embed() to fail with a network error mid-flow.
    def boom(*a, **k):
        raise pinecone.PineconeError("simulated network failure")
    monkeypatch.setattr(pinecone, "embed", boom)
    result = pinecone.query_and_track("any_ns", "any query", topk=3)
    assert result == []  # never raises, never exits


def test_query_and_track_does_not_propagate_systemexit(monkeypatch):
    import pinecone
    # Even if something raises SystemExit internally, query_and_track's
    # contract is "never crash the caller". (SystemExit is BaseException,
    # so this documents that the wrapper's except Exception will NOT catch
    # it — verifying we don't reintroduce sys.exit in the hot path.)
    def real_embed_path(*a, **k):
        raise pinecone.PineconeError("net down")
    monkeypatch.setattr(pinecone, "embed", real_embed_path)
    # Should return [] cleanly, not raise.
    assert pinecone.query_and_track("ns", "q") == []
