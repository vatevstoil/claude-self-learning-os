"""Tests for memory_archiver.py — archive never-recalled old learning vectors.

Schema (from local_rag.py):
    CREATE TABLE IF NOT EXISTS vectors (
        ns    TEXT NOT NULL,
        id    TEXT NOT NULL,
        text  TEXT NOT NULL,
        meta  TEXT NOT NULL,
        vec   BLOB NOT NULL,
        PRIMARY KEY (ns, id)
    )

All tests use in-memory or tmp_path sqlite — NEVER touch the real DB.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))

# Reference datetime: 2026-06-10 UTC (today per task context)
NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def _make_today_backup(backup_dir: Path) -> Path:
    """Create a fake backup whose mtime matches the injected NOW.

    _today_backup_exists() compares the file's mtime DATE against now.date().
    A freshly written file carries the REAL clock's date, so tests written
    with a pinned NOW silently break the day after they are authored unless
    the mtime is pinned too (latent failure caught 2026-06-11).
    """
    p = backup_dir / "local_rag-daily.db"
    p.write_bytes(b"fake backup")
    ts = NOW.timestamp()
    os.utime(p, (ts, ts))
    return p

# ---------------------------------------------------------------------------
# Test DB helpers — replicate exact local_rag.py schema
# ---------------------------------------------------------------------------

_CREATE_VECTORS = """
CREATE TABLE IF NOT EXISTS vectors (
    ns    TEXT NOT NULL,
    id    TEXT NOT NULL,
    text  TEXT NOT NULL,
    meta  TEXT NOT NULL,
    vec   BLOB NOT NULL,
    PRIMARY KEY (ns, id)
)
"""


def _make_db(path: Path) -> sqlite3.Connection:
    """Create a test DB with the vectors table (local_rag.py schema)."""
    conn = sqlite3.connect(str(path))
    conn.execute(_CREATE_VECTORS)
    conn.commit()
    return conn


def _fake_vec() -> bytes:
    """Minimal non-empty bytes blob (not a real embedding)."""
    import struct
    return struct.pack("4f", 0.1, 0.2, 0.3, 0.4)


def _insert(conn: sqlite3.Connection, ns: str, vid: str,
            meta_type: str, date_str: str, text: str = "test") -> None:
    meta = json.dumps({"type": meta_type, "date": date_str, "ttl_days": 90})
    conn.execute(
        "INSERT OR REPLACE INTO vectors (ns, id, text, meta, vec) VALUES (?,?,?,?,?)",
        (ns, vid, text, meta, _fake_vec()),
    )
    conn.commit()


def _make_tracker(path: Path, recalled_ids: list[str]) -> None:
    """Write a minimal recall-tracker.json with the given IDs as hit."""
    data = {vid: {"hit_count": 1, "namespace": "test", "avg_score": 0.9,
                  "last_recalled": NOW.isoformat()}
            for vid in recalled_ids}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# find_archivable tests
# ---------------------------------------------------------------------------

def test_find_archivable_identifies_stale_learning(tmp_path):
    from memory_archiver import find_archivable
    db = tmp_path / "test.db"
    conn = _make_db(db)
    old_date = (NOW - timedelta(days=200)).date().isoformat()
    _insert(conn, "ns1", "v1", "learning", old_date)

    results = find_archivable(conn, recall_data=set(), older_than_days=180, now=NOW)
    conn.close()
    ids = [r["id"] for r in results]
    assert "v1" in ids


def test_find_archivable_skips_recent():
    """Vectors newer than threshold must not be returned."""
    from memory_archiver import find_archivable
    conn = sqlite3.connect(":memory:")
    conn.execute(_CREATE_VECTORS)
    recent_date = (NOW - timedelta(days=30)).date().isoformat()
    _insert(conn, "ns1", "v_recent", "learning", recent_date)
    results = find_archivable(conn, recall_data=set(), older_than_days=180, now=NOW)
    conn.close()
    assert not results


def test_find_archivable_skips_recalled():
    """Vectors with hits in tracker must be skipped."""
    from memory_archiver import find_archivable
    conn = sqlite3.connect(":memory:")
    conn.execute(_CREATE_VECTORS)
    old_date = (NOW - timedelta(days=300)).date().isoformat()
    _insert(conn, "ns1", "hot_vec", "learning", old_date)
    results = find_archivable(conn, recall_data={"hot_vec"}, older_than_days=180, now=NOW)
    conn.close()
    assert not results


def test_find_archivable_skips_protected_types():
    """promoted / decision / gotcha must never be returned, even if stale."""
    from memory_archiver import find_archivable
    conn = sqlite3.connect(":memory:")
    conn.execute(_CREATE_VECTORS)
    old_date = (NOW - timedelta(days=400)).date().isoformat()
    for t in ("promoted", "decision", "gotcha", "antipattern", "pattern"):
        _insert(conn, "ns1", f"v_{t}", t, old_date)
    results = find_archivable(conn, recall_data=set(), older_than_days=180, now=NOW)
    conn.close()
    assert not results


def test_find_archivable_mixed_returns_only_eligible():
    """Mix of eligible and ineligible rows — only learning+old+unreached returned."""
    from memory_archiver import find_archivable
    conn = sqlite3.connect(":memory:")
    conn.execute(_CREATE_VECTORS)
    old = (NOW - timedelta(days=300)).date().isoformat()
    recent = (NOW - timedelta(days=10)).date().isoformat()
    _insert(conn, "ns1", "eligible", "learning", old)
    _insert(conn, "ns1", "too_recent", "learning", recent)
    _insert(conn, "ns1", "protected", "promoted", old)
    _insert(conn, "ns1", "recalled", "learning", old)
    results = find_archivable(conn, recall_data={"recalled"}, older_than_days=180, now=NOW)
    conn.close()
    ids = {r["id"] for r in results}
    assert ids == {"eligible"}


# ---------------------------------------------------------------------------
# archive DRY mode tests
# ---------------------------------------------------------------------------

def test_archive_dry_mode_counts_no_db_change(tmp_path):
    """DRY run (apply=False) must not modify the DB."""
    from memory_archiver import archive
    db = tmp_path / "rag.db"
    conn = _make_db(db)
    old = (NOW - timedelta(days=300)).date().isoformat()
    _insert(conn, "ns1", "v1", "learning", old)
    conn.close()

    tracker = tmp_path / "tracker.json"
    _make_tracker(tracker, recalled_ids=[])  # v1 not recalled

    result = archive(db_path=db, recall_path=tracker, older_than_days=180,
                     apply=False, now=NOW)

    assert result["applied"] is False
    assert result["archivable"] >= 1

    # DB unchanged
    conn2 = sqlite3.connect(str(db))
    count = conn2.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    conn2.close()
    assert count == 1


def test_archive_dry_namespace_breakdown(tmp_path):
    from memory_archiver import archive
    db = tmp_path / "rag.db"
    conn = _make_db(db)
    old = (NOW - timedelta(days=300)).date().isoformat()
    _insert(conn, "alpha", "a1", "learning", old)
    _insert(conn, "alpha", "a2", "learning", old)
    _insert(conn, "beta", "b1", "learning", old)
    conn.close()

    tracker = tmp_path / "tracker.json"
    _make_tracker(tracker, [])

    result = archive(db_path=db, recall_path=tracker, older_than_days=180,
                     apply=False, now=NOW)
    assert result["by_namespace"]["alpha"] == 2
    assert result["by_namespace"]["beta"] == 1


def test_archive_missing_db_returns_zeros(tmp_path):
    from memory_archiver import archive
    db = tmp_path / "nonexistent.db"
    tracker = tmp_path / "tracker.json"
    _make_tracker(tracker, [])
    result = archive(db_path=db, recall_path=tracker, apply=False, now=NOW)
    assert result["examined"] == 0
    assert result["archivable"] == 0


# ---------------------------------------------------------------------------
# archive --apply with backup gate
# ---------------------------------------------------------------------------

def test_archive_apply_blocked_without_backup(tmp_path):
    """apply=True must refuse when no today's backup exists."""
    from memory_archiver import archive
    import memory_archiver as ma

    db = tmp_path / "rag.db"
    conn = _make_db(db)
    old = (NOW - timedelta(days=300)).date().isoformat()
    _insert(conn, "ns1", "v1", "learning", old)
    conn.close()

    tracker = tmp_path / "tracker.json"
    _make_tracker(tracker, [])

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    # Patch BACKUP_DIR so no backup is found
    original_backup_dir = ma.BACKUP_DIR
    ma.BACKUP_DIR = backup_dir
    try:
        result = archive(db_path=db, recall_path=tracker, older_than_days=180,
                         apply=True, now=NOW)
        # Must NOT have applied
        assert result["applied"] is False
    finally:
        ma.BACKUP_DIR = original_backup_dir


