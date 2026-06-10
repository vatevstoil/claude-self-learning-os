#!/usr/bin/env python3
"""salience.py — Amygdala analog for the brain-inspired self-learning OS.

Scores text by how high-stakes it is so that important events receive stronger
retention than routine work.  Mirrors the biological amygdala: a signal
detector that flags threat/reward-laden stimuli for priority encoding.

Marker categories and weights
------------------------------
SECURITY   0.5 — auth, tokens, vulnerabilities, exploits
MONEY      0.5 — payments, invoices, billing (Bulgarian too)
PRODUCTION 0.4 — prod incidents, outages, hotfixes
ERRORS     0.3 — exceptions, tracebacks, crashes

Usage (standalone)::

    python salience.py                    # last 7 days
    python salience.py --days 30
    python salience.py --days 7 --out /tmp/sal.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECTS_DIR = Path.home() / ".claude" / "projects"
LOGS_DIR = Path.home() / ".claude" / "logs"
DEFAULT_OUT = LOGS_DIR / "salience.json"

# ---------------------------------------------------------------------------
# Marker registry
# ---------------------------------------------------------------------------

_MARKERS: dict[str, tuple[float, re.Pattern[str]]] = {
    "SECURITY": (
        0.5,
        re.compile(
            r"security|vulnerab|auth|password|token|exploit|CVE|breach",
            re.IGNORECASE,
        ),
    ),
    "MONEY": (
        0.5,
        re.compile(
            r"payment|invoice|фактура|billing|refund|financial|пари|плащане",
            re.IGNORECASE,
        ),
    ),
    "PRODUCTION": (
        0.4,
        re.compile(
            r"production|\bprod\b|incident|outage|downtime|hotfix|rollback",
            re.IGNORECASE,
        ),
    ),
    "ERRORS": (
        0.3,
        re.compile(
            r"error|exception|traceback|failed|crash|stack trace",
            re.IGNORECASE,
        ),
    ),
}

# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def classify_markers(text: str) -> list[str]:
    """Return a sorted list of category names whose patterns match *text*.

    Each category appears at most once regardless of how many keyword hits it
    scores within the text.

    Args:
        text: Arbitrary string to classify.

    Returns:
        Sorted list of matching category names (e.g. ``["ERRORS", "SECURITY"]``).
        Empty list when no markers fire.
    """
    if not text:
        return []
    matched: list[str] = [
        category
        for category, (_weight, pattern) in _MARKERS.items()
        if pattern.search(text)
    ]
    return sorted(matched)


def salience_score(text: str) -> float:
    """Compute a salience score in [0.0, 1.0] for *text*.

    Sums the weights of all categories whose patterns fire, capped at 1.0.
    Benign text with no marker hits returns 0.0.

    Args:
        text: Arbitrary string to score.

    Returns:
        Float in [0.0, 1.0].
    """
    if not text:
        return 0.0
    total = sum(
        _MARKERS[cat][0]
        for cat in classify_markers(text)
    )
    return min(total, 1.0)


# ---------------------------------------------------------------------------
# JSONL helpers (mirrors habit_miner.parse_jsonl_for_habits structure)
# ---------------------------------------------------------------------------


def _extract_text_from_content(content: Any) -> str:
    """Pull plain text out of a JSONL message content field.

    Handles both string and list-of-parts formats used by Claude transcripts.

    Args:
        content: The ``message.content`` value from a JSONL record.

    Returns:
        Concatenated text string.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "tool_result":
                    # tool result content can itself be a list
                    inner = item.get("content", "")
                    parts.append(_extract_text_from_content(inner))
        return " ".join(parts)
    return ""


def _parse_jsonl_messages(path: Path) -> list[str]:
    """Yield text strings from all user/assistant messages in a JSONL file.

    Args:
        path: Path to a ``.jsonl`` transcript file.

    Returns:
        List of non-empty text strings extracted from the file.
    """
    texts: list[str] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    rec = json.loads(raw_line)
                except Exception:
                    continue
                rec_type = rec.get("type", "")
                if rec_type not in ("user", "assistant"):
                    continue
                msg = rec.get("message", {})
                content = msg.get("content") or []
                text = _extract_text_from_content(content).strip()
                if text:
                    texts.append(text)
    except Exception:
        pass
    return texts


