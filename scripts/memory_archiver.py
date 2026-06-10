#!/usr/bin/env python3
"""memory_archiver.py — Archive never-recalled old learning vectors from local_rag.db.

Rationale: local_rag.db has ~41k vectors; re-recall for the last 30 days is 0.
Stale "learning" vectors degrade cosine retrieval by adding noise.  This tool
identifies vectors that are:
  - type == "learning"  (configurable; "promoted"/"decision"/"gotcha" are NEVER touched)
  - older than N days   (meta['date'] field, default 180 days)
  - zero recall hits    (absent from recall-tracker.json)

DRY by default — nothing is changed until --apply is passed.

Usage:
    python memory_archiver.py              # DRY report: counts only
    python memory_archiver.py --apply      # requires today's backup to exist
    python memory_archiver.py --restore <id1,id2,...>

Never raises a non-zero exit on missing DB — prints a warning and exits 0.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# ---------------------------------------------------------------------------
# Path constants — mirror local_rag.py conventions exactly
# ---------------------------------------------------------------------------
DB_PATH = Path(os.environ.get("LOCAL_RAG_DB", str(Path.home() / ".claude" / "local_rag.db")))
BACKUP_DIR = Path.home() / ".claude" / "backups"
TRACKER_PATH = Path.home() / ".claude" / "logs" / "recall-tracker.json"

# Types that are NEVER archived regardless of age or recall hits
PROTECTED_TYPES: frozenset[str] = frozenset({"promoted", "decision", "gotcha", "antipattern", "pattern"})


# ---------------------------------------------------------------------------
# Schema note (from local_rag.py):
#   CREATE TABLE vectors (
#       ns    TEXT NOT NULL,
#       id    TEXT NOT NULL,
#       text  TEXT NOT NULL,
#       meta  TEXT NOT NULL,   -- JSON: {type, date, ttl_days, ...}
#       vec   BLOB NOT NULL,
#       PRIMARY KEY (ns, id)
#   )
# The archive table adds `archived_at TEXT NOT NULL`.
# ---------------------------------------------------------------------------

_CREATE_ARCHIVE = """
CREATE TABLE IF NOT EXISTS vectors_archive (
    ns          TEXT NOT NULL,
    id          TEXT NOT NULL,
    text        TEXT NOT NULL,
    meta        TEXT NOT NULL,
    vec         BLOB NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (ns, id)
)
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_tracker(recall_path: Path) -> set[str]:
    """Return set of vector IDs that have at least one recall hit.

    Args:
        recall_path: Path to recall-tracker.json.

    Returns:
        Set of IDs with hit_count >= 1.
    """
    if not recall_path.exists():
        return set()
    try:
        data: dict[str, Any] = json.loads(recall_path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {vid for vid, entry in data.items()
            if isinstance(entry, dict) and int(entry.get("hit_count", 0)) >= 1}


def _parse_date(meta_json: str) -> datetime | None:
    """Extract and parse meta['date'] (YYYY-MM-DD or ISO) from a JSON string.

    Args:
        meta_json: Serialised meta dict.

    Returns:
        Timezone-aware datetime or None on failure.
    """
    try:
        m = json.loads(meta_json) if meta_json else {}
        raw = m.get("date", "")
        if not raw:
            return None
        # Accept full ISO or date-only
        dt = datetime.fromisoformat(str(raw)[:10])  # keep date part
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _get_type(meta_json: str) -> str:
    """Extract meta['type'] from JSON string. Returns '' on failure."""
    try:
        m = json.loads(meta_json) if meta_json else {}
        return str(m.get("type", ""))
    except Exception:
        return ""


def _today_backup_exists(backup_dir: Path, now: datetime) -> bool:
    """Return True if a local_rag backup file was created today.

    Checks both the fixed 'local_rag-daily.db' (mtime today) and any
    dated 'local_rag-<timestamp>.db' files modified today.

    Args:
        backup_dir: Directory containing backup files.
        now: Reference datetime for 'today'.

    Returns:
        True if at least one valid backup from today is found.
    """
    today = now.date()
    # Check fixed daily backup
    daily = backup_dir / "local_rag-daily.db"
    if daily.exists():
        mtime = datetime.fromtimestamp(daily.stat().st_mtime, tz=timezone.utc).date()
        if mtime == today:
            return True
    # Check dated rotating backups
    for p in backup_dir.glob("local_rag-[0-9]*.db"):
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).date()
        if mtime == today:
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_archivable(
    conn: sqlite3.Connection,
    recall_data: set[str],
    older_than_days: int = 180,
    types: tuple[str, ...] = ("learning",),
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Find vectors eligible for archiving.

    Criteria:
      - meta['type'] is in *types* (and NOT in PROTECTED_TYPES)
      - meta['date'] is older than *older_than_days* days
      - vector ID not in *recall_data* (zero recall hits)

    Args:
        conn: Open sqlite3 connection to local_rag.db.
        recall_data: Set of IDs with at least one recall hit.
        older_than_days: Age threshold in days.
        types: Tuple of type values to consider. Protected types are skipped.
        now: Reference datetime (UTC). Defaults to current UTC time.

    Returns:
        List of dicts with keys: ns, id, meta_type, date, age_days.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=older_than_days)

    # Filter out protected types from the requested set
    safe_types = [t for t in types if t not in PROTECTED_TYPES]
    if not safe_types:
        return []

    rows = conn.execute("SELECT ns, id, meta FROM vectors").fetchall()
    result: list[dict[str, Any]] = []

    for ns, vid, meta_json in rows:
        meta_type = _get_type(meta_json)
        # Must be an eligible type
        if meta_type not in safe_types:
            continue
        # Safety double-check — never archive protected types
        if meta_type in PROTECTED_TYPES:
            continue
        # Must have a parseable date older than cutoff
        dt = _parse_date(meta_json)
        if dt is None or dt >= cutoff:
            continue
        # Must have zero recall hits
        if vid in recall_data:
            continue
        age_days = (now - dt).days
        result.append({"ns": ns, "id": vid, "meta_type": meta_type,
                       "date": dt.date().isoformat(), "age_days": age_days})

    return result


