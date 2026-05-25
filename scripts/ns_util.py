"""ns_util.py — Pinecone namespace sanitization (shared).

Pinecone namespaces must be ASCII. Cyrillic project names (Клошар, Дело Зоя…) cause
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


def sanitize_ns(name: str) -> str:
    """Return an ASCII, Pinecone-safe namespace for any (possibly Cyrillic) name."""
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
    return s or "default"
