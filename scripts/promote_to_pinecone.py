#!/usr/bin/env python3
"""promote_to_pinecone.py - Manually save a confirmed pattern to Pinecone.

Use this for high-value learnings that should NEVER expire.
Sets type=promoted, ttl_days=9999, prefixes ID with 'promoted-'.

Non-ASCII namespaces (e.g. Cyrillic "Петър Дънов") are automatically
transliterated to an ASCII slug for the Pinecone namespace + vector ID
(Pinecone requires ASCII IDs). The original namespace is preserved in
metadata as `display_namespace` so retrieval tools can show the human name.

Usage:
    python promote_to_pinecone.py <namespace> "<the pattern text>"
    python promote_to_pinecone.py _shared "BullMQ workers must call worker.close() on SIGTERM"
    python promote_to_pinecone.py Cinemind "RLS gotcha: ..." --source cinemind/wiki/sources/learnings.md
    python promote_to_pinecone.py "Петър Дънов" "..."    # → namespace='Petar-Danov', display_namespace='Петър Дънов'
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

PINECONE_CLI = Path.home() / ".claude" / "scripts" / "pinecone.py"

# Bulgarian Cyrillic → Latin transliteration (official BG standard, simplified).
# Covers all 30 letters of the modern BG alphabet (both cases).
CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sht", "ъ": "a",
    "ь": "y", "ю": "yu", "я": "ya",
    # Archaic / Old Bulgarian
    "ѣ": "ya", "ѫ": "a",
    # Other Cyrillic letters (Russian/Ukrainian) for safety
    "ё": "yo", "ы": "y", "э": "e", "є": "ie", "і": "i", "ї": "yi", "ґ": "g",
}


def ascii_slug(s: str) -> str:
    """Transliterate Cyrillic + ASCII-fold any other non-ASCII, then slugify.

    - Cyrillic letters → Latin per BG transliteration table.
    - Any remaining non-ASCII → NFKD decomposed and stripped of combining marks.
    - Whitespace + punctuation other than [A-Za-z0-9_] → '-'.
    - Collapses multiple '-' and trims edges.
    - If already ASCII-clean → returned unchanged (no-op for normal namespaces).
    """
    # Fast path: already a safe ASCII slug
    if s.isascii() and re.fullmatch(r"[A-Za-z0-9._-]+", s or ""):
        return s

    # Apply Cyrillic → Latin (case-preserving)
    out = []
    for ch in s:
        low = ch.lower()
        if low in CYR_TO_LAT:
            mapped = CYR_TO_LAT[low]
            # Preserve case of the first letter for human-readable IDs
            out.append(mapped.capitalize() if ch.isupper() else mapped)
        else:
            out.append(ch)
    s = "".join(out)

    # Fold any remaining non-ASCII via NFKD (strip combining marks)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.encode("ascii", "ignore").decode("ascii")

    # Slugify: keep alnum + . _ -, replace runs of other chars with '-'
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-_.")
    return s or "unnamed"


def _check_duplicates(namespace: str, text: str, threshold: float = 0.88) -> list[dict]:
    """Query Pinecone for semantically similar existing entries.

    Args:
        namespace: target namespace
        text: candidate text to promote
        threshold: score above which to consider it a near-duplicate

    Returns list of {id, score, preview} dicts for matches above threshold.
    Returns [] if Pinecone unavailable — never blocks promotion on network errors.
    """
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import pinecone as _pc
        matches = _pc.query_and_track(namespace, text, topk=3)
        return [
            {
                "id": m.get("id", "?"),
                "score": m.get("score", 0.0),
                "preview": ((m.get("metadata") or {}).get("text", ""))[:120],
            }
            for m in (matches or [])
            if m.get("score", 0.0) >= threshold
        ]
    except Exception:
        return []


def main():
    p = argparse.ArgumentParser()
    p.add_argument("namespace")
    p.add_argument("text")
    p.add_argument("--source", default="manual",
                   help="Where this pattern came from (e.g., 'cinemind/auth.md')")
    p.add_argument("--force", action="store_true",
                   help="Skip pre-promote semantic duplicate check")
    args = p.parse_args()

    if not args.text.strip():
        sys.exit("ERROR: text is empty")

    safe_ns = ascii_slug(args.namespace)

    # Pre-promote semantic check: refuse if near-duplicate exists. Prevents
    # accumulation of redundant patterns and forces explicit override via --force.
    # Skipped silently when Pinecone is unavailable (e.g. offline run).
    if not args.force:
        dupes = _check_duplicates(safe_ns, args.text, threshold=0.88)
        if dupes:
            print(f"⚠ Found {len(dupes)} similar existing entry/ies in '{safe_ns}':")
            for d in dupes:
                print(f"  [{d['score']:.3f}] {d['id']}")
                print(f"     {d['preview']}")
            print()
            print("Refusing to promote — duplicate-looking pattern. Options:")
            print("  - Adjust text to add new insight, or")
            print("  - Re-run with --force to promote anyway.")
            sys.exit(2)

    content_hash = hashlib.sha256(args.text.encode("utf-8")).hexdigest()[:12]
    entry_id = f"{safe_ns}-promoted-{content_hash}"

    meta_parts = [
        "type=promoted",
        f"date={datetime.now().strftime('%Y-%m-%d')}",
        f"project={safe_ns}",
        "ttl_days=9999",
        f"source={args.source}",
    ]
    if safe_ns != args.namespace:
        # Preserve human-readable original for retrieval/display
        meta_parts.append(f"display_namespace={args.namespace}")
    meta = ",".join(meta_parts)

    cmd = [sys.executable, str(PINECONE_CLI), "save",
           safe_ns, entry_id, args.text, "--meta", meta]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        sys.exit(f"Failed: {result.stderr or result.stdout}")

    if safe_ns != args.namespace:
        print(f"Promoted: {entry_id} (namespace='{safe_ns}' from '{args.namespace}', type=promoted, ttl=NEVER_EXPIRE)")
    else:
        print(f"Promoted: {entry_id} (type=promoted, ttl=NEVER_EXPIRE)")


if __name__ == "__main__":
    main()
