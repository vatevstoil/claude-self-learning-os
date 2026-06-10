"""ns_util.py — Pinecone namespace sanitization (shared).

Pinecone namespaces must be ASCII. Cyrillic project names (Клошар, {{PRIVATE_NS}}…) cause
HTTP 400 on upsert. This transliterates BG Cyrillic → Latin and strips remaining
non-ASCII, so saves AND recall always hit the SAME namespace regardless of caller.
"""
from __future__ import annotations

import re

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sht", "ъ": "a",
    "ь": "y", "ю": "yu", "я": "ya",
}


# Canonical aliases — keyed on the POST-sanitize form (spaces already → '-').
# Prevents namespace re-fragmentation: historical drift created duplicates like
# Trading/Claude Trading/Claude-Trading that split recall. After a one-time
# consolidation (knowledge_cleanup.py), this keeps every future save+recall
# converging on the canonical namespace. Add new aliases here as they appear.
_ALIASES = {
    "Claude-Trading": "Trading",
    "Facturka.bg": "Fakturka.bg",
    "AI Video": "AI-Video",
    "Davinci Plugin": "Davinci-Plugin",
    "CasinoScore-AI": "CasinoScore",
    "Web-Design": "WebDesign",
    "Web-Designe": "WebDesign",
    "Petar-Danov": "PetarDanov",
    "shared": "_shared",
}


def _norm(s: str) -> str:
    """Collapse separators + case so 'Facturka-bg', 'Facturka.bg', 'facturka bg'
    all compare equal — alias lookup must not depend on dash/dot/space/case."""
    return re.sub(r"[-._ ]", "", s).lower()


# Separator-insensitive alias index, built once from the explicit aliases above.
_ALIAS_NORM = {_norm(k): v for k, v in _ALIASES.items()}


def sanitize_ns(name: str) -> str:
    """Return an ASCII, Pinecone-safe, canonical namespace for any name."""
    if not name:
        return "default"
    out = []
    for ch in name:
        low = ch.lower()
        if low in _TRANSLIT:
            t = _TRANSLIT[low]
            out.append(t.capitalize() if ch.isupper() else t)
        else:
            out.append(ch)
    s = "".join(out)
    s = s.encode("ascii", "ignore").decode("ascii")   # drop any remaining non-ASCII
    s = re.sub(r"[^A-Za-z0-9._-]", "-", s).strip("-")  # safe charset; keep leading _ (valid, intentional: _meta/_shared)
    s = s or "default"
    if s in _ALIASES:                                  # exact alias (fast path)
        return _ALIASES[s]
    return _ALIAS_NORM.get(_norm(s), s)                # separator/case-insensitive alias
