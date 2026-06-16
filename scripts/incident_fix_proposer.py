#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""incident_fix_proposer.py — turn recurring incidents into reviewable fix tasks.

incident_tracker clusters repeated user complaints into OPEN incidents. This
module takes the strong ones and drafts a self-contained "fix session" prompt
for each — a ready-to-launch task the human can approve and run.

SAFETY — this NEVER auto-executes anything. Spawning a session that modifies
code on its own, unsupervised, off a heuristic complaint cluster is exactly the
kind of irreversible action that must stay human-gated. The output is a DRAFT
queue (logs/fix-proposals.json); a person reviews and launches. Proposals are
deduplicated by incident id and an acknowledged/dismissed one never returns.

Pure functions + dependency injection; tolerant I/O; CLI exits 0 on non-fatal.

CLI:
    python incident_fix_proposer.py            # refresh proposals from incidents.json
    python incident_fix_proposer.py --list     # print pending proposals
    python incident_fix_proposer.py --ack <id>  # mark a proposal handled (won't return)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
except Exception:
    pass

_LOGS = Path.home() / ".claude" / "logs"
DEFAULT_INCIDENTS = _LOGS / "incidents.json"
DEFAULT_PROPOSALS = _LOGS / "fix-proposals.json"
DEFAULT_STATE = _LOGS / "incidents-state.json"

# Only propose for incidents that have genuinely recurred — a single stray
# complaint is not worth a fix session.  Matches incident_tracker's open
# threshold (>= 3 similar corrections triggers OPEN status).
DEFAULT_MIN_COUNT = 3
# Never flood: cap how many fresh proposals one run emits.
MAX_NEW_PER_RUN = 5

# Projects the user fixes directly — the proposer must NEVER surface fix
# sessions for these (their slugs as they appear verbatim in incidents.json).
BLOCKED_PROJECTS = frozenset({
    "J--Obsidian-Resurch-Claude-Trading",
    "J--Antigraviti-Reed",
})


# ---------------------------------------------------------------------------
# I/O helpers (tolerant)
# ---------------------------------------------------------------------------

