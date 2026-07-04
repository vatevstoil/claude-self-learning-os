"""ab_eval.py — A/B counterfactual eval via Interrupted Time Series (per-rule).

For each auto-applied rule in ``applied-ledger.jsonl``, measures the causal
effect on the complaint cluster it targeted: did applying the rule reduce
complaint recurrence?

Algorithm (Interrupted Time Series, no randomized control):
1. Load all rules from ``applied-ledger.jsonl`` with application timestamp t_R.
2. Derive the target project from the rule's ``target_file`` path.
3. For each rule, find the matching incident cluster in ``incidents-state.json``
   by project + text similarity (reuses ``incident_tracker.similarity``).
4. Count complaint RATE in [t_R - W, t_R] (before) vs [t_R, t_R + W] (after),
   where W = ``window_days`` (default 21). Rate = complaints in window / W.
5. Classify verdict:
   - ``"pending"``           — t_R + W > now (insufficient post-application history)
   - ``"insufficient_data"`` — n < 4 (too few events to judge)
   - ``"effective"``         — after_rate <= 0.5 * before_rate  (material reduction)
   - ``"regressed"``         — after_rate > before_rate
   - ``"no_effect"``         — otherwise
6. Write ``logs/ab-eval.json`` with per-rule records + portfolio summary.

CAVEATS (structural, cannot be eliminated by more data):
  - Confounded: other simultaneous changes affect the complaint rate.
  - Small-n noisy: most clusters have very few events; verdicts may flip on 1 event.
  - Needs >= window_days of post-application history; rules applied recently are
    marked "pending" until that window closes.
  - No randomized control group: we cannot rule out secular trends.

Usage:
    python ab_eval.py                 # compute + write logs/ab-eval.json
    python ab_eval.py --window-days 14
    python ab_eval.py --dry-run       # compute + print, do NOT write
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Stdlib-only import of core helpers (never raises on missing)
# ---------------------------------------------------------------------------

try:
    from core_io import load_json_tolerant, atomic_write_json
except ImportError:  # running tests without scripts/ on path
    def load_json_tolerant(path: Any, default: Any) -> Any:  # type: ignore[misc]
        """Fallback: load JSON file tolerantly, return default on any error."""
        p = Path(path)
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default

    def atomic_write_json(path: Any, data: Any) -> None:  # type: ignore[misc]
        """Fallback: write JSON atomically via temp-file + os.replace."""
        import os
        import tempfile
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, str(p))
        except Exception:
            try:
                import os as _os
                _os.unlink(tmp)
            except OSError:
                pass
            raise


try:
    from incident_tracker import normalize, similarity
except ImportError:
    # Minimal fallback (token-overlap Jaccard) — identical interface
    def normalize(text: str) -> list[str]:  # type: ignore[misc]
        """Tokenize text into lowercase words (>= 3 chars, no punctuation)."""
        import string
        text = text.lower().translate(
            str.maketrans(string.punctuation, " " * len(string.punctuation))
        )
        return [t for t in text.split() if len(t) >= 3]

    def similarity(a: list[str], b: list[str]) -> float:  # type: ignore[misc]
        """Compute Jaccard similarity between two token lists."""
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        sa, sb = set(a), set(b)
        return len(sa & sb) / len(sa | sb)


log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default paths (DI-injectable in all functions)
# ---------------------------------------------------------------------------

_LOGS = Path.home() / ".claude" / "logs"
DEFAULT_LEDGER = _LOGS / "applied-ledger.jsonl"
DEFAULT_STATE = _LOGS / "incidents-state.json"
DEFAULT_OUT = _LOGS / "ab-eval.json"

# Minimum similarity to consider a rule matching a cluster.
# Set conservatively low because the cluster text pool is large (representative
# + all examples) which dilutes token overlap; the project-scoped pass keeps
# false-positive rate manageable.
_MATCH_THRESHOLD: float = 0.08

# "Effective" = after_rate is at most this fraction of before_rate
_EFFECTIVE_RATIO: float = 0.5

# Minimum sample size for a non-"insufficient_data" verdict
_MIN_N: int = 4


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a timezone-aware datetime.

    Returns None on any parse failure so callers can skip gracefully.

    Args:
        ts: ISO-8601 string, e.g. "2026-06-10T12:00:00+00:00".

    Returns:
        Timezone-aware datetime or None.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _project_from_target_file(target_file: str) -> str:
    """Extract a normalised project key from a ``target_file`` path string.

    Strategy: take the parent directory name of the target file and normalise
    to lowercase. NOTE: this yields a directory basename ("facturka.bg"), NOT
    the cwd-slug form incident_tracker uses as cluster keys
    ("j--antigraviti-facturka-bg") — it is a last-resort fallback only.

    Args:
        target_file: Absolute path string to a CLAUDE.md or similar file.

    Returns:
        Normalised project key string (may be empty if path unparseable).
    """
    if not target_file:
        return ""
    p = Path(target_file)
    # CLAUDE.md lives at <project_root>/CLAUDE.md; parent is the project root.
    return p.parent.name.lower()


def _project_from_entry(entry: dict[str, Any]) -> str:
    """Derive the project key for a ledger entry, tolerating backfilled rows.

    Some ledger entries (e.g. ``source: backfill_review_verdicts``) carry a
    human-readable placeholder in ``target_file`` (not a real path), such as
    ``"(backfilled from review-verdicts 2026-06-11)"``. In that case
    ``_project_from_target_file`` cannot recover a project key (no path
    separator to parse), which previously produced an empty project string
    and starved the project-scoped cluster match.

    Fallback: derive the project from ``item_id`` instead, which encodes it
    as ``<type>-<project>`` (e.g. ``"boris-j--antigraviti-facturka-bg"`` or
    ``"habit-j--antigraviti-higgsfield-ai-powershell-read"``). We strip the
    leading type token (the segment before the first ``-``) and keep the rest.

    Args:
        entry: One row from ``applied-ledger.jsonl``.

    Returns:
        Normalised project key string (may be empty if nothing parseable).
    """
    item_id = entry.get("item_id", "")
    # Real auto-applied rows encode the project as a cwd slug (contains "--",
    # e.g. "auto-boris-j--antigraviti-facturka-bg") — the ONLY form that
    # matches incident_tracker cluster keys. A target_file path yields just a
    # directory basename ("facturka.bg") that never prefix-matches those keys,
    # so the slug must win over the path branch when both are present.
    m = re.search(r"[a-z0-9]+--[a-z0-9._-]+", item_id.lower())
    if m:
        return m.group(0)

    target_file = entry.get("target_file", "")
    if "/" in target_file or "\\" in target_file:
        return _project_from_target_file(target_file)

    if "-" not in item_id:
        return ""
    _prefix, rest = item_id.split("-", 1)
    return rest.lower()


def load_ledger_tolerant(ledger_path: Path) -> list[dict[str, Any]]:
    """Load all valid entries from a JSONL ledger file.

    Skips corrupt or empty lines silently.  Returns empty list when the file
    is absent or completely unreadable.

    Args:
        ledger_path: Path to the ``.jsonl`` file.

    Returns:
        List of entry dicts in file order.
    """
    if not ledger_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return entries


# ---------------------------------------------------------------------------
# Core matching: rule → cluster
# ---------------------------------------------------------------------------


def _rule_text(entry: dict[str, Any]) -> str:
    """Extract the human-readable text of a ledger entry for similarity matching.

    Prefers ``rule_preview`` (most descriptive), falls back to ``item_id``.

    Args:
        entry: A single ledger entry dict.

    Returns:
        Text string for token comparison.
    """
    return entry.get("rule_preview") or entry.get("item_id") or ""


def find_matching_cluster(
    rule_project: str,
    rule_tokens: list[str],
    clusters: list[dict[str, Any]],
    threshold: float = _MATCH_THRESHOLD,
) -> dict[str, Any] | None:
    """Find the best-matching incident cluster for a rule.

    Matches on project first (exact, case-insensitive), then picks the cluster
    with highest text similarity (via ``incident_tracker.similarity``) that
    exceeds ``threshold``.  If no cluster matches the project, falls back to
    cross-project similarity so sparse ledgers still yield some matches.

    Args:
        rule_project: Normalised project key from the ledger entry.
        rule_tokens: Normalised tokens from the rule text.
        clusters: List of cluster dicts from ``incidents-state.json``.
        threshold: Minimum similarity to accept a match.

    Returns:
        Best-matching cluster dict, or None if no cluster exceeds threshold.
    """
    if not clusters:
        return None

    best: dict[str, Any] | None = None
    best_score: float = threshold - 1e-9  # must strictly exceed threshold

    # Project-scoped pass first, then cross-project if nothing found
    for scoped in (True, False):
        for cluster in clusters:
            cluster_project = cluster.get("project", "").lower()
            # ponytail: prefix match (not exact equality) because item_id-derived
            # rule_project can carry a routine/habit suffix the cluster's project
            # key lacks (e.g. "...higgsfield-ai-powershell-read" vs
            # "...higgsfield-ai"). Guarded so an empty key on either side never
            # matches. Ceiling: two sibling projects sharing a name prefix can
            # collide (e.g. "foo" vs "foo-bar"); upgrade path = an explicit
            # project key field on future ledger rows instead of derived prefix.
            if scoped and (not cluster_project or not rule_project.startswith(cluster_project)):
                continue

            # Build candidate text: representative + examples
            parts = [cluster.get("representative", "")]
            for ex in cluster.get("examples", []):
                if isinstance(ex, dict):
                    parts.append(ex.get("text", ""))
                elif isinstance(ex, str):
                    parts.append(ex)
            cluster_tokens = normalize(" ".join(parts))

            score = similarity(rule_tokens, cluster_tokens)
            if score > best_score:
                best_score = score
                best = cluster

        if best is not None:
            break  # found something in the scoped pass

    return best


# ---------------------------------------------------------------------------
# Rate computation
# ---------------------------------------------------------------------------


def _complaints_in_window(
    cluster: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
) -> list[datetime]:
    """Collect complaint timestamps that fall within [window_start, window_end).

    Reads ``examples[].seen`` timestamps from the cluster dict.

    Args:
        cluster: A single cluster dict from ``incidents-state.json``.
        window_start: Start of the window (inclusive).
        window_end: End of the window (exclusive).

    Returns:
        List of datetime objects for complaints in the window.
    """
    hits: list[datetime] = []
    for ex in cluster.get("examples", []):
        if isinstance(ex, dict):
            raw_ts = ex.get("seen", "")
        else:
            continue
        dt = _parse_iso(raw_ts)
        if dt is None:
            continue
        if window_start <= dt < window_end:
            hits.append(dt)
    return hits


# ---------------------------------------------------------------------------
# Per-rule evaluation
# ---------------------------------------------------------------------------


def evaluate_rule(
    entry: dict[str, Any],
    clusters: list[dict[str, Any]],
    window_days: int = 21,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute the A/B verdict for a single ledger entry.

    Args:
        entry: One row from ``applied-ledger.jsonl``.
        clusters: All cluster dicts from ``incidents-state.json``.
        window_days: Width W of the before/after observation window in days.
        now: Current datetime (UTC, timezone-aware).  Injected for testability;
            never call datetime.now() inside this function.

    Returns:
        Dict with fields:
            item_id         — from ledger entry
            item_type       — from ledger entry
            ts_applied      — ISO-8601 string of application time
            project         — derived project key
            cluster_id      — matched cluster id, or null
            before_rate     — complaints/day in [t_R - W, t_R]
            after_rate      — complaints/day in [t_R, t_R + W]
            n               — total complaints counted (before + after)
            confidence      — "low" when n < 4, else "normal"
            verdict         — one of: pending | insufficient_data | effective
                              | no_effect | regressed
            note            — human-readable explanation
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    item_id = entry.get("item_id", "")
    item_type = entry.get("item_type", "")
    ts_raw = entry.get("ts", "")
    t_r = _parse_iso(ts_raw)

    # Cannot evaluate without an application timestamp
    if t_r is None:
        return {
            "item_id": item_id,
            "item_type": item_type,
            "ts_applied": ts_raw,
            "project": _project_from_entry(entry),
            "cluster_id": None,
            "before_rate": None,
            "after_rate": None,
            "n": 0,
            "confidence": "low",
            "verdict": "insufficient_data",
            "note": "Could not parse application timestamp; skipped.",
        }

    project = _project_from_entry(entry)
    rule_tokens = normalize(_rule_text(entry))
    cluster = find_matching_cluster(project, rule_tokens, clusters)

    W = timedelta(days=window_days)
    window_before_start = t_r - W
    window_after_end = t_r + W

    # Pending: not enough post-application time has elapsed
    if window_after_end > now:
        return {
            "item_id": item_id,
            "item_type": item_type,
            "ts_applied": ts_raw,
            "project": project,
            "cluster_id": cluster.get("id") if cluster else None,
            "before_rate": None,
            "after_rate": None,
            "n": 0,
            "confidence": "low",
            "verdict": "pending",
            "note": (
                f"Post-application window closes {window_after_end.date().isoformat()}; "
                "verdict available then."
            ),
        }

    if cluster is None:
        return {
            "item_id": item_id,
            "item_type": item_type,
            "ts_applied": ts_raw,
            "project": project,
            "cluster_id": None,
            "before_rate": None,
            "after_rate": None,
            "n": 0,
            "confidence": "low",
            "verdict": "insufficient_data",
            "note": "No matching incident cluster found for this rule.",
        }

    before_complaints = _complaints_in_window(cluster, window_before_start, t_r)
    after_complaints = _complaints_in_window(cluster, t_r, window_after_end)
    n = len(before_complaints) + len(after_complaints)
    confidence = "low" if n < _MIN_N else "normal"

    if n < _MIN_N:
        return {
            "item_id": item_id,
            "item_type": item_type,
            "ts_applied": ts_raw,
            "project": project,
            "cluster_id": cluster.get("id"),
            "before_rate": round(len(before_complaints) / window_days, 4),
            "after_rate": round(len(after_complaints) / window_days, 4),
            "n": n,
            "confidence": confidence,
            "verdict": "insufficient_data",
            "note": f"n={n} < {_MIN_N}; need more events for a reliable verdict.",
        }

    before_rate = len(before_complaints) / window_days
    after_rate = len(after_complaints) / window_days

    if before_rate == 0.0 and after_rate == 0.0:
        verdict = "no_effect"
        note = "Zero complaints in both windows; rule applied in a quiet period."
    elif before_rate == 0.0:
        verdict = "regressed"
        note = "No complaints before but complaints appeared after application."
    elif after_rate <= _EFFECTIVE_RATIO * before_rate:
        verdict = "effective"
        note = (
            f"After-rate ({after_rate:.4f}/day) is <= {int(_EFFECTIVE_RATIO*100)}% "
            f"of before-rate ({before_rate:.4f}/day)."
        )
    elif after_rate > before_rate:
        verdict = "regressed"
        note = (
            f"After-rate ({after_rate:.4f}/day) exceeds "
            f"before-rate ({before_rate:.4f}/day)."
        )
    else:
        verdict = "no_effect"
        note = (
            f"After-rate ({after_rate:.4f}/day) changed but not enough to classify "
            f"as effective (need <= {int(_EFFECTIVE_RATIO*100)}% of {before_rate:.4f}/day)."
        )

    return {
        "item_id": item_id,
        "item_type": item_type,
        "ts_applied": ts_raw,
        "project": project,
        "cluster_id": cluster.get("id"),
        "before_rate": round(before_rate, 4),
        "after_rate": round(after_rate, 4),
        "n": n,
        "confidence": confidence,
        "verdict": verdict,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Portfolio summary
# ---------------------------------------------------------------------------


def compute_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-rule records into a portfolio summary.

    Args:
        records: List of dicts as returned by ``evaluate_rule``.

    Returns:
        Dict with counts per verdict and effectiveness_pct.
    """
    counts: dict[str, int] = {
        "total": len(records),
        "effective": 0,
        "no_effect": 0,
        "regressed": 0,
        "pending": 0,
        "insufficient_data": 0,
    }
    for r in records:
        v = r.get("verdict", "insufficient_data")
        counts[v] = counts.get(v, 0) + 1

    decided = counts["effective"] + counts["no_effect"] + counts["regressed"]
    effectiveness_pct: float | None = (
        round(100.0 * counts["effective"] / decided, 1) if decided > 0 else None
    )

    return {
        "total_rules": counts["total"],
        "effective": counts["effective"],
        "no_effect": counts["no_effect"],
        "regressed": counts["regressed"],
        "pending": counts["pending"],
        "insufficient_data": counts["insufficient_data"],
        "decided": decided,
        "effectiveness_pct": effectiveness_pct,
        "note": (
            f"{effectiveness_pct}% of decided auto-applied rules measurably "
            "reduced their target complaint rate."
        ) if effectiveness_pct is not None else "No decided rules yet.",
    }


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def run_eval(
    ledger_path: Path = DEFAULT_LEDGER,
    state_path: Path = DEFAULT_STATE,
    out_path: Path = DEFAULT_OUT,
    window_days: int = 21,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the full A/B evaluation and optionally write results.

    This is the primary entry point for programmatic use.  All paths and the
    current time are injected so the function is fully testable without
    touching the filesystem or the real clock.

    Args:
        ledger_path: Path to ``applied-ledger.jsonl``.
        state_path: Path to ``incidents-state.json``.
        out_path: Path to write ``ab-eval.json``.
        window_days: Width of before/after observation window in days.
        dry_run: When True, compute but do NOT write ``out_path``.
        now: Current UTC datetime (timezone-aware).  Defaults to real clock.

    Returns:
        The full result dict (same content as the written JSON).
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)

    ledger_entries = load_ledger_tolerant(ledger_path)
    state_data = load_json_tolerant(state_path, {})
    clusters: list[dict[str, Any]] = state_data.get("clusters", []) if isinstance(state_data, dict) else []

    # Filter out rolled-back entries
    active_entries = [e for e in ledger_entries if not e.get("rolled_back", False)]

    records: list[dict[str, Any]] = [
        evaluate_rule(entry, clusters, window_days=window_days, now=now)
        for entry in active_entries
    ]

    summary = compute_summary(records)

    result: dict[str, Any] = {
        "generated": now.isoformat(),
        "window_days": window_days,
        "caveats": [
            "Confounded: other simultaneous changes may move the complaint rate.",
            "Small-n noisy: with few events, verdicts may flip on a single complaint.",
            "Needs >= window_days of post-application history; recent rules are 'pending'.",
            "No randomized control group: secular trends cannot be ruled out.",
        ],
        "summary": summary,
        "rules": records,
    }

    if not dry_run:
        atomic_write_json(out_path, result)
        log.info("ab_eval: wrote %s (%d rules evaluated)", out_path, len(records))
    else:
        log.info("ab_eval: dry-run complete (%d rules evaluated)", len(records))

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Command-line entry point.

    Exits 0 on all non-fatal conditions.  A sparse or empty ledger is not
    an error; it simply produces a summary with 0 rules.
    """
    parser = argparse.ArgumentParser(
        description="A/B counterfactual eval for auto-applied rules (Interrupted Time Series).",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=21,
        metavar="N",
        help="Width of before/after window in days (default: 21).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print results without writing ab-eval.json.",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER,
        metavar="PATH",
        help=f"Path to applied-ledger.jsonl (default: {DEFAULT_LEDGER}).",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE,
        metavar="PATH",
        help=f"Path to incidents-state.json (default: {DEFAULT_STATE}).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        metavar="PATH",
        help=f"Output path for ab-eval.json (default: {DEFAULT_OUT}).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    result = run_eval(
        ledger_path=args.ledger,
        state_path=args.state,
        out_path=args.out,
        window_days=args.window_days,
        dry_run=args.dry_run,
    )

    summary = result["summary"]
    print(f"ab_eval summary (window={result['window_days']}d):")
    print(f"  total rules in ledger : {summary['total_rules']}")
    print(f"  pending               : {summary['pending']}")
    print(f"  insufficient_data     : {summary['insufficient_data']}")
    print(f"  decided               : {summary['decided']}")
    print(f"    effective           : {summary['effective']}")
    print(f"    no_effect           : {summary['no_effect']}")
    print(f"    regressed           : {summary['regressed']}")
    pct = summary["effectiveness_pct"]
    print(f"  effectiveness         : {pct}%" if pct is not None else "  effectiveness         : n/a (no decided rules)")

    if args.dry_run:
        print("\n[dry-run] Output NOT written.")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"\nOutput written to: {args.out}")


if __name__ == "__main__":
    main()