# ---------------------------------------------------------------------------
# Session scanner
# ---------------------------------------------------------------------------


def scan_sessions(days: int, out_path: Path) -> dict[str, dict]:
    """Walk recent JSONL transcripts and score each session for salience.

    For each session:
    - Computes the MAX salience score over all user + assistant messages.
    - Unions all marker categories across those messages.
    - Captures a ~120-char snippet from the highest-salience message.

    Only sessions with ``score >= 0.5`` are included in the output.

    Writes results to ``out_path`` as JSON and returns the same dict.

    Args:
        days: How many days back from now to consider (based on file mtime).
        out_path: Destination JSON file path.

    Returns:
        Mapping of session_id -> ``{score, markers, project, snippet}``.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # session_id -> accumulator
    # We use file path as fallback session_id when the record lacks sessionId.
    session_data: dict[str, dict] = defaultdict(lambda: {
        "score": 0.0,
        "markers": set(),
        "project": "unknown",
        "snippet": "",
        "_best_text": "",   # internal: text of highest-score message so far
    })

    if not PROJECTS_DIR.exists():
        _write_output({}, out_path)
        return {}

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        project_name = project_dir.name

        for jsonl_file in project_dir.glob("*.jsonl"):
            try:
                mtime = datetime.fromtimestamp(
                    jsonl_file.stat().st_mtime, tz=timezone.utc
                )
                if mtime < cutoff:
                    continue
            except Exception:
                continue

            # Determine session_id from file name (stem) as fallback; override
            # from record data below.
            file_sid = jsonl_file.stem

            try:
                with jsonl_file.open(encoding="utf-8", errors="replace") as fh:
                    for raw_line in fh:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            rec = json.loads(raw_line)
                        except Exception:
                            continue

                        rec_type = rec.get("type", "")
                        if rec_type not in ("user", "assistant"):
                            continue

                        sid = rec.get("sessionId") or file_sid
                        msg = rec.get("message", {})
                        content = msg.get("content") or []
                        text = _extract_text_from_content(content).strip()
                        if not text:
                            continue

                        score = salience_score(text)
                        markers = classify_markers(text)
                        acc = session_data[sid]
                        acc["project"] = project_name

                        if score > acc["score"]:
                            acc["score"] = score
                            acc["_best_text"] = text

                        for m in markers:
                            acc["markers"].add(m)

            except Exception:
                continue

    # Build clean output — only sessions with score >= 0.5
    result: dict[str, dict] = {}
    for sid, acc in session_data.items():
        if acc["score"] < 0.5:
            continue
        snippet = (acc["_best_text"] or "")[:120]
        result[sid] = {
            "score": round(acc["score"], 4),
            "markers": sorted(acc["markers"]),
            "project": acc["project"],
            "snippet": snippet,
        }

    _write_output(result, out_path)
    return result


def _write_output(data: dict, path: Path) -> None:
    """Safely write *data* as JSON to *path*, creating parent dirs as needed.

    Args:
        data: Serialisable dict to write.
        path: Destination file path.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[salience] WARNING: could not write {path}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point — scan sessions and print top-5 salient events."""
    parser = argparse.ArgumentParser(
        description="Salience scorer — amygdala analog for the self-learning OS"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Days of history to scan (default: 7)",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help=f"Output JSON path (default: {DEFAULT_OUT})",
    )
    args = parser.parse_args()

    out_path = Path(args.out)
    print(f"[salience] Scanning last {args.days} days ...", file=sys.stderr)

    events = scan_sessions(days=args.days, out_path=out_path)

    print(f"[salience] {len(events)} high-salience sessions -> {out_path}", file=sys.stderr)

    if not events:
        print("[salience] No high-salience events found.", file=sys.stderr)
        return

    # Print top-5 sorted by score descending
    top5 = sorted(events.items(), key=lambda kv: -kv[1]["score"])[:5]
    print("\n[salience] Top salient events:", file=sys.stderr)
    for rank, (sid, info) in enumerate(top5, start=1):
        print(
            f"  {rank}. score={info['score']:.2f} [{', '.join(info['markers'])}]"
            f" project={info['project']}"
            f"\n     snippet: {info['snippet'][:80]!r}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