def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _atomic_write(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _valid_iso_date(value: Any) -> bool:
    """True only if *value* is a non-empty string parseable as an ISO date/time.

    Used to validate ``accepted_by_user`` provenance: a status of 'accepted'
    that lacks a real timestamp was injected by hand (or corrupted) and must
    not be trusted to silently hide an incident from the human.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.strip())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_prompt(incident: dict[str, Any]) -> str:
    """Build a self-contained fix-session prompt from one incident.

    The spawned session has NO memory of this context, so the prompt embeds the
    project, the verbatim complaints, and the working discipline.
    """
    project = incident.get("project", "(unknown project)")
    title = incident.get("title", "").strip() or "(no title)"
    examples = incident.get("examples", []) or []
    count = incident.get("count", 0)

    bullets = "\n".join(f"- {str(e).strip()}" for e in examples[:5] if str(e).strip())
    return (
        f"Project: {project}\n"
        f"Recurring user complaint (seen ~{count}x): {title}\n\n"
        f"Verbatim examples:\n{bullets}\n\n"
        "Task: find the ROOT CAUSE of this recurring complaint and fix it.\n"
        "1. Reproduce / locate the failing behaviour before changing anything.\n"
        "2. Make the smallest correct fix; match the surrounding code style.\n"
        "3. Verify with evidence (run it / add a regression test) — do not claim\n"
        "   done until double-checked. Tests must not hit the network.\n"
        "4. Report what was wrong, the fix, and the verification evidence.\n"
    )


def propose_fixes(
    incidents_doc: dict[str, Any],
    existing: dict[str, Any] | None = None,
    now: datetime | None = None,
    min_count: int = DEFAULT_MIN_COUNT,
    max_new: int = MAX_NEW_PER_RUN,
) -> dict[str, Any]:
    """Compute the proposal queue from incidents.json + the existing queue.

    Args:
        incidents_doc: parsed incidents.json ({"open": [...], ...}).
        existing: current proposals file content (for dedup / ack memory).
        now: injected clock.
        min_count: minimum incident count to warrant a fix session.
        max_new: cap on fresh proposals emitted this run.

    Returns:
        Updated proposals dict: {"generated", "proposals": [...]}. Each proposal
        carries id, project, title, count, status ("proposed"), prompt, and
        first/last_proposed timestamps. Acknowledged ids are preserved as
        status "acknowledged" and never re-proposed.
    """
    if now is None:
        now = _now_utc()
    existing = existing or {}
    prior: dict[str, dict] = {p["id"]: p for p in existing.get("proposals", [])
                              if isinstance(p, dict) and "id" in p}

    open_incidents = [i for i in incidents_doc.get("open", [])
                      if isinstance(i, dict) and int(i.get("count", 0)) >= min_count]
    # Strongest first, so the per-run cap keeps the most painful ones.
    open_incidents.sort(key=lambda i: int(i.get("count", 0)), reverse=True)

    result: dict[str, dict] = dict(prior)  # keep acknowledged/old entries
    new_emitted = 0
    for inc in open_incidents:
        iid = inc.get("id", "")
        if not iid:
            continue
        proj = inc.get("project", "")
        if iid in prior:
            # Already known. Refresh count/last_seen; keep status (ack stays ack).
            entry = dict(prior[iid])
            # Corrupt-provenance guard: an 'accepted' entry with no valid
            # accepted_by_user date was hand-injected — demote so the human
            # sees it again instead of it silently vanishing.
            if (entry.get("status") == "accepted"
                    and not _valid_iso_date(entry.get("accepted_by_user"))):
                entry["status"] = "proposed"
            # Blocklist guard: never keep an actionable proposal for a project
            # the user fixes directly — retire it.
            if proj in BLOCKED_PROJECTS and entry.get("status") == "proposed":
                entry["status"] = "suppressed"
            entry["count"] = inc.get("count", entry.get("count"))
            entry["last_proposed"] = now.isoformat()
            if entry.get("status") == "proposed":
                entry["prompt"] = build_prompt(inc)  # keep prompt current
            result[iid] = entry
            continue
        # Never emit a fresh proposal for a blocked project.
        if proj in BLOCKED_PROJECTS:
            continue
        if new_emitted >= max_new:
            continue
        result[iid] = {
            "id": iid,
            "project": inc.get("project", ""),
            "title": inc.get("title", ""),
            "count": inc.get("count", 0),
            "status": "proposed",
            "prompt": build_prompt(inc),
            "first_proposed": now.isoformat(),
            "last_proposed": now.isoformat(),
        }
        new_emitted += 1

    proposals = sorted(result.values(),
                       key=lambda p: int(p.get("count", 0)), reverse=True)
    return {"generated": now.isoformat(), "proposals": proposals}


def pending(proposals_doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Proposals still awaiting human action (status 'proposed')."""
    return [p for p in proposals_doc.get("proposals", [])
            if isinstance(p, dict) and p.get("status") == "proposed"]


def acknowledge(proposals_doc: dict[str, Any], incident_id: str) -> bool:
    """Mark a proposal acknowledged so it is not re-proposed. Returns found?."""
    for p in proposals_doc.get("proposals", []):
        if isinstance(p, dict) and p.get("id") == incident_id:
            p["status"] = "acknowledged"
            return True
    return False


def accept(proposals_doc: dict[str, Any], incident_id: str,
           actor: str = "user", now: datetime | None = None) -> bool:
    """Mark a proposal accepted via a TRACKED code path. Returns found?.

    This is the only legitimate way to set ``accepted_by_user``: stamps a real
    ISO date + the actor, so a later refresh can tell a genuine human accept
    apart from a hand-injected/corrupt one (which gets demoted).
    """
    if now is None:
        now = _now_utc()
    for p in proposals_doc.get("proposals", []):
        if isinstance(p, dict) and p.get("id") == incident_id:
            p["status"] = "accepted"
            p["accepted_by_user"] = now.date().isoformat()
            p["accepted_actor"] = actor
            return True
    return False


def sync_status_to_state(
    proposal: dict[str, Any],
    state_path: Path = DEFAULT_STATE,
    now: datetime | None = None,
) -> bool:
    """Reflect a proposal's accepted/resolved status back to incidents-state.json.

    When a fix proposal is accepted or resolved, the corresponding cluster in
    incidents-state.json is annotated so the daily brief and outcome_kpi do not
    keep surfacing it as an unresolved open issue.

    This writes ONLY to the data file (incidents-state.json) — it NEVER touches
    incident_tracker.py code.

    Args:
        proposal: A single proposal dict containing at minimum 'id' and 'status'.
        state_path: Path to incidents-state.json (DI for tests).
        now: Reference timestamp; defaults to UTC now.

    Returns:
        True if the cluster was found and updated, False otherwise.
    """
    new_status = proposal.get("status")
    if new_status not in ("accepted", "resolved", "acknowledged"):
        return False

    state = _load_json(state_path, {})
    if not isinstance(state, dict):
        return False

    iid = proposal.get("id", "")
    if not iid:
        return False

    if now is None:
        now = _now_utc()

    updated = False
    for cluster in state.get("clusters", []):
        if isinstance(cluster, dict) and cluster.get("id") == iid:
            cluster["proposal_status"] = new_status
            cluster["proposal_synced_at"] = now.isoformat()
            updated = True
            break

    if updated:
        _atomic_write(state_path, state)
    return updated


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_refresh(incidents_path: Path = DEFAULT_INCIDENTS,
                proposals_path: Path = DEFAULT_PROPOSALS,
                now: datetime | None = None) -> int:
    incidents_doc = _load_json(incidents_path, {"open": []})
    existing = _load_json(proposals_path, {})
    updated = propose_fixes(incidents_doc, existing=existing, now=now)
    _atomic_write(proposals_path, updated)
    n = len(pending(updated))
    print(f"Fix proposals: {n} pending (review before launching).")
    for p in pending(updated):
        print(f"  [{p['id']}] {p.get('project','')} | count={p.get('count')} | {p.get('title','')[:60]}")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="Propose human-gated fix sessions from incidents.")
    ap.add_argument("--list", action="store_true", help="List pending proposals.")
    ap.add_argument("--ack", metavar="ID", help="Acknowledge a proposal (won't re-propose).")
    ap.add_argument("--accept", metavar="ID", help="Accept a proposal (tracked provenance).")
    ap.add_argument("--actor", default="user", help="Who is accepting (with --accept).")
    args = ap.parse_args()

    if args.ack:
        doc = _load_json(DEFAULT_PROPOSALS, {})
        if acknowledge(doc, args.ack):
            _atomic_write(DEFAULT_PROPOSALS, doc)
            # Reflect back to incidents-state.json (incidents-status-desync fix).
            prop = next((p for p in doc.get("proposals", [])
                         if isinstance(p, dict) and p.get("id") == args.ack), None)
            if prop:
                sync_status_to_state(prop)
            print(f"Proposal '{args.ack}' acknowledged.")
        else:
            print(f"Proposal '{args.ack}' not found.")
    elif args.accept:
        doc = _load_json(DEFAULT_PROPOSALS, {})
        if accept(doc, args.accept, actor=args.actor):
            _atomic_write(DEFAULT_PROPOSALS, doc)
            # Reflect back to incidents-state.json (incidents-status-desync fix).
            prop = next((p for p in doc.get("proposals", [])
                         if isinstance(p, dict) and p.get("id") == args.accept), None)
            if prop:
                synced = sync_status_to_state(prop)
                if synced:
                    print(f"  (synced status=accepted to incidents-state.json)")
            print(f"Proposal '{args.accept}' accepted by {args.actor}.")
        else:
            print(f"Proposal '{args.accept}' not found.")
    elif args.list:
        doc = _load_json(DEFAULT_PROPOSALS, {})
        items = pending(doc)
        if not items:
            print("No pending fix proposals.")
        for p in items:
            print(f"\n[{p['id']}] {p.get('project','')} (count={p.get('count')})")
            print(p.get("prompt", ""))
    else:
        cmd_refresh()

    sys.exit(0)


if __name__ == "__main__":
    main()
