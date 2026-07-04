"""Tests for incident_fix_proposer.py — human-gated fix-session drafting."""
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


def _incidents(*items):
    return {"generated": NOW.isoformat(), "open": list(items), "resolved_recent": []}


def _inc(iid, count, project="Trading", title="терминал изскача", examples=None):
    return {"id": iid, "project": project, "title": title, "count": count,
            "examples": examples or ["терминал изскача отново", "пак изскача"],
            "status": "open", "resolution": None}


# ── build_prompt ──────────────────────────────────────────────────────────────

def test_build_prompt_is_self_contained():
    from incident_fix_proposer import build_prompt
    p = build_prompt(_inc("a1", 5, project="MyProj", title="X breaks"))
    assert "MyProj" in p
    assert "терминал изскача отново" in p  # verbatim example embedded
    assert "ROOT CAUSE" in p
    assert "network" in p  # discipline reminder present


# ── blocklist / provenance / accept (2026-06-13 governance) ─────────────────

def test_blocked_project_never_proposed():
    from incident_fix_proposer import propose_fixes, pending
    doc = _incidents(
        _inc("t1", 9, project="J--Obsidian-Resurch-Claude-Trading"),
        _inc("r1", 9, project="J--Antigraviti-Reed"),
        _inc("ok1", 9, project="Facturka"),
    )
    out = propose_fixes(doc, existing={}, now=NOW)
    ids = {p["id"] for p in pending(out)}
    assert ids == {"ok1"}  # Trading + Reed excluded


def test_blocked_prior_proposed_is_suppressed():
    from incident_fix_proposer import propose_fixes
    prior = {"proposals": [{"id": "t1", "project": "J--Antigraviti-Reed",
                            "status": "proposed", "count": 5}]}
    doc = _incidents(_inc("t1", 9, project="J--Antigraviti-Reed"))
    out = propose_fixes(doc, existing=prior, now=NOW)
    entry = next(p for p in out["proposals"] if p["id"] == "t1")
    assert entry["status"] == "suppressed"


def test_corrupt_accepted_provenance_demoted():
    from incident_fix_proposer import propose_fixes
    # 'accepted' with NO valid accepted_by_user = hand-injected -> demote so the
    # human sees it again (here a non-blocked project so it re-surfaces).
    prior = {"proposals": [{"id": "x1", "project": "Facturka",
                            "status": "accepted", "count": 5}]}
    doc = _incidents(_inc("x1", 9, project="Facturka"))
    out = propose_fixes(doc, existing=prior, now=NOW)
    entry = next(p for p in out["proposals"] if p["id"] == "x1")
    assert entry["status"] == "proposed"


def test_valid_accepted_provenance_preserved():
    from incident_fix_proposer import propose_fixes
    prior = {"proposals": [{"id": "x1", "project": "Facturka", "status": "accepted",
                            "accepted_by_user": "2026-06-11", "count": 5}]}
    doc = _incidents(_inc("x1", 9, project="Facturka"))
    out = propose_fixes(doc, existing=prior, now=NOW)
    entry = next(p for p in out["proposals"] if p["id"] == "x1")
    assert entry["status"] == "accepted"  # genuine provenance kept


def test_accept_sets_tracked_provenance():
    from incident_fix_proposer import accept
    doc = {"proposals": [{"id": "x1", "project": "Facturka", "status": "proposed"}]}
    assert accept(doc, "x1", actor="{{PRIVATE_NS}}", now=NOW) is True
    p = doc["proposals"][0]
    assert p["status"] == "accepted"
    assert p["accepted_by_user"] == NOW.date().isoformat()
    assert p["accepted_actor"] == "{{PRIVATE_NS}}"
    assert accept(doc, "missing", now=NOW) is False


# ── propose_fixes: threshold + dedup + cap ────────────────────────────────────

def test_proposes_only_above_min_count():
    from incident_fix_proposer import propose_fixes, pending
    doc = _incidents(_inc("weak", 2), _inc("strong", 6))
    out = propose_fixes(doc, now=NOW, min_count=4)
    ids = {p["id"] for p in pending(out)}
    assert "strong" in ids
    assert "weak" not in ids


def test_existing_proposal_not_duplicated():
    from incident_fix_proposer import propose_fixes
    doc = _incidents(_inc("i1", 5))
    first = propose_fixes(doc, now=NOW)
    second = propose_fixes(doc, existing=first, now=NOW + timedelta(days=1))
    assert sum(p["id"] == "i1" for p in second["proposals"]) == 1


def test_acknowledged_not_reproposed():
    from incident_fix_proposer import propose_fixes, acknowledge, pending
    doc = _incidents(_inc("i1", 5))
    first = propose_fixes(doc, now=NOW)
    acknowledge(first, "i1")
    # Same incident still open next run — must NOT reappear as pending.
    second = propose_fixes(doc, existing=first, now=NOW + timedelta(days=1))
    assert "i1" not in {p["id"] for p in pending(second)}
    # but the record is preserved as acknowledged
    assert any(p["id"] == "i1" and p["status"] == "acknowledged"
               for p in second["proposals"])


def test_max_new_per_run_caps_output():
    from incident_fix_proposer import propose_fixes, pending
    doc = _incidents(*[_inc(f"i{i}", 5 + i) for i in range(10)])
    out = propose_fixes(doc, now=NOW, max_new=3)
    assert len(pending(out)) == 3


def test_cap_keeps_strongest_incidents():
    from incident_fix_proposer import propose_fixes, pending
    doc = _incidents(_inc("low", 4), _inc("high", 20), _inc("mid", 9))
    out = propose_fixes(doc, now=NOW, max_new=1)
    ids = {p["id"] for p in pending(out)}
    assert ids == {"high"}


