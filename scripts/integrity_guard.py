#!/usr/bin/env python3
"""integrity_guard.py — Daily invariant checker for the self-learning automation system.

Detects "broken ruler" situations where the system is measuring itself with
corrupted or regressed signals.  Each check is a pure function that returns a
list of violation dicts; missing/corrupt inputs yield [] (or a low-severity
note), never an exception.

Usage:
    python integrity_guard.py            # write to logs/integrity-report.json
    python integrity_guard.py --dry-run  # print report, do not write file

Violation schema:
    {"check": str, "severity": "critical"|"high"|"medium"|"low", "detail": str}

Report schema (logs/integrity-report.json):
    {
        "generated": "<ISO-8601 UTC>",
        "violations": [<violation>, ...],
        "counts": {"critical": int, "high": int, "medium": int, "low": int}
    }
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Bootstrap: inject scripts/ into sys.path so sibling imports work
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from core_io import atomic_write_json, load_json_tolerant, now_utc  # noqa: E402

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------
Violation = dict[str, str]

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------
_LOGS_DIR = Path.home() / ".claude" / "logs"
_DRAFTS_DIR = _LOGS_DIR / "skill-drafts"
_THRESHOLDS_PATH = _LOGS_DIR / "thresholds.json"
_EFFECTIVENESS_PATH = _LOGS_DIR / "effectiveness.json"
_GRAPH_PATH = Path("{{WIKI_PATH}}/Claude/graph/knowledge_graph.json")
_CROSS_RECALL_PATH = _LOGS_DIR / "cross-recall-surfaced.jsonl"
_INCIDENTS_PATH = _LOGS_DIR / "incidents.json"
_FIX_PROPOSALS_PATH = _LOGS_DIR / "fix-proposals.json"
_QUEUE_PATH = _LOGS_DIR / "improvement-queue.json"
_REPORT_PATH = _LOGS_DIR / "integrity-report.json"
_DISCIPLINE_STATS_PATH = _LOGS_DIR / "discipline_stats.json"
_DISCIPLINE_HISTORY_PATH = _LOGS_DIR / "discipline_history.jsonl"
_DISCIPLINE_STALE_DAYS = 10
_DISCIPLINE_REGRESSION_PTS = 15.0

# ---------------------------------------------------------------------------
# Mojibake detection helpers
# ---------------------------------------------------------------------------
# U+FFFD is the Unicode replacement character — signals decoding failure.
_REPLACEMENT_CHAR = "�"

# Classic UTF-8-as-cp1252 mojibake bigrams: a Latin letter followed by a
# combining/punctuation char that should never appear in normal prose.
# Only flag these when they appear as multi-byte sequences that look like
# mangled Cyrillic.  Real Cyrillic (U+0400–U+04FF) is explicitly allowed.
_MOJIBAKE_BIGRAM_RE = re.compile(
    r"(?:"
    r"Ð[^\x00-\x7F]"   # Ð followed by any non-ASCII (mangled Cyrillic)
    r"|Ñ[^\x00-\x7F]"  # Ñ followed by any non-ASCII
    r"|Â[«»\xa0\xb0\xb1]"  # Â + specific punctuation that never pairs normally
    r")"
)


def _has_mojibake(text: str) -> bool:
    """Return True if *text* contains U+FFFD or classic UTF-8-as-cp1252 bigrams.

    Real Cyrillic (U+0400–U+04FF) is explicitly NOT flagged.

    Args:
        text: String to inspect.

    Returns:
        True if mojibake detected, False otherwise.
    """
    if _REPLACEMENT_CHAR in text:
        return True
    return bool(_MOJIBAKE_BIGRAM_RE.search(text))


def _extract_text_fields(obj: Any, fields: tuple[str, ...] = ("text", "project")) -> list[str]:
    """Recursively collect string values from *fields* in a nested structure.

    Args:
        obj: Any JSON-decoded object.
        fields: Field names to extract from dicts.

    Returns:
        Flat list of string values found.
    """
    results: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in fields and isinstance(v, str):
                results.append(v)
            else:
                results.extend(_extract_text_fields(v, fields))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_extract_text_fields(item, fields))
    return results


# ---------------------------------------------------------------------------
# Check 1 — drafts_have_no_tool_ngrams
# ---------------------------------------------------------------------------

def check_drafts_have_no_tool_ngrams(
    drafts_dir: Path = _DRAFTS_DIR,
) -> list[Violation]:
    """Check that skill-drafts/ contains no directories named as pure tool-ngrams.

    Imports is_tool_ngram from llm_judge for the canonical definition so the
    guard and the prune gate use identical logic.

    Args:
        drafts_dir: Path to the skill-drafts directory.

    Returns:
        List of violations; empty list means the check passed.
    """
    try:
        from llm_judge import is_tool_ngram  # noqa: PLC0415
    except ImportError as exc:
        return [
            {
                "check": "drafts_have_no_tool_ngrams",
                "severity": "low",
                "detail": f"Could not import is_tool_ngram from llm_judge: {exc}",
            }
        ]

    if not drafts_dir.exists():
        return []  # Nothing to check — no drafts yet.

    try:
        dirs = [d.name for d in drafts_dir.iterdir() if d.is_dir()]
    except OSError as exc:
        return [
            {
                "check": "drafts_have_no_tool_ngrams",
                "severity": "low",
                "detail": f"Could not read skill-drafts dir: {exc}",
            }
        ]

    tool_ngram_dirs = [name for name in dirs if is_tool_ngram(name)]

    if not tool_ngram_dirs:
        return []

    examples = tool_ngram_dirs[:5]
    return [
        {
            "check": "drafts_have_no_tool_ngrams",
            "severity": "high",
            "detail": (
                f"Prune/generation-gate regression: {len(tool_ngram_dirs)} skill-draft "
                f"director{'y' if len(tool_ngram_dirs) == 1 else 'ies'} "
                f"{'is' if len(tool_ngram_dirs) == 1 else 'are'} pure tool-ngrams. "
                f"Examples: {', '.join(examples)}"
            ),
        }
    ]


# ---------------------------------------------------------------------------
# Check 2 — thresholds_in_bounds
# ---------------------------------------------------------------------------

def check_thresholds_in_bounds(
    thresholds_path: Path = _THRESHOLDS_PATH,
    effectiveness_path: Path = _EFFECTIVENESS_PATH,
) -> list[Violation]:
    """Check that habit_distinctiveness_min is within expected bounds.

    Rules:
    - Must be present and in [50, 500].
    - If it is at the lowered extreme (< 150) AND effectiveness.json reports
      habit precision > 0.5 with samples < 50, flag "precision likely inflated".

    Args:
        thresholds_path: Path to thresholds.json.
        effectiveness_path: Path to effectiveness.json.

    Returns:
        List of violations.
    """
    violations: list[Violation] = []

    thresholds: dict[str, Any] = load_json_tolerant(thresholds_path, {})
    if not isinstance(thresholds, dict):
        thresholds = {}

    key = "habit_distinctiveness_min"
    if key not in thresholds:
        violations.append(
            {
                "check": "thresholds_in_bounds",
                "severity": "medium",
                "detail": f"'{key}' is absent from thresholds.json — expected value in [50, 500].",
            }
        )
        return violations  # No point checking bounds on a missing value.

    value = thresholds[key]
    try:
        fval = float(value)
    except (TypeError, ValueError):
        violations.append(
            {
                "check": "thresholds_in_bounds",
                "severity": "medium",
                "detail": f"'{key}' = {value!r} is not numeric.",
            }
        )
        return violations

    if not (50 <= fval <= 500):
        violations.append(
            {
                "check": "thresholds_in_bounds",
                "severity": "medium",
                "detail": (
                    f"'{key}' = {fval} is outside the valid range [50, 500]. "
                    "A fabricated precision metric may have distorted this threshold."
                ),
            }
        )

    # Secondary check: if value is at the lowered extreme (< 150), probe effectiveness.
    if fval < 150:
        eff: dict[str, Any] = load_json_tolerant(effectiveness_path, {})
        precision_by_type = eff.get("precision_by_type", {}) if isinstance(eff, dict) else {}
        samples_by_type = eff.get("samples_by_type", {}) if isinstance(eff, dict) else {}

        habit_precision = precision_by_type.get("habit", 0.0)
        habit_samples = samples_by_type.get("habit", 0)

        try:
            hp = float(habit_precision)
            hs = int(habit_samples)
        except (TypeError, ValueError):
            hp, hs = 0.0, 0

        if hp > 0.5 and hs < 50:
            violations.append(
                {
                    "check": "thresholds_in_bounds",
                    "severity": "medium",
                    "detail": (
                        f"'{key}' = {fval} (< 150, lowered extreme) while "
                        f"habit precision = {hp:.3f} (> 0.5) on only {hs} samples "
                        f"(< 50) — precision is likely inflated from insufficient data."
                    ),
                }
            )

    return violations


# ---------------------------------------------------------------------------
# Check 3 — graph_counts_match_disk
# ---------------------------------------------------------------------------

def check_graph_counts_match_disk(
    graph_path: Path = _GRAPH_PATH,
) -> list[Violation]:
    """Check that count fields in knowledge_graph.json meta match actual array lengths.

    Also detects U+FFFD replacement characters (mojibake) in meta.description.

    Args:
        graph_path: Path to knowledge_graph.json.

    Returns:
        List of violations.
    """
    violations: list[Violation] = []

    if not graph_path.exists():
        return [
            {
                "check": "graph_counts_match_disk",
                "severity": "low",
                "detail": f"knowledge_graph.json not found at {graph_path}",
            }
        ]

    graph: dict[str, Any] = load_json_tolerant(graph_path, {})
    if not isinstance(graph, dict):
        return [
            {
                "check": "graph_counts_match_disk",
                "severity": "low",
                "detail": "knowledge_graph.json could not be parsed as a JSON object.",
            }
        ]

    meta: dict[str, Any] = graph.get("meta", {}) if isinstance(graph.get("meta"), dict) else {}

    # --- Mojibake in meta.description ---
    description = meta.get("description", "")
    if isinstance(description, str) and _has_mojibake(description):
        violations.append(
            {
                "check": "graph_counts_match_disk",
                "severity": "medium",
                "detail": (
                    "meta.description contains U+FFFD replacement character — "
                    "the file was written with an incorrect encoding."
                ),
            }
        )

    # --- Count field consistency ---
    # Map: meta field name -> (graph top-level key, array key or None for direct list)
    # The meta *may* or may not contain these fields; only flag if present AND wrong.
    # Only fields that map to an actual array in the graph belong here.
    # test_count / script_count are scalar metrics (NOT graph arrays) — they are
    # int-sanity-checked separately below, never compared to a phantom array.
    count_checks: list[tuple[str, str, str | None]] = [
        ("cluster_count", "clusters", None),     # clusters is a dict -> len(keys)
        ("critical_rule_count", "critical_rules", None),
    ]

    for meta_field, graph_key, _sub_key in count_checks:
        if meta_field not in meta:
            continue  # Not claimed — nothing to verify.

        claimed = meta[meta_field]
        try:
            claimed_int = int(claimed)
        except (TypeError, ValueError):
            violations.append(
                {
                    "check": "graph_counts_match_disk",
                    "severity": "medium",
                    "detail": f"meta.{meta_field} = {claimed!r} is not an integer.",
                }
            )
            continue

        actual_obj = graph.get(graph_key)
        if actual_obj is None:
            violations.append(
                {
                    "check": "graph_counts_match_disk",
                    "severity": "high",
                    "detail": (
                        f"meta.{meta_field} = {claimed_int} but '{graph_key}' key "
                        f"is absent from knowledge_graph.json."
                    ),
                }
            )
            continue

        if isinstance(actual_obj, (list, dict)):
            actual_count = len(actual_obj)
        else:
            violations.append(
                {
                    "check": "graph_counts_match_disk",
                    "severity": "medium",
                    "detail": (
                        f"meta.{meta_field} claims to count '{graph_key}' "
                        f"but that key is not a list or dict."
                    ),
                }
            )
            continue

        if claimed_int != actual_count:
            violations.append(
                {
                    "check": "graph_counts_match_disk",
                    "severity": "high",
                    "detail": (
                        f"meta.{meta_field} = {claimed_int} but actual "
                        f"len({graph_key}) = {actual_count}. "
                        "A prior session may have reported fabricated counts."
                    ),
                }
            )

    # --- Scalar metric sanity (test_count / script_count are NOT arrays) ---
    for scalar_field in ("test_count", "script_count"):
        if scalar_field not in meta:
            continue
        val = meta[scalar_field]
        if val is None:
            continue  # explicit null = "unknown", acceptable
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            violations.append(
                {
                    "check": "graph_counts_match_disk",
                    "severity": "low",
                    "detail": f"meta.{scalar_field} = {val!r} is not a non-negative integer.",
                }
            )

    return violations


# ---------------------------------------------------------------------------
# Check 4 — no_mojibake_in_jsonl
# ---------------------------------------------------------------------------

def check_no_mojibake_in_jsonl(
    cross_recall_path: Path = _CROSS_RECALL_PATH,
    incidents_path: Path = _INCIDENTS_PATH,
) -> list[Violation]:
    """Scan JSONL/JSON logs for mojibake in text and project fields.

    Real Cyrillic (U+0400–U+04FF) is valid and is NOT flagged.
    Only U+FFFD and classic UTF-8-as-cp1252 bigrams are flagged.

    Args:
        cross_recall_path: Path to cross-recall-surfaced.jsonl.
        incidents_path: Path to incidents.json.

    Returns:
        List of violations.
    """
    violations: list[Violation] = []

    def _scan_jsonl(path: Path) -> int:
        """Return count of corrupted records in a JSONL file."""
        if not path.exists():
            return 0
        corrupted = 0
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    texts = _extract_text_fields(obj, ("text", "project", "t"))
                    if any(_has_mojibake(t) for t in texts):
                        corrupted += 1
        except OSError:
            pass
        return corrupted

    def _scan_json(path: Path) -> int:
        """Return count of corrupted text/project fields in a JSON file."""
        if not path.exists():
            return 0
        data = load_json_tolerant(path, None)
        if data is None:
            return 0
        texts = _extract_text_fields(data, ("text", "project", "title", "examples"))
        return sum(1 for t in texts if _has_mojibake(t))

    cross_count = _scan_jsonl(cross_recall_path)
    if cross_count > 0:
        violations.append(
            {
                "check": "no_mojibake_in_jsonl",
                "severity": "medium",
                "detail": (
                    f"{cross_count} record(s) in cross-recall-surfaced.jsonl contain "
                    "U+FFFD or UTF-8-as-cp1252 mojibake bigrams. "
                    "Run encoding_guard to repair."
                ),
            }
        )

    incidents_count = _scan_json(incidents_path)
    if incidents_count > 0:
        violations.append(
            {
                "check": "no_mojibake_in_jsonl",
                "severity": "medium",
                "detail": (
                    f"{incidents_count} field(s) in incidents.json contain "
                    "U+FFFD or UTF-8-as-cp1252 mojibake bigrams."
                ),
            }
        )

    return violations


# ---------------------------------------------------------------------------
# Check 5 — accepted_provenance_valid
# ---------------------------------------------------------------------------

def check_accepted_provenance_valid(
    fix_proposals_path: Path = _FIX_PROPOSALS_PATH,
) -> list[Violation]:
    """Verify every accepted fix-proposal has a valid ISO-date provenance field.

    Args:
        fix_proposals_path: Path to fix-proposals.json.

    Returns:
        List of violations.
    """
    data = load_json_tolerant(fix_proposals_path, None)
    if data is None:
        return []

    # fix-proposals.json is either a list of proposals or a dict with "proposals" key.
    if isinstance(data, dict):
        proposals: list[Any] = data.get("proposals", [])
    elif isinstance(data, list):
        proposals = data
    else:
        return []

    bad_ids: list[str] = []
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        if proposal.get("status") != "accepted":
            continue
        provenance = proposal.get("accepted_by_user")
        if not provenance:
            bad_ids.append(str(proposal.get("id", "<no-id>")))
            continue
        # Must parse as an ISO date (at minimum YYYY-MM-DD).
        try:
            datetime.fromisoformat(str(provenance))
        except ValueError:
            bad_ids.append(str(proposal.get("id", "<no-id>")))

    if not bad_ids:
        return []

    return [
        {
            "check": "accepted_provenance_valid",
            "severity": "medium",
            "detail": (
                f"{len(bad_ids)} accepted proposal(s) lack a valid ISO-date in "
                f"'accepted_by_user': {', '.join(bad_ids[:5])}. "
                "Provenance is required for audit traceability."
            ),
        }
    ]


# ---------------------------------------------------------------------------
# Check 6 — queue_noise_ratio
# ---------------------------------------------------------------------------

def check_queue_noise_ratio(
    queue_path: Path = _QUEUE_PATH,
    threshold: float = 0.1,
) -> list[Violation]:
    """Check the fraction of habit queue items whose routine slug is a tool-ngram.

    The routine slug is extracted from the habit item's id field:
    format is ``habit-<project>-<slug>`` where <slug> is the routine name.

    Args:
        queue_path: Path to improvement-queue.json.
        threshold: Maximum allowed fraction of tool-ngram slugs (default 0.1).

    Returns:
        List of violations.
    """
    try:
        from llm_judge import is_tool_ngram  # noqa: PLC0415
    except ImportError as exc:
        return [
            {
                "check": "queue_noise_ratio",
                "severity": "low",
                "detail": f"Could not import is_tool_ngram from llm_judge: {exc}",
            }
        ]

    queue: list[Any] = load_json_tolerant(queue_path, [])
    if not isinstance(queue, list):
        return []

    habit_items = [item for item in queue if isinstance(item, dict) and item.get("type") == "habit"]
    if not habit_items:
        return []

    def _extract_slug(item_id: str) -> str:
        """Extract the routine slug from a habit item id."""
        # id format: habit-<project>-<slug>  where project may contain hyphens
        # The slug is everything after 'habit-<project>-'
        # Use the 'description' field as fallback if needed.
        parts = item_id.split("-", 2)  # ['habit', '<project>', '<rest>']
        if len(parts) >= 3:
            return parts[2]
        return item_id

    ngram_items = [
        item for item in habit_items
        if is_tool_ngram(_extract_slug(item.get("id", "")))
    ]

    ratio = len(ngram_items) / len(habit_items)

    if ratio > threshold:
        examples = [item.get("id", "") for item in ngram_items[:3]]
        return [
            {
                "check": "queue_noise_ratio",
                "severity": "low",
                "detail": (
                    f"Improvement queue noise ratio = {ratio:.1%} "
                    f"({len(ngram_items)}/{len(habit_items)} habit items have tool-ngram slugs, "
                    f"threshold = {threshold:.0%}). Examples: {', '.join(examples)}"
                ),
            }
        ]

    return []


def check_discipline_gated(
    *,
    stats_path: Path = _DISCIPLINE_STATS_PATH,
    history_path: Path = _DISCIPLINE_HISTORY_PATH,
    now: datetime | None = None,
) -> list[Violation]:
    """Flag a broken or regressing discipline-measurement loop.

    The weekly dispatcher task refreshes discipline_stats.json (Sonnet vs Fable).
    If that file is absent or stale, the self-measurement loop is not running —
    exactly the gap (documented-but-unwired) this guard closes. Regression is
    judged ONLY on the logging-INDEPENDENT sequence signals (read>edit, test>edit)
    between the last two history rows; the thinking-dependent rates are confounded
    by whether a session logged thinking, so they are never used here.
    """
    if not stats_path.exists():
        return [{
            "check": "discipline_gated",
            "severity": "medium",
            "detail": f"{stats_path.name} absent — the weekly discipline task is not "
                      "producing stats (loop unwired or failing).",
        }]
    out: list[Violation] = []
    ref = (now or now_utc()).timestamp()
    try:
        age_days = (ref - stats_path.stat().st_mtime) / 86400.0
    except OSError:
        age_days = 0.0
    if age_days > _DISCIPLINE_STALE_DAYS:
        out.append({
            "check": "discipline_gated",
            "severity": "low",
            "detail": f"{stats_path.name} stale ({age_days:.0f}d > {_DISCIPLINE_STALE_DAYS}d) "
                      "— weekly discipline task may have stopped refreshing it.",
        })
    rows: list[dict] = []
    if history_path.exists():
        for line in history_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    # The weekly task appends one row PER model run (Sonnet then Opus), so the
    # history interleaves targets. Track the PRIMARY model — the one whose stats
    # are the canonical discipline_stats.json (Sonnet, the default driver) — and
    # compare only ITS rows. Otherwise a Sonnet-vs-Opus diff masquerades as a
    # regression, and the secondary (Opus, always appended last) shadows the primary.
    stats = load_json_tolerant(stats_path, {})
    primary = (stats.get("target") or {}).get("model") if isinstance(stats, dict) else None
    if rows:
        target = primary or rows[-1].get("target")
        same = [r for r in rows if r.get("target") == target]
    else:
        same = []
    if len(same) >= 2:
        prev, last = same[-2], same[-1]
        for key, label in (("read_before_edit_pct", "read>edit"),
                           ("real_test_after_edit_pct", "test>edit")):
            pv = (prev.get("t") or {}).get(key)
            lv = (last.get("t") or {}).get(key)
            if (isinstance(pv, (int, float)) and isinstance(lv, (int, float))
                    and pv - lv >= _DISCIPLINE_REGRESSION_PTS):
                out.append({
                    "check": "discipline_gated",
                    "severity": "medium",
                    "detail": f"discipline regression: {label} {pv:.0f}%→{lv:.0f}% "
                              f"(-{pv - lv:.0f} pts) for {last.get('target', '?')} "
                              f"between {prev.get('date', '?')} and {last.get('date', '?')}.",
                })
    return out


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

_SEVERITIES = ("critical", "high", "medium", "low")


def run_all(
    *,
    drafts_dir: Path = _DRAFTS_DIR,
    thresholds_path: Path = _THRESHOLDS_PATH,
    effectiveness_path: Path = _EFFECTIVENESS_PATH,
    graph_path: Path = _GRAPH_PATH,
    cross_recall_path: Path = _CROSS_RECALL_PATH,
    incidents_path: Path = _INCIDENTS_PATH,
    fix_proposals_path: Path = _FIX_PROPOSALS_PATH,
    queue_path: Path = _QUEUE_PATH,
    discipline_stats_path: Path = _DISCIPLINE_STATS_PATH,
    discipline_history_path: Path = _DISCIPLINE_HISTORY_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run all integrity checks and return the report dict.

    Args:
        drafts_dir: Override for skill-drafts directory.
        thresholds_path: Override for thresholds.json.
        effectiveness_path: Override for effectiveness.json.
        graph_path: Override for knowledge_graph.json.
        cross_recall_path: Override for cross-recall-surfaced.jsonl.
        incidents_path: Override for incidents.json.
        fix_proposals_path: Override for fix-proposals.json.
        queue_path: Override for improvement-queue.json.
        now: Inject a fixed datetime for deterministic timestamps in tests.

    Returns:
        Report dict with keys: generated, violations, counts.
    """
    ts = (now or now_utc()).isoformat()

    all_violations: list[Violation] = []
    all_violations.extend(check_drafts_have_no_tool_ngrams(drafts_dir=drafts_dir))
    all_violations.extend(
        check_thresholds_in_bounds(
            thresholds_path=thresholds_path,
            effectiveness_path=effectiveness_path,
        )
    )
    all_violations.extend(check_graph_counts_match_disk(graph_path=graph_path))
    all_violations.extend(
        check_no_mojibake_in_jsonl(
            cross_recall_path=cross_recall_path,
            incidents_path=incidents_path,
        )
    )
    all_violations.extend(check_accepted_provenance_valid(fix_proposals_path=fix_proposals_path))
    all_violations.extend(check_queue_noise_ratio(queue_path=queue_path))
    all_violations.extend(
        check_discipline_gated(
            stats_path=discipline_stats_path,
            history_path=discipline_history_path,
            now=now,
        )
    )

    counts: dict[str, int] = {sev: 0 for sev in _SEVERITIES}
    for v in all_violations:
        sev = v.get("severity", "low")
        if sev in counts:
            counts[sev] += 1

    return {
        "generated": ts,
        "violations": all_violations,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point.  Always exits 0 so the dispatcher can call this safely."""
    parser = argparse.ArgumentParser(
        description="Daily integrity check for the self-learning automation system."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the report to stdout instead of writing to logs/integrity-report.json.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=_REPORT_PATH,
        help="Override the output path for the report JSON.",
    )
    args = parser.parse_args()

    report = run_all()

    output = json.dumps(report, ensure_ascii=False, indent=2)

    if args.dry_run:
        print(output)
    else:
        atomic_write_json(args.report_path, report)
        total = sum(report["counts"].values())
        print(
            f"integrity_guard: {total} violation(s) "
            f"[critical={report['counts']['critical']} "
            f"high={report['counts']['high']} "
            f"medium={report['counts']['medium']} "
            f"low={report['counts']['low']}] "
            f"→ {args.report_path}"
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
