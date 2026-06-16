#!/usr/bin/env python3
"""self_improvement_queue.py — Unified ranked self-improvement inbox.

Aggregates all sources (Boris corrections, habit candidates, graphify enrichment,
pending promotions) into ONE ranked list. Score = confidence x value_weight.
Items with score >= AUTO_APPLY_THRESHOLD are marked "auto_apply" (reflexes).

Usage:
    python self_improvement_queue.py
    python self_improvement_queue.py --out path.json
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

# Shared filters: keep pure tool-ngram routines (Edit>Bash:grep noise) out of
# the queue entirely so the judge budget is spent on real candidates.
try:  # pragma: no cover - siblings are always present in practice
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from llm_judge import is_tool_ngram as _is_tool_ngram
    from habit_to_skill import slugify_routine as _slugify_routine
except Exception:  # pragma: no cover
    _is_tool_ngram = None
    _slugify_routine = None

LOGS_DIR = Path.home() / ".claude" / "logs"
DEFAULT_OUT = LOGS_DIR / "improvement-queue.json"
# Boris items with count >= 6 score 0.855; threshold 0.85 makes auto_apply reachable
AUTO_APPLY_THRESHOLD = 0.85

# Default paths for trust_tiers dependency injection (patchable in tests)
_TRUST_EFFECTIVENESS_PATH = LOGS_DIR / "effectiveness.json"
_TRUST_OVERRIDES_PATH = Path.home() / ".claude" / "trust-overrides.json"


def _get_trust_tier(
    item_type: str,
    effectiveness_path: Path | None = None,
    overrides_path: Path | None = None,
) -> int:
    """Return trust tier for *item_type* via trust_tiers.get_tier, fail-closed to 0.

    Lazily imports trust_tiers so the rest of the module works even if the
    file is temporarily absent.  Any import or runtime error returns 0
    (fail-closed: do NOT auto_apply).

    Args:
        item_type: The queue item type string.
        effectiveness_path: Path override for tests; None uses module default.
        overrides_path: Path override for tests; None uses module default.

    Returns:
        Integer tier 0, 1, or 2.  Returns 0 on any failure.
    """
    eff_path = effectiveness_path if effectiveness_path is not None else _TRUST_EFFECTIVENESS_PATH
    ov_path = overrides_path if overrides_path is not None else _TRUST_OVERRIDES_PATH
    try:
        import importlib
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        trust_tiers = importlib.import_module("trust_tiers")
        return trust_tiers.get_tier(item_type, effectiveness_path=eff_path, overrides_path=ov_path)
    except Exception:
        return 0  # fail-closed

# Patchable in tests so suppression filter reads a tmp feedback file
_FEEDBACK_PATH = LOGS_DIR / "suggestion-feedback.json"


def _effective_distinctiveness_min() -> float:
    """Return HABIT_DISTINCTIVENESS_MIN, overridden by thresholds.json if present.

    Reads ``~/.claude/logs/thresholds.json`` (key ``habit_distinctiveness_min``).
    Falls back to the module constant on any error.

    Returns:
        Effective distinctiveness threshold as a float.
    """
    try:
        t = json.loads((LOGS_DIR / "thresholds.json").read_text(encoding="utf-8"))
        return float(t.get("habit_distinctiveness_min", HABIT_DISTINCTIVENESS_MIN))
    except Exception:
        return HABIT_DISTINCTIVENESS_MIN


@dataclass
class QueueItem:
    id: str
    type: str         # "boris_rule" | "promotion" | "habit" | "graphify"
    description: str
    project: str      # project name or "all"
    confidence: float
    value: float
    score: float
    status: str = "queued"   # "queued" | "auto_apply" | "surfaced" | "dismissed"
    source: str = ""
    judge_score: float | None = None   # LLM quality score 0.0-1.0, None = unscored
    judge_reason: str = ""             # One-sentence LLM explanation
    judge_verdict: str = ""            # "useful" | "junk" | "error" | "" (empty = unscored)
    judge_fail_count: int = 0          # consecutive failures; >= 2 → permanently skipped


_VALUE = {
    "boris_rule": 0.9,
    "promotion": 0.8,
    "habit": 0.7,
    "graphify": 0.5,
}


def _load_json(path: Path) -> dict | list:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _load_boris(path: Path) -> list[QueueItem]:
    data = _load_json(path)
    if not isinstance(data, dict):
        return []
    items = []
    for project, info in (data.get("projects") or {}).items():
        count = info.get("count", 0)
        if count < 2:
            continue
        confidence = min(0.5 + count * 0.08, 0.95)
        value = _VALUE["boris_rule"]
        examples = info.get("examples", [])
        desc = f"{count} corrections in {project}"
        if examples:
            desc += f" — e.g. '{examples[0][:80]}'"
        item = QueueItem(
            id=f"boris-{project.lower().replace(' ', '-')[:40]}",
            type="boris_rule",
            description=desc,
            project=project,
            confidence=round(confidence, 3),
            value=value,
            score=round(confidence * value, 3),
            source=str(path),
        )
        # Status will be tier-gated in build_queue; set provisional here.
        item.status = "auto_apply" if item.score >= AUTO_APPLY_THRESHOLD else "queued"
        items.append(item)
    return items


# Habits only enter the queue if they are distinctive (real workflows, not file churn)
# and rewarded. This drops the thousands of generic Read->Edit n-grams.
HABIT_DISTINCTIVENESS_MIN = 150.0
HABIT_REWARD_MIN = 0.6


def _load_habits(path: Path, ledger_path: Path | None = None) -> list[QueueItem]:
    data = _load_json(path)
    if not isinstance(data, list):
        return []
    # Ledger status (if available) lets graduated habits jump the queue
    ledger = {}
    if ledger_path is not None:
        ld = _load_json(ledger_path)
        if isinstance(ld, dict):
            ledger = ld
    items = []
    for h in data:
        count = h.get("count", 0)
        sess = h.get("session_count", 0)
        distinctiveness = float(h.get("distinctiveness", 0.0))
        reward = float(h.get("reward_ratio", 1.0))
        routine = h.get("routine") or []
        proj = h.get("project", "all")

        # Drop pure tool-ngram routines before they crowd the queue and consume
        # the LLM-judge budget — they have no reusable semantic value.
        if _is_tool_ngram is not None and _slugify_routine is not None:
            ngram_slug = _slugify_routine(proj, routine)
            if ngram_slug and _is_tool_ngram(ngram_slug):
                continue

        key = f"{proj}|{'>'.join(str(t) for t in routine)}"
        status = (ledger.get(key) or {}).get("status", "detected")

        graduated = status in ("suggested_skill", "skill_exists")
        # Surface only graduated habits OR distinctive+rewarded ones — drop the noise
        if not graduated:
            if (count < 4 or sess < 2
                    or distinctiveness < _effective_distinctiveness_min()
                    or reward < HABIT_REWARD_MIN):
                continue
        if status == "skill_exists":
            continue  # already a skill — nothing to suggest

        # Confidence driven by distinctiveness (not raw count) + graduation boost
        confidence = min(0.4 + distinctiveness / 1000.0, 0.85)
        if graduated:
            confidence = min(confidence + 0.15, 0.95)
        value = _VALUE["habit"]
        verb = "Codify as skill" if graduated else "Recurring workflow"
        item = QueueItem(
            id=f"habit-{proj}-{'-'.join(routine[:3])}".lower()[:60],
            type="habit",
            description=f"{verb} in {proj}: {' -> '.join(routine)} "
                        f"(dist={distinctiveness:.0f}, {count}x, reward={reward:.2f})",
            project=proj,
            confidence=round(confidence, 3),
            value=value,
            score=round(confidence * value, 3),
            source=str(path),
        )
        # Status will be tier-gated in build_queue; set provisional here.
        item.status = "auto_apply" if item.score >= AUTO_APPLY_THRESHOLD else "queued"
        items.append(item)
    return items


def _load_graphify(path: Path) -> list[QueueItem]:
    data = _load_json(path)
    if not isinstance(data, dict):
        return []
    items = []
    for entry in data.get("queued_for_llm") or []:
        project = entry.get("project", "?")
        if not entry.get("enrich"):
            continue
        item = QueueItem(
            id=f"graphify-{project.lower()[:30]}",
            type="graphify",
            description=f"Graph enrichment needed: {project} — run /graphify to add critical_rules",
            project=project,
            confidence=0.7,
            value=_VALUE["graphify"],
            score=round(0.7 * _VALUE["graphify"], 3),
            source=str(path),
        )
        items.append(item)
    return items


def _load_promotions(path: Path) -> list[QueueItem]:
    """Parse pending promotions written by learning_promoter.py.

    learning_promoter writes two complementary markers per candidate:
      - Section heading:  ``## Candidate N: <title>``
      - Status checkbox:  ``- [ ] Candidate N``   (no title, just the number)

    We parse the section headings to extract titles and skip any candidate
    whose checkbox has been ticked (``- [x]``) — meaning it was already applied.

    Args:
        path: Path to promotions-pending.md.

    Returns:
        List of QueueItem for each pending (un-ticked) promotion candidate.
    """
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []

    # Build set of already-applied candidate numbers (checked boxes).
    # Format: ``- [x] Candidate N``
    applied_nums: set[int] = {
        int(m.group(1))
        for m in re.finditer(r"- \[x\] Candidate (\d+)", text, re.IGNORECASE)
    }

    items = []
    # Section heading format: ``## Candidate N: <title>``
    for m in re.finditer(r"^## Candidate (\d+): (.+?)$", text, re.MULTILINE):
        num = int(m.group(1))
        if num in applied_nums:
            continue  # already applied — skip
        desc = m.group(2).strip()[:200]
        confidence = 0.75
        value = _VALUE["promotion"]
        item = QueueItem(
            id=f"promo-{hashlib.md5(desc.encode()).hexdigest()[:6]}",
            type="promotion",
            description=desc,
            project="all",
            confidence=confidence,
            value=value,
            score=round(confidence * value, 3),
            source=str(path),
        )
        items.append(item)
    return items


def build_queue(
    boris_path: Path = LOGS_DIR / "boris-candidates.json",
    habits_path: Path = LOGS_DIR / "habits.json",
    graphify_path: Path = LOGS_DIR / "graphify-queue.json",
    promotions_path: Path = LOGS_DIR / "promotions-pending.md",
    ledger_path: Path = LOGS_DIR / "habit-ledger.json",
    carry_judge_path: Path | None = DEFAULT_OUT,
    trust_effectiveness_path: Path | None = None,
    trust_overrides_path: Path | None = None,
) -> list[QueueItem]:
    """Build a ranked list of self-improvement items from all sources.

    After loading, applies a trust-tier gate: ``status="auto_apply"`` is only
    kept when the item's type has a trust tier >= 1 (via trust_tiers.get_tier).
    Items that no longer qualify are downgraded back to ``"queued"``.
    If trust_tiers cannot be imported the gate fails-closed (no auto_apply).

    Args:
        boris_path: Path to boris-candidates.json.
        habits_path: Path to habits.json.
        graphify_path: Path to graphify-queue.json.
        promotions_path: Path to promotions-pending.md.
        ledger_path: Path to habit-ledger.json (graduation status).
        carry_judge_path: Previously saved queue whose judge_* fields are
            carried over by item id (regeneration must not wipe LLM verdicts).
            None disables carry-over.
        trust_effectiveness_path: Path override for trust_tiers.get_tier
            (tests inject a tmp path; None uses module default).
        trust_overrides_path: Path override for trust_tiers.get_tier
            (tests inject a tmp path; None uses module default).

    Returns:
        List of QueueItem sorted by score descending.
    """
    items: list[QueueItem] = []
    items.extend(_load_boris(boris_path))
    items.extend(_load_habits(habits_path, ledger_path=ledger_path))
    items.extend(_load_graphify(graphify_path))
    items.extend(_load_promotions(promotions_path))

    # Judge verdicts live only in the saved queue file; a rebuild from sources
    # would silently discard them, so llm_judge work survives regeneration.
    if carry_judge_path is not None:
        prev = {d.get("id"): d for d in load_queue(carry_judge_path)}
        for it in items:
            old = prev.get(it.id)
            if not old:
                continue
            # Carry judge_score/reason/verdict only when the item has not yet
            # been scored in this build (avoids clobbering a fresh score).
            if it.judge_score is None and old.get("judge_score") is not None:
                it.judge_score = old.get("judge_score")
                it.judge_reason = old.get("judge_reason", "")
                it.judge_verdict = old.get("judge_verdict", "")
            # Always carry judge_fail_count and error verdicts so poisoned items
            # are not retried after a rebuild when Ollama was genuinely down.
            if old.get("judge_fail_count", 0) > 0:
                it.judge_fail_count = old.get("judge_fail_count", 0)
            if it.judge_verdict == "" and old.get("judge_verdict") == "error":
                it.judge_verdict = "error"
                it.judge_reason = old.get("judge_reason", "")
                it.judge_score = old.get("judge_score", 0.0)

        # Items the LLM judge already ruled "junk" must not resurface on rebuild.
        items = [it for it in items if it.judge_verdict != "junk"]

    # Tier gate: auto_apply requires trust tier >= 1.
    # Items that score >= AUTO_APPLY_THRESHOLD but whose type is Tier 0 are
    # downgraded back to "queued".  Fail-closed: if trust_tiers is unavailable,
    # no item is allowed to keep auto_apply status.
    _tier_cache: dict[str, int] = {}

    def _allowed_auto_apply(item_type: str) -> bool:
        if item_type not in _tier_cache:
            try:
                _tier_cache[item_type] = _get_trust_tier(
                    item_type,
                    effectiveness_path=trust_effectiveness_path,
                    overrides_path=trust_overrides_path,
                )
            except Exception:
                _tier_cache[item_type] = 0  # fail-closed
        return _tier_cache[item_type] >= 1

    for it in items:
        if it.status == "auto_apply" and not _allowed_auto_apply(it.type):
            it.status = "queued"

    # Inhibitory feedback: drop items that are suppressed (dismissed) OR already accepted.
    # Accepted items must not re-surface — the user has explicitly acted on them.
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from suggestion_feedback import is_suppressed, load_feedback
        _fb = load_feedback(_FEEDBACK_PATH)
        def _should_drop(it: QueueItem) -> bool:
            entry = _fb.get(it.id)
            if entry and entry.get("status") == "accepted":
                return True
            return is_suppressed(it.id, path=_FEEDBACK_PATH)
        items = [it for it in items if not _should_drop(it)]
    except Exception:
        pass

    # Primary sort: items with judge_score ranked by judge_score desc;
    # unscored items (judge_score=None) come after all scored items.
    # Secondary sort: original score desc (preserved within each group).
    def _sort_key(x: QueueItem) -> tuple[int, float, float]:
        has_score = 0 if x.judge_score is None else 1
        js = x.judge_score if x.judge_score is not None else 0.0
        return (-has_score, -js, -x.score)

    items.sort(key=_sort_key)
    return items


def filter_for_project(items: list[QueueItem], project: str) -> list[QueueItem]:
    """Return items relevant to the given project (substring match, or project='all').

    Cross-project habits (project='_cross_project') are always included — they
    represent universal patterns relevant to every project.

    Args:
        items: Full queue returned by build_queue().
        project: Project name to filter by. Empty string returns only "all" items.

    Returns:
        Filtered list preserving original order.
    """
    if not project:
        return [i for i in items if i.project in ("all", "_cross_project")]
    pnorm = project.lower().replace(" ", "").replace("-", "")
    return [i for i in items
            if i.project in ("all", "_cross_project")
            or pnorm in i.project.lower().replace(" ", "").replace("-", "")
            or i.project.lower().replace(" ", "").replace("-", "") in pnorm]


def save_queue(items: list[QueueItem], path: Path = DEFAULT_OUT) -> None:
    """Serialize queue to JSON file.

    Args:
        items: Queue items to save.
        path: Output file path. Parent directories are created if needed.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(i) for i in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_queue(path: Path = DEFAULT_OUT) -> list[dict]:
    """Load a previously saved queue from JSON.

    Args:
        path: Path to the saved queue JSON file.

    Returns:
        List of raw dicts (not QueueItem instances).
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Build unified self-improvement queue")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument(
        "--top",
        type=int,
        default=None,
        metavar="N",
        help="Print only top N items (by judge_score then score). "
             "Output goes to stdout; queue is still saved in full.",
    )
    args = p.parse_args()
    items = build_queue(carry_judge_path=Path(args.out))
    save_queue(items, path=Path(args.out))
    auto = sum(1 for i in items if i.status == "auto_apply")
    print(f"[queue] {len(items)} items ({auto} auto_apply) -> {args.out}", file=sys.stderr)

    if args.top is not None:
        top_items = items[: args.top]
        import json as _json
        from dataclasses import asdict as _asdict
        print(_json.dumps([_asdict(i) for i in top_items], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
