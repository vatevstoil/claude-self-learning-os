#!/usr/bin/env python3
"""recall_tracker.py -- Log Pinecone query hits for Hebbian memory reinforcement.

Each recalled memory gets its hit_count incremented. The dreaming pass uses
this for Hebbian TTL: effective_ttl = base_ttl x (1 + recall_count).

Usage (standalone):
    python recall_tracker.py   # prints current stats from default log path

Programmatic:
    from recall_tracker import log_hit, log_hits
    log_hit("my-namespace", "vector-id-123", score=0.92)
    log_hits("my-namespace", matches)   # matches = list of dicts with 'id' and 'score'
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

DEFAULT_PATH = Path.home() / ".claude" / "logs" / "recall-tracker.json"


def _load(path: Path) -> dict:
    """Load tracker JSON file, returning empty dict on any failure."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(data: dict, path: Path) -> None:
    """Persist tracker data atomically (write-then-replace, never partial write)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False, indent=2))
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception:
        pass


def log_hit(
    namespace: str,
    vector_id: str,
    score: float,
    tracker_path: Path = DEFAULT_PATH,
) -> None:
    """Record one recall hit for a single vector ID.

    Args:
        namespace: Pinecone namespace the vector belongs to.
        vector_id: The unique vector ID returned by Pinecone.
        score: Similarity score from the query (0.0 - 1.0).
        tracker_path: Path to the JSON tracker file. Defaults to
            ``~/.claude/logs/recall-tracker.json``.
    """
    data = _load(tracker_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = data.get(
        vector_id,
        {
            "hit_count": 0,
            "namespace": namespace,
            "avg_score": 0.0,
            "last_recalled": now,
        },
    )
    n = entry["hit_count"]
    entry["avg_score"] = round((entry["avg_score"] * n + score) / (n + 1), 4)
    entry["hit_count"] = n + 1
    entry["last_recalled"] = now
    entry["namespace"] = namespace
    data[vector_id] = entry
    _save(data, tracker_path)


def log_hits(
    namespace: str,
    matches: Sequence[dict],
    tracker_path: Path = DEFAULT_PATH,
) -> None:
    """Record all hits from a single Pinecone query response (batch version).

    Silently skips entries without a valid ``id`` field. Never raises.

    Args:
        namespace: Pinecone namespace the vectors belong to.
        matches: List of match dicts, each with ``id`` and ``score`` keys
            (same shape as Pinecone query response ``matches`` list).
        tracker_path: Path to the JSON tracker file. Defaults to
            ``~/.claude/logs/recall-tracker.json``.
    """
    if not matches:
        return
    data = _load(tracker_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changed = False
    for m in matches:
        vid = m.get("id") or m.get("vector_id", "")
        score = float(m.get("score", 0.0))
        if not vid:
            continue
        entry = data.get(
            vid,
            {
                "hit_count": 0,
                "namespace": namespace,
                "avg_score": 0.0,
                "last_recalled": now,
            },
        )
        n = entry["hit_count"]
        entry["avg_score"] = round((entry["avg_score"] * n + score) / (n + 1), 4)
        entry["hit_count"] = n + 1
        entry["last_recalled"] = now
        entry["namespace"] = namespace
        data[vid] = entry
        changed = True
    if changed:
        _save(data, tracker_path)


if __name__ == "__main__":
    data = _load(DEFAULT_PATH)
    if not data:
        print(f"No recall data yet at {DEFAULT_PATH}")
    else:
        print(f"Recall tracker — {len(data)} vectors tracked ({DEFAULT_PATH})")
        sorted_entries = sorted(
            data.items(), key=lambda kv: kv[1].get("hit_count", 0), reverse=True
        )
        for vid, entry in sorted_entries[:20]:
            print(
                f"  [{entry['hit_count']:>3}x] score={entry['avg_score']:.4f}"
                f"  ns={entry['namespace']}  id={vid}"
            )
