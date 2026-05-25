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

LOGS_DIR = Path.home() / ".claude" / "logs"
DEFAULT_OUT = LOGS_DIR / "improvement-queue.json"
# Boris items with count >= 6 score 0.855; threshold 0.85 makes auto_apply reachable
AUTO_APPLY_THRESHOLD = 0.85

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
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    items = []
    for m in re.finditer(r"- \[ \] Candidate \d+: (.+?)(?:\n|$)", text):
        desc = m.group(1).strip()[:200]
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
) -> list[QueueItem]:
    """Build a ranked list of self-improvement items from all sources.

    Args:
        boris_path: Path to boris-candidates.json.
        habits_path: Path to habits.json.
        graphify_path: Path to graphify-queue.json.
        promotions_path: Path to promotions-pending.md.
        ledger_path: Path to habit-ledger.json (graduation status).

    Returns:
        List of QueueItem sorted by score descending.
    """
    items: list[QueueItem] = []
    items.extend(_load_boris(boris_path))
    items.extend(_load_habits(habits_path, ledger_path=ledger_path))
    items.extend(_load_graphify(graphify_path))
    items.extend(_load_promotions(promotions_path))

    # Inhibitory feedback: drop items currently in their suppression window
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from suggestion_feedback import is_suppressed
        items = [it for it in items if not is_suppressed(it.id, path=_FEEDBACK_PATH)]
    except Exception:
        pass

    items.sort(key=lambda x: -x.score)
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
    args = p.parse_args()
    items = build_queue()
    save_queue(items, path=Path(args.out))
    auto = sum(1 for i in items if i.status == "auto_apply")
    print(f"[queue] {len(items)} items ({auto} auto_apply) -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