def test_empty_incidents_yields_no_proposals():
    from incident_fix_proposer import propose_fixes, pending
    out = propose_fixes({"open": []}, now=NOW)
    assert pending(out) == []


def test_tolerates_missing_fields():
    from incident_fix_proposer import propose_fixes
    # incident with no id is skipped, not crashed on
    doc = {"open": [{"project": "X", "count": 9}]}
    out = propose_fixes(doc, now=NOW)
    assert out["proposals"] == []


# ── acknowledge ───────────────────────────────────────────────────────────────

def test_acknowledge_unknown_returns_false():
    from incident_fix_proposer import acknowledge
    assert acknowledge({"proposals": []}, "ghost") is False


# ── CLI round-trip ────────────────────────────────────────────────────────────

def test_cmd_refresh_writes_file(tmp_path):
    from incident_fix_proposer import cmd_refresh, _load_json, pending
    inc_p = tmp_path / "incidents.json"
    inc_p.write_text(json.dumps(_incidents(_inc("i1", 7))), encoding="utf-8")
    prop_p = tmp_path / "fix-proposals.json"
    n = cmd_refresh(incidents_path=inc_p, proposals_path=prop_p, now=NOW)
    assert n == 1
    doc = _load_json(prop_p, {})
    assert pending(doc)[0]["id"] == "i1"
    assert "prompt" in pending(doc)[0]


# ── threshold-alignment regression tests (DEFAULT_MIN_COUNT == 3) ─────────────

def test_count_3_generates_proposal():
    """count=3 must produce a proposal — matches incident_tracker's open threshold."""
    from incident_fix_proposer import propose_fixes, pending, DEFAULT_MIN_COUNT
    assert DEFAULT_MIN_COUNT == 3, "threshold mismatch — update this test if changed deliberately"
    doc = _incidents(_inc("reed-001", 3, project="Reed", title="TTS замлъква"))
    out = propose_fixes(doc, now=NOW)
    ids = {p["id"] for p in pending(out)}
    assert "reed-001" in ids, "count=3 incident must be proposed"


def test_count_2_no_proposal():
    """count=2 is below the threshold and must NOT generate a proposal."""
    from incident_fix_proposer import propose_fixes, pending
    doc = _incidents(_inc("weak-001", 2, project="Reed", title="рядък проблем"))
    out = propose_fixes(doc, now=NOW)
    assert pending(out) == [], "count=2 incident must not be proposed"


def test_ack_via_cmd_refresh_roundtrip(tmp_path):
    """Ack via --ack flag persists; next cmd_refresh does not re-propose."""
    from incident_fix_proposer import cmd_refresh, acknowledge, _load_json, _atomic_write, pending
    inc_p = tmp_path / "incidents.json"
    inc_p.write_text(json.dumps(_incidents(_inc("i-ack", 3, project="Claude-Trading"))),
                     encoding="utf-8")
    prop_p = tmp_path / "fix-proposals.json"
    # First run — should propose
    n1 = cmd_refresh(incidents_path=inc_p, proposals_path=prop_p, now=NOW)
    assert n1 == 1
    # Simulate --ack
    doc = _load_json(prop_p, {})
    found = acknowledge(doc, "i-ack")
    assert found is True
    _atomic_write(prop_p, doc)
    # Second run — same incident still open; must NOT re-appear as pending
    n2 = cmd_refresh(incidents_path=inc_p, proposals_path=prop_p, now=NOW)
    assert n2 == 0, "acknowledged proposal must not re-appear as pending"


# ── incidents-status-desync: sync_status_to_state ────────────────────────────

def test_sync_status_to_state_accept(tmp_path):
    """Accepting a proposal writes proposal_status=accepted back to incidents-state.json."""
    from incident_fix_proposer import sync_status_to_state, _load_json
    state_p = tmp_path / "incidents-state.json"
    state_p.write_text(json.dumps({"clusters": [
        {"id": "x1", "project": "Facturka", "status": "open", "examples": []}
    ]}), encoding="utf-8")
    proposal = {"id": "x1", "status": "accepted", "project": "Facturka"}
    result = sync_status_to_state(proposal, state_path=state_p, now=NOW)
    assert result is True
    state = _load_json(state_p, {})
    cluster = next(c for c in state["clusters"] if c["id"] == "x1")
    assert cluster["proposal_status"] == "accepted"
    assert "proposal_synced_at" in cluster


def test_sync_status_to_state_missing_cluster(tmp_path):
    """sync_status_to_state returns False when cluster id not found."""
    from incident_fix_proposer import sync_status_to_state
    state_p = tmp_path / "incidents-state.json"
    state_p.write_text(json.dumps({"clusters": []}), encoding="utf-8")
    proposal = {"id": "ghost", "status": "accepted"}
    assert sync_status_to_state(proposal, state_path=state_p, now=NOW) is False


def test_sync_status_to_state_non_terminal_skipped(tmp_path):
    """sync_status_to_state ignores non-terminal statuses (e.g. 'proposed')."""
    from incident_fix_proposer import sync_status_to_state
    state_p = tmp_path / "incidents-state.json"
    state_p.write_text(json.dumps({"clusters": [
        {"id": "y1", "project": "X", "status": "open", "examples": []}
    ]}), encoding="utf-8")
    proposal = {"id": "y1", "status": "proposed"}
    assert sync_status_to_state(proposal, state_path=state_p, now=NOW) is False
