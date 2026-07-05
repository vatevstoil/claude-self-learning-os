"""visibility_guard.py — privacy wall for cross-project / Pinecone flows.

Three call sites import this (all inside try/except): pinecone._queue_pending,
cross_project_search, and pii_scanner. Its absence is "fail-OPEN" — a private
namespace could be written to the plaintext save queue or surface in a
cross-project search. This module restores fail-CLOSED behaviour for the
namespaces you list below.

INTERFACE (relied on by callers):
  - SAFETY_DEFAULT      : iterable of private markers   (pii_scanner)
  - private_namespaces(): set of canonical private namespaces (pinecone)
  - filter_namespaces(names) -> (kept, skipped)         (cross_project_search)

CONFIGURE: replace the {{PRIVATE_NS}} placeholders with YOUR own personal /
legal / non-shareable namespace names (the ones that must never leak into a
cross-project search or a plaintext queue). Business namespaces you are happy to
recall across projects should NOT be listed here.
"""
from __future__ import annotations

import re
from typing import Iterable, Tuple, List, Set

# Personal / legal / non-business namespaces that must never leak into a
# cross-project search or sit in a plaintext save queue. Fill with your own.
_PRIVATE: frozenset = frozenset({
    "{{PRIVATE_NS}}",
    "{{PRIVATE_NS}}",
    "{{PRIVATE_NS}}",
})

# Exposed for pii_scanner (it filters out bare-identity words itself).
SAFETY_DEFAULT = _PRIVATE


def _norm(s: str) -> str:
    """Separator/case-insensitive key so 'Foo-Bar' == 'Foo Bar' == 'foobar'."""
    return re.sub(r"[-._ ]", "", s or "").lower()


_PRIVATE_NORM: Set[str] = {_norm(x) for x in _PRIVATE}


def is_private(name: str) -> bool:
    """True if *name* resolves to a private namespace (separator/case-insensitive)."""
    return _norm(name) in _PRIVATE_NORM


def private_namespaces() -> Set[str]:
    """Canonical set of private namespace names."""
    return set(_PRIVATE)


def filter_namespaces(names: Iterable[str]) -> Tuple[List[str], List[str]]:
    """Split *names* into (public_kept, private_skipped)."""
    kept: List[str] = []
    skipped: List[str] = []
    for n in names:
        (skipped if is_private(n) else kept).append(n)
    return kept, skipped
