import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def test_compute_hebbian_ttl_basic():
    """Formula: int(base * (1 + log2(1 + hits))), capped at NEVER_EXPIRE.
    recall_count=0: log2(1)=0.0 -> base unchanged.
    recall_count=1: log2(2)=1.0 -> base*2.
    recall_count=2: log2(3)~=1.585 -> int(90*2.585)=232."""
    import math
    from hebbian_consolidation import compute_hebbian_ttl
    assert compute_hebbian_ttl(base_ttl=90, recall_count=0) == 90
    assert compute_hebbian_ttl(base_ttl=90, recall_count=1) == int(90 * (1 + math.log2(2)))
    assert compute_hebbian_ttl(base_ttl=90, recall_count=2) == int(90 * (1 + math.log2(3)))


def test_compute_hebbian_ttl_cap():
    """High recall counts must still be capped at NEVER_EXPIRE.
    With log2 damping, only extremely large bases or huge hit counts reach cap.
    base_ttl=9999 (promoted) caps immediately at recall_count=1."""
    from hebbian_consolidation import compute_hebbian_ttl, NEVER_EXPIRE
    # promoted (base=9999) caps on first recall
    assert compute_hebbian_ttl(base_ttl=9999, recall_count=1) == NEVER_EXPIRE
    # Any count must never exceed NEVER_EXPIRE
    for hits in (0, 1, 10, 100, 1000):
        assert compute_hebbian_ttl(365, hits) <= NEVER_EXPIRE
    # Moderate count grows but stays well below cap
    result = compute_hebbian_ttl(base_ttl=365, recall_count=10)
    assert 365 < result <= NEVER_EXPIRE


def test_compute_hebbian_ttl_already_never_expire():
    from hebbian_consolidation import compute_hebbian_ttl
    # NEVER_EXPIRE sentinel (9999) must stay 9999 regardless of recall_count
    assert compute_hebbian_ttl(base_ttl=9999, recall_count=5) == 9999


def test_get_high_recall_ids(tmp_path):
    from hebbian_consolidation import get_high_recall_ids
    tracker = tmp_path / "recall-tracker.json"
    tracker.write_text(json.dumps({
        "id-frequent": {"hit_count": 5, "namespace": "ns1", "avg_score": 0.85, "last_recalled": "2026-01-01T00:00:00+00:00"},
        "id-rare": {"hit_count": 1, "namespace": "ns1", "avg_score": 0.7, "last_recalled": "2026-01-01T00:00:00+00:00"},
    }), encoding="utf-8")
    ids = get_high_recall_ids(tracker_path=tracker, min_count=2)
    assert "id-frequent" in ids
    assert "id-rare" not in ids


def test_get_high_recall_ids_empty_file(tmp_path):
    from hebbian_consolidation import get_high_recall_ids
    # Non-existent file -> returns empty dict
    ids = get_high_recall_ids(tracker_path=tmp_path / "missing.json", min_count=2)
    assert ids == {}


def test_load_salience_missing_file(tmp_path):
    from hebbian_consolidation import load_salience
    result = load_salience(tmp_path / "missing.json")
    assert result == {}


def test_load_salience_parses_scores(tmp_path):
    from hebbian_consolidation import load_salience
    p = tmp_path / "salience.json"
    p.write_text(json.dumps({
        "sess-1": {"score": 0.9, "markers": ["SECURITY"], "project": "P", "snippet": "auth bug"},
        "sess-2": {"score": 0.3, "markers": ["ERRORS"], "project": "P", "snippet": "error"},
    }), encoding="utf-8")
    result = load_salience(p)
    assert result == {"sess-1": 0.9, "sess-2": 0.3}


def test_load_salience_tolerates_corrupt(tmp_path):
    from hebbian_consolidation import load_salience
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    assert load_salience(p) == {}


def test_apply_hebbian_salience_bonus_high(tmp_path):
    """score >= 0.8 adds +2 to recall_count -> higher TTL than without bonus.
    With log2 formula: bonus adds more hits, increasing log2(1+hits).
    Critical: bonus result must be strictly > no-bonus result."""
    import math
    from hebbian_consolidation import compute_hebbian_ttl, _SALIENCE_BONUS_HIGH

    base = 90
    hits_no_bonus = 1
    hits_with_bonus = 1 + _SALIENCE_BONUS_HIGH  # 3
    ttl_no_bonus = compute_hebbian_ttl(base, hits_no_bonus)
    ttl_with_bonus = compute_hebbian_ttl(base, hits_with_bonus)
    assert ttl_with_bonus > ttl_no_bonus, "salience bonus must increase TTL"
    # Exact values with log2 formula
    assert ttl_no_bonus == int(base * (1 + math.log2(2)))    # hits=1
    assert ttl_with_bonus == int(base * (1 + math.log2(4)))  # hits=3


