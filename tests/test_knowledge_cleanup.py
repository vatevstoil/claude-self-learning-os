"""Smoke tests for knowledge_cleanup.py — pure/importable logic only.

knowledge_cleanup.py's high-level functions (purge_noise, consolidate,
report) all require either Pinecone HTTP or a local_rag SQLite backend.
The only importable pure helpers are:
  - pc_quote   — URL-quote utility
  - CONSOLIDATE — the alias→canonical map (structural integrity)

Everything that calls _req() / _dump() / _delete() / _upsert() hits
the network or an external process — those are NOT unit-testable without
a heavy integration harness (real Pinecone key or sqlite DB).

Strategy: test pc_quote + the structural invariants of CONSOLIDATE, and
verify that purge_noise in dry-run mode (MEMORY_BACKEND=local with a
stubbed local_rag) does NOT call _delete (mock the one side-effecting
call).
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_local_rag(rows: list[dict]) -> types.ModuleType:
    """Return a minimal stub of local_rag with controllable fetch_all."""
    mod = types.ModuleType("local_rag")
    mod.fetch_all = lambda ns: rows  # type: ignore[attr-defined]
    mod.existing_ids = lambda ns: []  # type: ignore[attr-defined]
    mod.all_namespaces = lambda: []   # type: ignore[attr-defined]
    mod.delete = lambda ns, vid: None  # type: ignore[attr-defined]
    mod.upsert_vec = lambda *a, **kw: None  # type: ignore[attr-defined]
    mod.recanonicalize = lambda: {}  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# 1. pc_quote — pure URL-quote utility
# ---------------------------------------------------------------------------

class TestPcQuote:
    def test_plain_ascii_unchanged(self):
        import knowledge_cleanup as kc
        assert kc.pc_quote("Trading") == "Trading"

    def test_space_encoded(self):
        import knowledge_cleanup as kc
        assert kc.pc_quote("Web Design") == "Web%20Design"

    def test_cyrillic_encoded(self):
        import knowledge_cleanup as kc
        result = kc.pc_quote("{{PRIVATE_NS}}")
        assert "%" in result  # must be percent-encoded

    def test_slash_encoded(self):
        import knowledge_cleanup as kc
        assert "/" not in kc.pc_quote("a/b")


# ---------------------------------------------------------------------------
# 2. CONSOLIDATE map — structural integrity
# ---------------------------------------------------------------------------

class TestConsolidateMap:
    def test_no_alias_equals_its_own_canonical(self):
        """An alias must not equal the canonical key it maps to."""
        import knowledge_cleanup as kc
        for canon, aliases in kc.CONSOLIDATE.items():
            for alias in aliases:
                assert alias != canon, (
                    f"Alias {alias!r} equals its own canonical — would create a self-loop"
                )

    def test_no_alias_appears_in_multiple_canonicals(self):
        """Each alias namespace must map to exactly one canonical."""
        import knowledge_cleanup as kc
        seen: dict[str, str] = {}
        for canon, aliases in kc.CONSOLIDATE.items():
            for alias in aliases:
                assert alias not in seen, (
                    f"Alias {alias!r} maps to both {seen[alias]!r} and {canon!r}"
                )
                seen[alias] = canon

    def test_all_values_are_lists(self):
        import knowledge_cleanup as kc
        for canon, aliases in kc.CONSOLIDATE.items():
            assert isinstance(aliases, list), f"{canon!r} aliases should be a list"


# ---------------------------------------------------------------------------
# 3. purge_noise dry-run: must NOT call _delete when apply=False
# ---------------------------------------------------------------------------

class TestPurgeNoiseDryRun:
    def test_no_delete_in_dry_run(self, monkeypatch, capsys):
        """With apply=False, _delete must never be invoked — even if all rows
        look like noise. This is the core dry-run guarantee."""
        # Reload to avoid cached env-driven module state
        if "knowledge_cleanup" in sys.modules:
            del sys.modules["knowledge_cleanup"]

        # Stub local_rag with two "noise" rows
        fake_rows = [
            {"id": "v1", "metadata": {"type": "learning", "text": "session complete"}},
            {"id": "v2", "metadata": {"type": "learning", "text": "thank you done"}},
        ]
        fake_lr = _make_fake_local_rag(fake_rows)
        sys.modules["local_rag"] = fake_lr

        # Stub auto_pinecone_save's noise guard — always classify as noise
        fake_aps = types.ModuleType("auto_pinecone_save")
        fake_aps.is_conversational_noise = lambda txt: True  # type: ignore[attr-defined]
        sys.modules["auto_pinecone_save"] = fake_aps

        import knowledge_cleanup as kc
        monkeypatch.setattr(kc, "MEMORY_BACKEND", "local")

        deleted_ids: list = []

        def _fake_delete(ns, ids):
            deleted_ids.extend(ids)

        monkeypatch.setattr(kc, "_delete", _fake_delete)
        monkeypatch.setattr(kc, "_local", lambda: fake_lr)

        kc.purge_noise("TestNS", apply=False)

        assert deleted_ids == [], (
            "dry-run purge_noise must not call _delete, but it did"
        )

        out = capsys.readouterr().out
        assert "WOULD DELETE" in out
        assert "dry-run" in out.lower()

        # Cleanup stubs
        for mod in ("local_rag", "auto_pinecone_save"):
            sys.modules.pop(mod, None)
