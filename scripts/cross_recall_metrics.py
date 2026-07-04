#!/usr/bin/env python3
"""cross_recall_metrics.py — Measure whether cross-project recall actually helps.

The session-start cross-project surfacing (session_start_brief.py) writes one
event per session to ``logs/cross-recall-surfaced.jsonl``:

    {"ts": "...", "project": "Fakturka.bg", "enriched": true,
     "surfaced": [{"ns": "_shared", "id": "...", "score": 0.89, "hc0": 3}, ...]}

``hc0`` is the vector's recall hit_count AT SURFACE TIME. Because the surfacing
queries are NON-tracking, any later increase of that vector's hit_count in
``recall-tracker.json`` is a genuine, independent RE-RECALL — i.e. the surfaced
lesson came back when something queried for it during real work. That is our
automatic engagement proxy.

Two engagement signals:
  1. re-recall  (automatic): hit_count(now) > hc0  → the lesson resurfaced in work.
  2. explicit   (high-signal): the id was marked used via ``--used <id>``.

HONEST CAVEAT: re-recall is a LOWER BOUND. A surfaced lesson can help without
being queried again (you just read it from the brief and applied it). So the
true usefulness rate is >= the measured re-recall rate, never lower.

Usage:
    python cross_recall_metrics.py                 # compute + print + write json
    python cross_recall_metrics.py --days 14
    python cross_recall_metrics.py --used <vec-id> # mark a surfaced lesson as used
    python cross_recall_metrics.py --json          # machine-readable only

Never raises a non-zero exit on missing data — prints "no data yet".
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from encoding_guard import guard as _enc_guard
except Exception:
    def _enc_guard(t: str) -> str:  # type: ignore[misc]
        return t

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

LOGS = Path.home() / ".claude" / "logs"
SURFACED = LOGS / "cross-recall-surfaced.jsonl"
USED = LOGS / "cross-recall-used.jsonl"
TRACKER = LOGS / "recall-tracker.json"
OUT = LOGS / "cross-recall-metrics.json"


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _parse_ts(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def project_threshold_bump(
    metrics: dict, project: str, min_surfaced: int = 20, bump: float = 0.08
) -> float:
    """Per-project suppression decay — NOT a kill switch.

    Returns `bump` when `project` has been surfaced >= min_surfaced times with
    ZERO engagement in `metrics["per_project"]` (a chronic-noise project like
    higgsfield.ai: 90 surfaced / 0 engaged). Returns 0.0 otherwise, including
    when the project or its keys are missing (tolerant — never raises).

    The caller adds this to the cross-project similarity threshold
    (_XTHRESH), so a 0-engagement project needs a higher-signal match to
    surface at all next time. Because every surfacing event keeps getting
    logged regardless, the very next `cross_recall_metrics.py` run can see a
    fresh engagement and the bump naturally drops back to 0 — self-correcting
    decay, not a permanent ban.
    """
    try:
        pp = (metrics or {}).get("per_project", {}) or {}
        stats = pp.get(project) or {}
        surfaced = int(stats.get("surfaced", 0))
        engaged = int(stats.get("engaged", 0))
    except Exception:
        return 0.0
    if surfaced >= min_surfaced and engaged == 0:
        return bump
    return 0.0


def mark_used(vec_id: str) -> None:
    """Append an explicit 'this surfaced lesson was used' record."""
    USED.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "id": vec_id}
    with USED.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def resolve_used(arg: str) -> list[str]:
    """Resolve a --used argument to vector id(s).

    Accepts either an exact vector id, or a text fragment that is matched
    (case-insensitive) against the short snippet ('t') stored with each
    surfaced lesson — so you can mark a lesson you saw in the brief without
    looking up its id, e.g. ``--used weasyprint``. Returns the matching ids
    from the MOST RECENT surfacing event that contains the fragment.
    """
    rows = _read_jsonl(SURFACED)
    for e in rows:  # exact id match wins
        for s in (e.get("surfaced") or []):
            if s.get("id") == arg:
                return [arg]
    low = arg.lower()
    for e in reversed(rows):  # newest first
        ids = [s["id"] for s in (e.get("surfaced") or [])
               if s.get("id") and low in (s.get("t") or "").lower()]
        if ids:
            return ids
    return []


def compute(days: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - days * 86400

    events = []
    for e in _read_jsonl(SURFACED):
        ts = _parse_ts(e.get("ts", ""))
        if ts and ts.timestamp() >= cutoff:
            events.append(e)

    tracker = _load_json(TRACKER)
    # Used records keyed by id with their timestamps: a use only counts as
    # engagement for surfacings that HAPPENED BEFORE it (mirrors is_rr's hc0
    # ordering). Without this, one legacy/false used record marks the vid
    # "engaged" for every future surfacing in every project — silently
    # disabling project_threshold_bump. Records without a parseable ts are
    # legacy-tolerated (count for any surfacing, the old behavior).
    used_ts: dict[str, list[float]] = {}
    legacy_used: set[str] = set()
    for r in _read_jsonl(USED):
        rid = r.get("id")
        if not rid:
            continue
        uts = _parse_ts(r.get("ts", ""))
        if uts is None:
            legacy_used.add(rid)
        else:
            used_ts.setdefault(rid, []).append(uts.timestamp())

    n_events = len(events)
    n_silent = sum(1 for e in events if not e.get("surfaced"))
    n_enriched = sum(1 for e in events if e.get("enriched"))

    surfaced = [s for e in events for s in (e.get("surfaced") or [])]
    n_surfaced = len(surfaced)

    scores: list[float] = []
    snippet_lens: list[int] = []
    per_project: dict[str, dict[str, int]] = {}

    # unique_rr_ids and unique_ex_ids deduplicate across surfacing events:
    # the same vector id may appear in multiple sessions (double-count bug).
    # We count engagement per UNIQUE vector id, not per surfacing occurrence.
    unique_rr_ids: set[str] = set()
    unique_ex_ids: set[str] = set()
    # per_project still counts surfacing occurrences (same id in different
    # projects counts once per project), matching the per_project semantics.

    # map surfaced item -> its project (events carry project)
    # Engaged ids per PROJECT (not per event) — the same vid surfaced in N
    # sessions of one project must count as 1 engaged lesson, not N.
    proj_engaged: dict[str, set[str]] = {}
    for e in events:
        proj = _enc_guard(e.get("project") or "?")
        pp = per_project.setdefault(proj, {"surfaced": 0, "engaged": 0})
        proj_engaged_ids = proj_engaged.setdefault(proj, set())
        ev_ts = _parse_ts(e.get("ts", ""))
        ev_epoch = ev_ts.timestamp() if ev_ts else cutoff
        for s in (e.get("surfaced") or []):
            vid = s.get("id") or ""
            sc = float(s.get("score", 0.0))
            scores.append(sc)
            snippet_lens.append(len(s.get("t", "") or ""))
            hc_now = int(tracker.get(vid, {}).get("hit_count", 0))
            is_rr = hc_now > int(s.get("hc0", 0))
            is_ex = vid in legacy_used or any(
                u >= ev_epoch for u in used_ts.get(vid, ()))
            if is_rr:
                unique_rr_ids.add(vid)
            if is_ex:
                unique_ex_ids.add(vid)
            if (is_rr or is_ex) and vid not in proj_engaged_ids:
                pp["engaged"] += 1
                proj_engaged_ids.add(vid)
            pp["surfaced"] += 1

    rerecalled = len(unique_rr_ids)
    explicit = len(unique_ex_ids)
    # unique_engaged: ids that were re-recalled OR explicitly used (deduplicated)
    unique_engaged_ids = unique_rr_ids | unique_ex_ids
    engaged = len(unique_engaged_ids)

    rate = round(engaged / n_surfaced, 3) if n_surfaced else 0.0
    return {
        "generated": now.isoformat(timespec="seconds"),
        "window_days": days,
        "events": n_events,
        "silent_events": n_silent,
        "enriched_pct": round(100 * n_enriched / n_events, 1) if n_events else 0.0,
        "surfaced": n_surfaced,
        "avg_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "snippet_len_avg": round(sum(snippet_lens) / len(snippet_lens), 1) if snippet_lens else 0.0,
        "rerecalled": rerecalled,
        "explicit_used": explicit,
        "engaged": engaged,
        "unique_engaged": engaged,  # explicit alias for clarity
        "engagement_rate": rate,
        "per_project": per_project,
        "note": (
            "engagement_rate is a LOWER BOUND (re-recall proxy); true usefulness >= this. "
            "engaged/rerecalled count UNIQUE vector ids (deduped across surfacing events)."
        ),
    }


def _print_summary(m: dict) -> None:
    if m["events"] == 0:
        print("cross-recall metrics: no surfacing events yet in window — "
              "data accumulates as sessions run.")
        return
    print(f"=== Cross-project recall — last {m['window_days']}d ===")
    print(f"  sessions with surfacing : {m['events']}  "
          f"(silent: {m['silent_events']}, enriched query: {m['enriched_pct']}%)")
    print(f"  lessons surfaced        : {m['surfaced']}  (avg score {m['avg_score']})")
    print(f"  engaged (used again)    : {m['engaged']}  "
          f"= re-recall {m['rerecalled']} + explicit {m['explicit_used']}")
    print(f"  ENGAGEMENT RATE         : {m['engagement_rate']*100:.0f}%  "
          f"(lower bound — true usefulness is higher)")
    if m["per_project"]:
        print("  per project:")
        for proj, pp in sorted(m["per_project"].items(),
                               key=lambda kv: -kv[1]["surfaced"]):
            if pp["surfaced"]:
                r = 100 * pp["engaged"] / pp["surfaced"]
                print(f"    {proj:18} {pp['engaged']}/{pp['surfaced']} engaged ({r:.0f}%)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30, help="window in days (default 30)")
    ap.add_argument("--used", metavar="VEC_ID", help="mark a surfaced lesson id as used")
    ap.add_argument("--json", dest="as_json", action="store_true", help="JSON only")
    args = ap.parse_args()

    if args.used:
        _ids = resolve_used(args.used)
        if not _ids:
            print(f"no surfaced lesson matched '{args.used}' (try an exact id or a "
                  f"keyword from the brief line)")
            return
        for _vid in _ids:
            mark_used(_vid)
        print(f"marked used: {', '.join(_ids)}")
        return

    m = compute(args.days)
    try:
        OUT.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    if args.as_json:
        print(json.dumps(m, ensure_ascii=False, indent=2))
    else:
        _print_summary(m)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"cross_recall_metrics: {e}")
        sys.exit(0)