def test_archive_apply_with_backup_moves_rows(tmp_path):
    """With a today's backup present, apply=True must move rows atomically."""
    from memory_archiver import archive
    import memory_archiver as ma

    db = tmp_path / "rag.db"
    conn = _make_db(db)
    old = (NOW - timedelta(days=300)).date().isoformat()
    _insert(conn, "ns1", "v1", "learning", old)
    _insert(conn, "ns1", "v2", "promoted", old)  # protected — must stay
    conn.close()

    tracker = tmp_path / "tracker.json"
    _make_tracker(tracker, [])  # nothing recalled

    # Create a fake backup dated to the injected NOW (mtime pinned)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _make_today_backup(backup_dir)

    original_backup_dir = ma.BACKUP_DIR
    ma.BACKUP_DIR = backup_dir
    try:
        result = archive(db_path=db, recall_path=tracker, older_than_days=180,
                         apply=True, now=NOW)
    finally:
        ma.BACKUP_DIR = original_backup_dir

    assert result["applied"] is True
    assert result.get("archived", 0) == 1  # only v1 moved

    conn2 = sqlite3.connect(str(db))
    remaining = conn2.execute("SELECT id FROM vectors").fetchall()
    archived = conn2.execute("SELECT id FROM vectors_archive").fetchall()
    conn2.close()

    remaining_ids = {r[0] for r in remaining}
    archived_ids = {r[0] for r in archived}

    assert "v1" in archived_ids
    assert "v2" in remaining_ids  # protected type stayed