def test_apply_hebbian_salience_bonus_mid():
    """score 0.5-0.79 adds +1 to recall_count -> TTL between no-bonus and high-bonus."""
    import math
    from hebbian_consolidation import compute_hebbian_ttl, _SALIENCE_BONUS_MID
    base = 90
    hits_with_mid = 1 + _SALIENCE_BONUS_MID  # 2
    expected = int(base * (1 + math.log2(1 + hits_with_mid)))  # log2(3)
    assert compute_hebbian_ttl(base, hits_with_mid) == expected


# ---------------------------------------------------------------------------
# NEW TESTS — Hebbian floor + log2 + remediation (Bug fixes 2026-06-10)
# ---------------------------------------------------------------------------

import math as _math


def test_compute_hebbian_ttl_log2_formula():
    """New formula: base * (1 + log2(1 + hits)); must differ from old linear."""
    from hebbian_consolidation import compute_hebbian_ttl
    # hits=3: log2(4)=2.0  -> 90*(1+2.0)=270
    assert compute_hebbian_ttl(90, 3) == 270
    # hits=7: log2(8)=3.0  -> 180*(1+3.0)=720
    assert compute_hebbian_ttl(180, 7) == 720
    # hits=1: log2(2)=1.0  -> 365*(1+1.0)=730
    assert compute_hebbian_ttl(365, 1) == 730


def test_compute_hebbian_ttl_log2_no_ratchet_at_26_hits():
    """Old linear formula: 365*(1+26)=9855 reached near-NEVER in 1 pass.
    New log2 formula: 365*(1+log2(27))~=365*5.75=2099 — safe progression."""
    from hebbian_consolidation import compute_hebbian_ttl, NEVER_EXPIRE
    result = compute_hebbian_ttl(365, 26)
    # Must NOT reach NEVER_EXPIRE in one weekly run with 26 hits
    assert result < NEVER_EXPIRE
    # Must still grow compared to base
    assert result > 365
    # Exact expected value: 365 * (1 + log2(27)) = 365 * 5.7549... = 2100 (int truncation)
    expected = int(365 * (1 + _math.log2(27)))
    assert result == expected


def test_get_high_recall_ids_floor_filters_low_score(tmp_path):
    """Vectors below MIN_COSINE_SCORE (0.60) must be excluded even if hit_count is high."""
    from hebbian_consolidation import get_high_recall_ids
    tracker = tmp_path / "recall-tracker.json"
    tracker.write_text(json.dumps({
        "noise-id": {"hit_count": 33, "namespace": "ns1", "avg_score": 0.434,
                     "last_recalled": "2026-01-01T00:00:00+00:00"},
        "quality-id": {"hit_count": 5, "namespace": "ns1", "avg_score": 0.72,
                       "last_recalled": "2026-01-01T00:00:00+00:00"},
    }), encoding="utf-8")
    ids = get_high_recall_ids(tracker_path=tracker, min_count=2)
    assert "noise-id" not in ids, "low-score high-count vector must be excluded (noise)"
    assert "quality-id" in ids


def test_get_high_recall_ids_exact_floor_boundary(tmp_path):
    """avg_score == MIN_COSINE_SCORE must pass; just below must fail."""
    from hebbian_consolidation import get_high_recall_ids, MIN_COSINE_SCORE
    tracker = tmp_path / "recall-tracker.json"
    tracker.write_text(json.dumps({
        "on-boundary": {"hit_count": 3, "namespace": "ns1",
                        "avg_score": MIN_COSINE_SCORE,
                        "last_recalled": "2026-01-01T00:00:00+00:00"},
        "below-boundary": {"hit_count": 3, "namespace": "ns1",
                           "avg_score": round(MIN_COSINE_SCORE - 0.001, 4),
                           "last_recalled": "2026-01-01T00:00:00+00:00"},
    }), encoding="utf-8")
    ids = get_high_recall_ids(tracker_path=tracker, min_count=2)
    assert "on-boundary" in ids
    assert "below-boundary" not in ids


def test_get_high_recall_ids_missing_avg_score_passes(tmp_path):
    """When avg_score is absent, default is 1.0 (pass-through) so old tracker
    entries without the field continue to work."""
    from hebbian_consolidation import get_high_recall_ids
    tracker = tmp_path / "recall-tracker.json"
    tracker.write_text(json.dumps({
        "old-entry": {"hit_count": 4, "namespace": "ns1",
                      "last_recalled": "2026-01-01T00:00:00+00:00"},
    }), encoding="utf-8")
    ids = get_high_recall_ids(tracker_path=tracker, min_count=2)
    assert "old-entry" in ids


def test_type_base_ttl_known_types():
    """Canonical base TTL values must match spec."""
    from hebbian_consolidation import type_base_ttl
    assert type_base_ttl("learning") == 90
    assert type_base_ttl("gotcha") == 180
    assert type_base_ttl("decision") == 365
    assert type_base_ttl("pattern") == 365
    assert type_base_ttl("antipattern") == 365
    assert type_base_ttl("promoted") == 9999


def test_type_base_ttl_unknown_returns_default():
    from hebbian_consolidation import type_base_ttl, DEFAULT_BASE_TTL
    assert type_base_ttl("raw_passage") == DEFAULT_BASE_TTL
    assert type_base_ttl("") == DEFAULT_BASE_TTL
    assert type_base_ttl(None) == DEFAULT_BASE_TTL


