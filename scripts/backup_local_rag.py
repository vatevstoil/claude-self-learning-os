#!/usr/bin/env python3
"""Daily consistent backup of local_rag.db (the ONLY copy of local-first memory
since the Pinecone decoupling). Uses the sqlite online-backup API (safe during
concurrent writes). Keeps the most recent N backups. Wired into the daily dispatcher.

Usage: python backup_local_rag.py [--keep 7]
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_rag

BACKUP_DIR = Path.home() / ".claude" / "backups"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=7)
    args = ap.parse_args()

    if not local_rag.DB_PATH.exists():
        print("local_rag.db not found — nothing to back up.")
        return
    dest = BACKUP_DIR / "local_rag-daily.db"
    # snapshot to a temp name then atomically replace, so a crash mid-backup
    # never leaves a half-written "latest" backup.
    tmp = BACKUP_DIR / "local_rag-daily.tmp.db"
    local_rag.backup(str(tmp))
    import os
    os.replace(str(tmp), str(dest))
    size = dest.stat().st_size
    # also keep dated rotating copies
    import datetime  # noqa: argless new is fine here (real script, not workflow)
    stamp = dest.stat().st_mtime_ns
    rotated = BACKUP_DIR / f"local_rag-{stamp}.db"
    local_rag.backup(str(rotated))

    # prune: keep newest N dated backups
    dated = sorted(BACKUP_DIR.glob("local_rag-[0-9]*.db"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for old in dated[args.keep:]:
        try:
            old.unlink()
        except Exception:
            pass
    print(f"backup OK: {dest} ({size/1e6:.1f} MB) + rotated; kept {min(len(dated), args.keep)}")


if __name__ == "__main__":
    main()