def test_archive_apply_protected_types_never_archived(tmp_path):
    """Even if requested via types param, protected types must not be archived."""
    from memory_archiver import archive
    import memory_archiver as ma

    db = tmp_path / "rag.db"
    conn = _make_db(db)
    old = (NOW - timedelta(days=400)).date().isoformat()
    for t in ("promoted", "decision", "gotcha"):
        _insert(conn, "ns1", f"v_{t}", t, old)
    conn.close()

    tracker = tmp_path / "tracker.json"
    _make_tracker(tracker, [])

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _make_today_backup(backup_dir)

    original = ma.BACKUP_DIR
    ma.BACKUP_DIR = backup_dir
    try:
        # Even requesting promoted/decision explicitly
        result = archive(db_path=db, recall_path=tracker,
                         types=("promoted", "decision", "gotcha"),
                         older_than_days=180, apply=True, now=NOW)
    finally:
        ma.BACKUP_DIR = original

    # No rows should have been archived
    conn2 = sqlite3.connect(str(db))
    count = conn2.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    arch_tbl = conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vectors_archive'"
    ).fetchone()
    conn2.close()
    assert count == 3  # all still there (archive table may not even exist)


# ---------------------------------------------------------------------------
# restore tests
# ---------------------------------------------------------------------------

def test_restore_moves_back(tmp_path):
    """Rows in archive must be moved back to vectors by restore()."""
    from memory_archiver import archive, restore
    import memory_archiver as ma

    db = tmp_path / "rag.db"
    conn = _make_db(db)
    old = (NOW - timedelta(days=300)).date().isoformat()
    _insert(conn, "ns1", "v1", "learning", old)
    conn.close()

    tracker = tmp_path / "tracker.json"
    _make_tracker(tracker, [])

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _make_today_backup(backup_dir)

    original = ma.BACKUP_DIR
    ma.BACKUP_DIR = backup_dir
    try:
        archive(db_path=db, recall_path=tracker, apply=True, now=NOW)
        n = restore(["v1"], db_path=db)
    finally:
        ma.BACKUP_DIR = original

    assert n == 1
    conn2 = sqlite3.connect(str(db))
    back = conn2.execute("SELECT id FROM vectors WHERE id='v1'").fetchone()
    still_archived = conn2.execute(
        "SELECT id FROM vectors_archive WHERE id='v1'"
    ).fetchone()
    conn2.close()
    assert back is not None
    assert still_archived is None


def test_restore_nonexistent_id_returns_zero(tmp_path):
    from memory_archiver import restore
    db = tmp_path / "rag.db"
    conn = _make_db(db)
    conn.close()
    n = restore(["ghost-id"], db_path=db)
    assert n == 0


def test_restore_empty_list_returns_zero(tmp_path):
    from memory_archiver import restore
    db = tmp_path / "rag.db"
    conn = _make_db(db)
    conn.close()
    assert restore([], db_path=db) == 0


# ---------------------------------------------------------------------------
# Tracker loading edge cases
# ---------------------------------------------------------------------------

def test_load_tracker_missing_file(tmp_path):
    """Missing tracker must return empty set (not error)."""
    from memory_archiver import _load_tracker
    result = _load_tracker(tmp_path / "nonexistent.json")
    assert result == set()


def test_load_tracker_corrupt_file(tmp_path):
    from memory_archiver import _load_tracker
    p = tmp_path / "tracker.json"
    p.write_text("{bad json{{", encoding="utf-8")
    result = _load_tracker(p)
    assert result == set()


def test_load_tracker_zero_hitcount_not_recalled(tmp_path):
    """Entries with hit_count=0 must NOT appear in recalled set."""
    from memory_archiver import _load_tracker
    p = tmp_path / "tracker.json"
    data = {"v1": {"hit_count": 0, "namespace": "x", "avg_score": 0.0}}
    p.write_text(json.dumps(data), encoding="utf-8")
    result = _load_tracker(p)
    assert "v1" not in result
