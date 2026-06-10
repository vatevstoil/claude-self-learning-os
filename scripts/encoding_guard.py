#!/usr/bin/env python3
"""encoding_guard.py — Mojibake detection and repair for cp1251-decoded-as-UTF-8 Cyrillic.

Common failure mode: a cp1251-encoded string was read with a UTF-8 decoder that
silently accepted the bytes.  The result is characteristic two-byte sequences
starting with 'Р' (U+0420) or 'С' (U+0421) — the cp1251 multibyte lead bytes
for the Cyrillic block.

Public API:
    looks_mojibake(text)  -> bool
    fix_mojibake(text)    -> str   (idempotent, never raises)
    guard(text)           -> str   (fix if broken, else original)
    scan_json_keys(obj)   -> list[str]

CLI:
    python encoding_guard.py check <file.json>   # list mojibake strings, exit 0
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Mojibake detection helpers
# ---------------------------------------------------------------------------

# The cp1251→UTF-8 mojibake pattern (seen in real data):
#
# Normal Bulgarian text stores А-Я (U+0410–U+042F) and а-я (U+0430–U+044F).
# When a cp1251-encoded string is read with a UTF-8 decoder that silently
# replaces bad bytes, the multi-byte UTF-8 sequences for Cyrillic are
# re-interpreted as individual codepoints.  The result is that each original
# Cyrillic letter (1 byte in cp1251) maps to two Unicode chars:
#
#   Р (U+0420) or С (U+0421) — the lead byte of the UTF-8 Cyrillic block
#   followed by a "tail" char OUTSIDE the standard BG range U+0410–U+044F
#
# Examples from production data:
#   Р + U+0452 (Ђ), U+0403 (Ѓ), U+0451 (ё), U+040E (Ў), U+045F (џ)
#   С + U+201A (‚), U+0409 (Љ), U+0402 (Ђ)
#   Р + U+00B5 (µ), U+00BB (»)  — Latin supplement artefacts
#
# In NORMAL Bulgarian text, Р and С are always followed by characters in the
# core range U+0410–U+044F (standard Cyrillic А-я) or whitespace/ASCII.
# The extended ranges (U+0400–U+040F, U+0450–U+04FF, U+2000–U+206F, etc.)
# never appear after Р/С in legitimate text.

_MOJIBAKE_LEAD = frozenset("РС")  # Р (U+0420), С (U+0421)

# Core standard Bulgarian Cyrillic: А-Я (U+0410–U+042F) + а-я (U+0430–U+044F)
_CORE_BG_LOWER = 0x0410
_CORE_BG_UPPER = 0x044F
_CYRILLIC_START = 0x0400
_CYRILLIC_END = 0x04FF


def _non_ascii_pairs(text: str) -> tuple[int, int]:
    """Return (lead_count, suspect_pairs) counts.

    lead_count: number of Р/С (U+0420/U+0421) characters followed by any
    non-ASCII char (i.e. total opportunities to form a mojibake pair).

    suspect_pairs: subset where the following char is non-ASCII AND outside
    the core Bulgarian range U+0410–U+044F.  This combination never occurs in
    normal Bulgarian text but is the hallmark of cp1251 mojibake.

    The density metric is suspect_pairs / lead_count (not over total chars),
    because in mojibake exactly half the chars are Р/С lead bytes — dividing
    by lead_count gives 100% for pure mojibake and 0% for normal Bulgarian.

    Args:
        text: Input string to analyse.

    Returns:
        Tuple of (lead_count, suspect_pair count).
    """
    lead_count = 0
    pairs = 0
    for i, c in enumerate(text):
        if c in _MOJIBAKE_LEAD and i + 1 < len(text):
            nxt = text[i + 1]
            nxt_cp = ord(nxt)
            if nxt_cp > 127:
                lead_count += 1
                # Suspect: next char is outside core BG block
                if not (_CORE_BG_LOWER <= nxt_cp <= _CORE_BG_UPPER):
                    pairs += 1
    return lead_count, pairs


def looks_mojibake(text: str) -> bool:
    """Return True when *text* appears to be Cyrillic mis-encoded via cp1251→latin.

    Algorithm:
      1. Count Р/С lead chars followed by any non-ASCII (lead_count) and the
         subset where that non-ASCII is OUTSIDE core BG U+0410–U+044F (suspect_pairs).
      2. If suspect_pairs / lead_count >= 0.60 AND the repaired string decodes
         to real Cyrillic → True.
      3. Normal Bulgarian: Р(U+0420) is always followed by а-я (U+0430–U+044F)
         → density == 0 → no false positive.
      4. Pure mojibake: every Р/С is followed by non-core-BG chars → density == 1.0.

    Args:
        text: Candidate string to examine.

    Returns:
        True if text looks like cp1251-decoded-as-UTF-8 mojibake.
    """
    if not text:
        return False
    lead_count, pairs = _non_ascii_pairs(text)
    if lead_count == 0 or pairs == 0:
        return False
    density = pairs / lead_count
    if density < 0.60:
        return False
    # Confirm repair produces valid Cyrillic (not just a density accident)
    repaired = fix_mojibake(text)
    if repaired == text:
        return False  # fix didn't change anything → not mojibake
    # Repaired should contain real core BG Cyrillic (U+0410–U+044F)
    return any(_CORE_BG_LOWER <= ord(c) <= _CORE_BG_UPPER for c in repaired)


def fix_mojibake(text: str) -> str:
    """Attempt to reverse cp1251→latin mis-decode.

    Encodes the string back to cp1251 bytes, then decodes as UTF-8.
    If the round-trip fails OR the result looks worse than the original,
    returns *text* unchanged.  Never raises.

    Args:
        text: Possibly-broken string.

    Returns:
        Repaired string, or original if repair would make things worse.
    """
    if not text:
        return text
    try:
        repaired = text.encode("cp1251", errors="strict").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text

    # Sanity: repaired should have more LOWERCASE Cyrillic (U+0430–U+044F) than
    # the original.  Mojibake strings contain Р/С (uppercase, U+0420/U+0421) but
    # virtually no lowercase а-я — after proper repair, lowercase letters appear.
    # This correctly rejects repairs that accidentally convert clean text to garbage.
    _LOWER_CYR_LOW = 0x0430
    _LOWER_CYR_HIGH = 0x044F
    orig_lower = sum(1 for c in text if _LOWER_CYR_LOW <= ord(c) <= _LOWER_CYR_HIGH)
    new_lower = sum(1 for c in repaired if _LOWER_CYR_LOW <= ord(c) <= _LOWER_CYR_HIGH)
    if new_lower <= orig_lower:
        return text  # repair made it worse or neutral — return original unchanged
    return repaired


def guard(text: str) -> str:
    """Return fix_mojibake(text) if looks_mojibake, else text unchanged.

    Args:
        text: Input string.

    Returns:
        Clean string (idempotent — calling twice returns the same result).
    """
    if looks_mojibake(text):
        return fix_mojibake(text)
    return text


# ---------------------------------------------------------------------------
# JSON structure scanner (diagnostic)
# ---------------------------------------------------------------------------

def scan_json_keys(obj: Any, _path: str = "") -> list[str]:
    """Recursively find all mojibake strings (keys or values) in a JSON structure.

    Args:
        obj: Any JSON-compatible value (dict, list, str, …).
        _path: Internal — current dot-path for display purposes.

    Returns:
        List of offending strings found anywhere in the structure.
    """
    found: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and looks_mojibake(k):
                found.append(k)
            found.extend(scan_json_keys(v, f"{_path}.{k}" if _path else k))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            found.extend(scan_json_keys(item, f"{_path}[{i}]"))
    elif isinstance(obj, str) and looks_mojibake(obj):
        found.append(obj)
    return found


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_check(file_path: str) -> None:
    """Print all mojibake strings found in a JSON file."""
    path = Path(file_path)
    if not path.exists():
        print(f"File not found: {file_path}", file=sys.stderr)
        sys.exit(1)
    try:
        obj = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        print(f"JSON parse error: {exc}", file=sys.stderr)
        sys.exit(1)

    found = scan_json_keys(obj)
    if not found:
        print("No mojibake strings found.")
    else:
        print(f"Found {len(found)} mojibake string(s):")
        for s in found:
            repaired = fix_mojibake(s)
            print(f"  BAD : {s!r}")
            print(f"  FIX : {repaired!r}")
            print()


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] != "check":
        print("Usage: python encoding_guard.py check <file.json>")
        sys.exit(0)
    _cli_check(sys.argv[2])


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    main()
