#!/usr/bin/env python3
"""promote_to_pinecone.py - Manually save a confirmed pattern to Pinecone.

Use this for high-value learnings that should NEVER expire.
Sets type=promoted, ttl_days=9999, prefixes ID with 'promoted-'.

Usage:
    python promote_to_pinecone.py <namespace> "<the pattern text>"
    python promote_to_pinecone.py _shared "BullMQ workers must call worker.close() on SIGTERM"
    python promote_to_pinecone.py Cinemind "RLS gotcha: ..." --source cinemind/wiki/sources/learnings.md
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PINECONE_CLI = Path.home() / ".claude" / "scripts" / "pinecone.py"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("namespace")
    p.add_argument("text")
    p.add_argument("--source", default="manual",
                   help="Where this pattern came from (e.g., 'cinemind/auth.md')")
    args = p.parse_args()

    if not args.text.strip():
        sys.exit("ERROR: text is empty")

    content_hash = hashlib.sha256(args.text.encode("utf-8")).hexdigest()[:12]
    entry_id = f"{args.namespace}-promoted-{content_hash}"

    meta = (
        f"type=promoted,"
        f"date={datetime.now().strftime('%Y-%m-%d')},"
        f"project={args.namespace},"
        f"ttl_days=9999,"
        f"source={args.source}"
    )

    cmd = [sys.executable, str(PINECONE_CLI), "save",
           args.namespace, entry_id, args.text, "--meta", meta]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        sys.exit(f"Failed: {result.stderr or result.stdout}")
    print(f"Promoted: {entry_id} (type=promoted, ttl=NEVER_EXPIRE)")


if __name__ == "__main__":
    main()
