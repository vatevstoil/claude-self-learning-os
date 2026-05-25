"""Anticipatory layer for brain-inspired self-learning OS.

Predicts likely next routine for a project from mined habits,
shifting the OS from reactive to proactive.

Usage:
    python anticipate.py                           # writes anticipations.json for all projects
    python anticipate.py --project <name>          # prints predictions for a specific project
    python anticipate.py --project <name> --recent "Bash:grep,Read"
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_CLAUDE_DIR = Path.home() / ".claude"
_DEFAULT_HABITS = _CLAUDE_DIR / "logs" / "habits.json"
_DEFAULT_ANTICIPATIONS = _CLAUDE_DIR / "logs" / "anticipations.json"

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("anticipate")


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------

# Index structure:
#   {
#     "by_first": {(project, first_token): [habit_dict, ...]},
#     "by_project": {project: [habit_dict, ...sorted by distinctiveness desc]},
#   }
Index = dict[str, Any]


# ---------------------------------------------------------------------------
# build_index
# ---------------------------------------------------------------------------


def build_index(habits: list[dict]) -> Index:
    """Build a lookup index from a flat list of habit dicts.

    Args:
        habits: List of habit dicts, each containing at least:
            ``project`` (str), ``routine`` (list[str]),
            ``distinctiveness`` (float).

    Returns:
        A dict with two keys:

        - ``"by_first"``: maps ``(project, first_token)`` → list of habits
          whose routine starts with *first_token* in *project*.
        - ``"by_project"``: maps ``project`` → list of all habits for that
          project, sorted by ``distinctiveness`` descending.
    """
    by_first: dict[tuple[str, str], list[dict]] = {}
    by_project: dict[str, list[dict]] = {}

    for habit in habits:
        project: str = habit.get("project", "")
        routine: list[str] = habit.get("routine", [])
        if not project or not routine:
            continue

        # by_project accumulation
        by_project.setdefault(project, []).append(habit)

        # by_first keyed on (project, routine[0])
        first_token: str = routine[0]
        key = (project, first_token)
        by_first.setdefault(key, []).append(habit)

    # Sort by_project entries by distinctiveness descending
    for proj in by_project:
        by_project[proj].sort(
            key=lambda h: h.get("distinctiveness", 0.0), reverse=True
        )

    return {"by_first": by_first, "by_project": by_project}


# ---------------------------------------------------------------------------
# predict_next
# ---------------------------------------------------------------------------


def predict_next(
    project: str,
    recent_tools: list[str] | None,
    index: Index,
    top: int = 3,
) -> list[dict]:
    """Predict the next likely tool/token given recent context.

    Two modes:

    1. **recent_tools non-empty**: look for habits whose routine *contains*
       ``recent_tools[-1]`` and return the token that follows it.  Ranked by
       distinctiveness descending.

    2. **recent_tools empty / None**: return the top-``top`` highest-
       distinctiveness habits for the project (the "what you usually do here"
       cold-start signal).

    Args:
        project: Target project name.
        recent_tools: Ordered list of recently used tool signatures, or None.
        index: Index built by :func:`build_index`.
        top: Maximum number of predictions to return.

    Returns:
        List of prediction dicts::

            {
                "routine": list[str],
                "next": str,          # predicted next token
                "confidence": float,  # 0.0–1.0 normalised distinctiveness
            }

        Empty list if project is unknown.
    """
    by_project: dict[str, list[dict]] = index.get("by_project", {})

    if project not in by_project:
        return []

    project_habits: list[dict] = by_project[project]  # already sorted desc

    # Compute max distinctiveness for normalisation (guaranteed ≥ 1 habit)
    max_dist: float = max(
        (h.get("distinctiveness", 0.0) for h in project_habits), default=1.0
    ) or 1.0

    def _confidence(habit: dict) -> float:
        return min(1.0, max(0.0, habit.get("distinctiveness", 0.0) / max_dist))

    # ------------------------------------------------------------------
    # Mode 1: recent context available
    # ------------------------------------------------------------------
    if recent_tools:
        last_token: str = recent_tools[-1]
        predictions: list[dict] = []

        for habit in project_habits:
            routine: list[str] = habit.get("routine", [])
            # Find all positions of last_token in the routine (except final)
            for pos, tok in enumerate(routine[:-1]):
                if tok == last_token:
                    next_tok = routine[pos + 1]
                    predictions.append(
                        {
                            "routine": routine,
                            "next": next_tok,
                            "confidence": _confidence(habit),
                        }
                    )
                    break  # one prediction per habit is enough

        # Deduplicate by (routine tuple, next) keeping highest confidence
        seen: dict[tuple, dict] = {}
        for pred in predictions:
            key = (tuple(pred["routine"]), pred["next"])
            if key not in seen or pred["confidence"] > seen[key]["confidence"]:
                seen[key] = pred

        ranked = sorted(seen.values(), key=lambda p: p["confidence"], reverse=True)
        return ranked[:top]

    # ------------------------------------------------------------------
    # Mode 2: cold start — top habits by distinctiveness
    # ------------------------------------------------------------------
    results: list[dict] = []
    for habit in project_habits[:top]:
        routine: list[str] = habit.get("routine", [])
        if not routine:
            continue
        results.append(
            {
                "routine": routine,
                "next": routine[0],
                "confidence": _confidence(habit),
            }
        )
    return results


# ---------------------------------------------------------------------------
# write_anticipations
# ---------------------------------------------------------------------------


def write_anticipations(
    habits_path: str | Path,
    out_path: str | Path,
) -> dict[str, list[dict]]:
    """Compute and persist anticipations for every project in habits.

    Args:
        habits_path: Path to ``habits.json``.
        out_path: Destination path for ``anticipations.json``.

    Returns:
        Dict mapping ``project`` → list of prediction dicts.
        Never raises — returns empty dict on any error.
    """
    try:
        habits_path = Path(habits_path)
        out_path = Path(out_path)

        habits: list[dict] = json.loads(habits_path.read_text(encoding="utf-8"))
        index: Index = build_index(habits)

        anticipations: dict[str, list[dict]] = {}
        for project in index.get("by_project", {}):
            preds = predict_next(project, None, index, top=3)
            if preds:
                anticipations[project] = preds

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(anticipations, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return anticipations

    except Exception as exc:  # noqa: BLE001
        log.error("write_anticipations failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for the anticipate CLI."""
    parser = argparse.ArgumentParser(
        description="Anticipate next routine for a project from mined habits."
    )
    parser.add_argument(
        "--project",
        metavar="PROJECT",
        default=None,
        help="Print predictions for a specific project instead of writing anticipations.json.",
    )
    parser.add_argument(
        "--recent",
        metavar="TOOL1,TOOL2,...",
        default=None,
        help="Comma-separated list of recently used tool signatures.",
    )
    parser.add_argument(
        "--habits",
        metavar="PATH",
        default=str(_DEFAULT_HABITS),
        help=f"Path to habits.json (default: {_DEFAULT_HABITS})",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        default=str(_DEFAULT_ANTICIPATIONS),
        help=f"Output path for anticipations.json (default: {_DEFAULT_ANTICIPATIONS})",
    )
    parser.add_argument(
        "--top",
        metavar="N",
        type=int,
        default=3,
        help="Number of predictions to return (default: 3).",
    )
    args = parser.parse_args()

    # Load habits
    habits_path = Path(args.habits)
    if not habits_path.exists():
        print(f"ERROR: habits file not found: {habits_path}", file=sys.stderr)
        sys.exit(1)

    try:
        habits: list[dict] = json.loads(habits_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: cannot parse habits file: {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(habits, list):
        print(f"ERROR: habits file is not a list: {habits_path}", file=sys.stderr)
        sys.exit(1)

    index = build_index(habits)

    if args.project:
        # Print predictions for a single project
        recent: list[str] | None = None
        if args.recent:
            recent = [t.strip() for t in args.recent.split(",") if t.strip()]

        preds = predict_next(args.project, recent, index, top=args.top)

        if not preds:
            print(f"No predictions for project '{args.project}'.")
            return

        print(f"Predictions for '{args.project}' (recent={recent}):")
        for i, pred in enumerate(preds, 1):
            routine_str = " -> ".join(pred["routine"])
            print(
                f"  {i}. next={pred['next']!r:20s}  conf={pred['confidence']:.2f}"
                f"  routine=[{routine_str}]"
            )
    else:
        # Write anticipations.json for all projects
        out_path = Path(args.out)
        result = write_anticipations(habits_path, out_path)
        projects = sorted(result.keys())
        print(f"Wrote anticipations for {len(projects)} project(s) to {out_path}")
        for proj in projects[:10]:
            top_pred = result[proj][0] if result[proj] else {}
            print(
                f"  {proj}: next={top_pred.get('next', '?')!r}"
                f"  conf={top_pred.get('confidence', 0):.2f}"
            )
        if len(projects) > 10:
            print(f"  ... and {len(projects) - 10} more.")


if __name__ == "__main__":
    main()