def archive(
    db_path: Path = DB_PATH,
    recall_path: Path = TRACKER_PATH,
    older_than_days: int = 180,
    types: tuple[str, ...] = ("learning",),
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Identify (and optionally move) archivable vectors.

    DRY by default (apply=False): counts and breaks down by namespace, no DB changes.

    When apply=True:
      1. Refuses if no today's backup exists in BACKUP_DIR.
      2. Ensures vectors_archive table exists.
      3. Moves rows atomically (INSERT…SELECT + DELETE in one transaction).
      4. Returns actual counts.

    Args:
        db_path: Path to local_rag.db.
        recall_path: Path to recall-tracker.json.
        older_than_days: Age threshold in days.
        types: Vector types to consider for archiving.
        apply: If True, actually perform the archive operation.
        now: Reference datetime (UTC). Defaults to current UTC time.

    Returns:
        Dict with keys: examined, archivable, applied, by_namespace.
    """
    now = now or datetime.now(timezone.utc)

    if not db_path.exists():
        print(f"Warning: DB not found at {db_path} — nothing to archive.", file=sys.stderr)
        return {"examined": 0, "archivable": 0, "applied": False, "by_namespace": {}}

    recall_data = _load_tracker(recall_path)

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    try:
        candidates = find_archivable(conn, recall_data, older_than_days, types, now)

        # Total row count for "examined"
        examined = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]

        by_ns: dict[str, int] = {}
        for c in candidates:
            by_ns[c["ns"]] = by_ns.get(c["ns"], 0) + 1

        result: dict[str, Any] = {
            "examined": examined,
            "archivable": len(candidates),
            "applied": False,
            "by_namespace": by_ns,
        }

        if not apply:
            return result

        # --- APPLY MODE ---
        if not _today_backup_exists(BACKUP_DIR, now):
            print(
                "ABORT: No today's backup found in "
                f"{BACKUP_DIR}\n"
                "Run first:  python backup_local_rag.py\n"
                "Then re-run with --apply.",
                file=sys.stderr,
            )
            return result

        if not candidates:
            result["applied"] = True
            return result

        # Ensure archive table exists
        conn.execute(_CREATE_ARCHIVE)

        archived_at = now.isoformat(timespec="seconds")
        ids = [(c["ns"], c["id"]) for c in candidates]

        # Single atomic transaction. Statements are chunked because SQLite caps
        # bound parameters at 999 (SQLITE_MAX_VARIABLE_NUMBER) — 500+ candidates
        # in one IN (VALUES ...) would raise "too many SQL variables".
        _CHUNK = 400  # 800 params per statement, safely under the 999 cap
        with conn:
            for i in range(0, len(ids), _CHUNK):
                chunk = ids[i:i + _CHUNK]
                params = [v for pair in chunk for v in pair]
                placeholders = ",".join("(?,?)" for _ in chunk)
                conn.execute(
                    f"INSERT OR REPLACE INTO vectors_archive "
                    f"SELECT ns, id, text, meta, vec, '{archived_at}' FROM vectors "
                    f"WHERE (ns, id) IN (VALUES {placeholders})",
                    params,
                )
                conn.execute(
                    f"DELETE FROM vectors "
                    f"WHERE (ns, id) IN (VALUES {placeholders})",
                    params,
                )

        result["applied"] = True
        result["archived"] = len(candidates)
        return result

    finally:
        conn.close()


def restore(
    item_ids: list[str],
    db_path: Path = DB_PATH,
) -> int:
    """Move vectors back from vectors_archive to vectors.

    Args:
        item_ids: List of vector IDs to restore (matched by id column).
        db_path: Path to local_rag.db.

    Returns:
        Number of rows actually restored.
    """
    if not db_path.exists() or not item_ids:
        return 0

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        # Check archive table exists
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vectors_archive'"
        ).fetchone()
        if not tbl:
            return 0

        ph = ",".join("?" * len(item_ids))
        with conn:
            conn.execute(
                f"INSERT OR REPLACE INTO vectors (ns, id, text, meta, vec) "
                f"SELECT ns, id, text, meta, vec FROM vectors_archive WHERE id IN ({ph})",
                item_ids,
            )
            n = conn.execute(
                f"DELETE FROM vectors_archive WHERE id IN ({ph})",
                item_ids,
            ).rowcount
        return n
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_report(result: dict[str, Any], older_than_days: int) -> None:
    print(f"=== memory_archiver (DRY RUN) ===")
    print(f"  DB vectors examined : {result['examined']:,}")
    print(f"  Archivable (>{older_than_days}d, 0 recalls, type=learning) : {result['archivable']:,}")
    if result["by_namespace"]:
        print("  By namespace:")
        for ns, cnt in sorted(result["by_namespace"].items(), key=lambda kv: -kv[1]):
            print(f"    {cnt:>6}  {ns}")
    if not result.get("applied"):
        print("\n  Run with --apply to move them to vectors_archive (backup required first).")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="Actually archive (requires today's backup)")
    ap.add_argument("--restore", metavar="IDS",
                    help="Comma-separated vector IDs to restore from archive")
    ap.add_argument("--days", type=int, default=180,
                    help="Age threshold in days (default 180)")
    args = ap.parse_args()

    if args.restore:
        ids = [x.strip() for x in args.restore.split(",") if x.strip()]
        n = restore(ids)
        print(f"Restored {n} vector(s) from archive.")
        return

    result = archive(older_than_days=args.days, apply=args.apply)

    if args.apply:
        print(f"Archive complete: {result.get('archived', 0)} vectors moved "
              f"(of {result['archivable']} eligible, {result['examined']} examined).")
    else:
        _print_report(result, args.days)


if __name__ == "__main__":
    main()