def test_apply_hebbian_uses_type_base_not_inflated(tmp_path, monkeypatch):
    """apply_hebbian_ttls must use type_base_ttl(), not stored ttl_days.
    An antipattern with inflated ttl=3285 should reset computation to base=365."""
    import sys, os
    sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))
    import sqlite3, numpy as np
    import hebbian_consolidation as hc

    # Build a minimal local_rag-like sqlite db in tmp_path
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE vectors (ns TEXT, id TEXT, text TEXT, meta TEXT, vec BLOB)"
    )
    # Vector with inflated TTL (antipattern should be 365, not 3285)
    meta = json.dumps({
        "type": "antipattern", "ttl_days": 3285,
        "hebbian_updated": "2026-06-06T08:00:00+00:00"
    })
    vec = np.ones(4, dtype=np.float32).tobytes()
    conn.execute(
        "INSERT INTO vectors VALUES (?,?,?,?,?)",
        ("Fakturka.bg", "inflated-vec", "text", meta, vec)
    )
    conn.commit()
    conn.close()

    # Monkey-patch _local() to return a mock that wraps our test db
    class _FakeLR:
        def fetch(self, ns, ids):
            conn2 = sqlite3.connect(str(db_path))
            out = []
            for vid in ids:
                r = conn2.execute(
                    "SELECT id,meta,vec FROM vectors WHERE ns=? AND id=?", (ns, vid)
                ).fetchone()
                if r:
                    out.append({
                        "id": r[0],
                        "values": np.frombuffer(r[2], dtype=np.float32).tolist(),
                        "metadata": json.loads(r[1]) if r[1] else {},
                    })
            conn2.close()
            return out

    monkeypatch.setattr(hc, "_local", lambda: _FakeLR())
    monkeypatch.setattr(hc, "MEMORY_BACKEND", "local")

    high_recall = {
        "inflated-vec": {"hit_count": 5, "namespace": "Fakturka.bg"},
    }
    logs = hc.apply_hebbian_ttls(high_recall, apply=False)
    # The UPDATE log must show base 365, not 3285
    update_line = next((l for l in logs if l.startswith("UPDATE")), "")
    assert "ttl 365 ->" in update_line, (
        f"Expected base 365 (antipattern canonical), got: {update_line}"
    )


def test_remediate_inflated_ttls_dry_run(tmp_path, monkeypatch):
    """remediate_inflated_ttls dry-run must count inflated vectors without writing."""
    import sys, sqlite3, numpy as np
    sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))
    import hebbian_consolidation as hc

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE vectors (ns TEXT, id TEXT, text TEXT, meta TEXT, vec BLOB)"
    )
    # Two inflated vectors
    for vid, vtype, ttl in [("v1", "learning", 9999), ("v2", "antipattern", 5000)]:
        meta = json.dumps({
            "type": vtype, "ttl_days": ttl,
            "hebbian_updated": "2026-06-06T08:00:00+00:00"
        })
        vec = np.ones(4, dtype=np.float32).tobytes()
        conn.execute("INSERT INTO vectors VALUES (?,?,?,?,?)",
                     ("ns1", vid, "text", meta, vec))
    # One already-at-base vector (learning=90)
    meta3 = json.dumps({
        "type": "learning", "ttl_days": 90,
        "hebbian_updated": "2026-06-06T08:00:00+00:00"
    })
    conn.execute("INSERT INTO vectors VALUES (?,?,?,?,?)",
                 ("ns1", "v3", "text", meta3, np.ones(4, dtype=np.float32).tobytes()))
    # One promoted (must be skipped)
    meta4 = json.dumps({
        "type": "promoted", "ttl_days": 9999,
        "hebbian_updated": "2026-06-06T08:00:00+00:00"
    })
    conn.execute("INSERT INTO vectors VALUES (?,?,?,?,?)",
                 ("ns1", "v4", "text", meta4, np.ones(4, dtype=np.float32).tobytes()))
    conn.commit()
    conn.close()

    class _FakeLR2:
        def all_namespaces(self):
            return ["ns1"]

        def fetch_all(self, ns):
            conn2 = sqlite3.connect(str(db_path))
            rows = conn2.execute(
                "SELECT id,meta,vec FROM vectors WHERE ns=?", (ns,)
            ).fetchall()
            conn2.close()
            return [{"id": r[0],
                     "values": np.frombuffer(r[2], dtype=np.float32).tolist(),
                     "metadata": json.loads(r[1]) if r[1] else {}} for r in rows]

        def update_meta(self, ns, vid, meta):
            return 1

    monkeypatch.setattr(hc, "_local", lambda: _FakeLR2())

    logs = hc.remediate_inflated_ttls(apply=False)
    summary = next((l for l in logs if "Would reset" in l), "")
    assert "2" in summary, f"Expected 2 would-reset, got: {summary}"
    # Promoted must not appear in logs
    assert all("v4" not in l for l in logs), "promoted vector must not be remediated"
