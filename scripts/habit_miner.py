#!/usr/bin/env python3
"""habit_miner.py — Mine JSONL tool sequences for procedural memory (habit engine).

Mirrors the basal ganglia cue->routine->reward loop:
- Cue: project context (cwd)
- Routine: recurring ordered tool sequence (n-gram, length 2-4)
- Reward: completion without correction (implied by recurrence)

Graduation ladder: detected (>=4 occurrences, >=2 sessions) -> suggested_skill -> automation.

Usage (standalone):
    python habit_miner.py                 # analyzes last 14 days
    python habit_miner.py --days 30
    python habit_miner.py --out path.json
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

PROJECTS_DIR = Path.home() / ".claude" / "projects"
LOGS_DIR = Path.home() / ".claude" / "logs"
DEFAULT_HABITS_PATH = LOGS_DIR / "habits.json"

# Pure navigation tools — sequences made only of these are trivial noise
READ_ONLY_TOOLS = {"Read", "Glob", "Grep", "WebSearch", "WebFetch",
                   "ListMcpResourcesTool", "ReadMcpResourceTool"}

# File/bookkeeping primitives — "function words" of coding (high freq, low info).
# A routine with no token outside CHURN is pure noise.
CHURN = READ_ONLY_TOOLS | {"Edit", "Write", "NotebookEdit", "TodoWrite", "Read"}

MIN_COUNT = 4       # minimum total occurrences across all sessions
MIN_SESSIONS = 2    # minimum distinct sessions
NGRAM_SIZES = (2, 3, 4)

# Time-gap threshold: gap > GAP_MINUTES between consecutive tool-turns = context switch
GAP_MINUTES = 20


# ---------------------------------------------------------------------------
# Design change 1: Tool signature extraction
# ---------------------------------------------------------------------------

def _bash_verb(cmd: str) -> str:
    """Extract the meaningful verb from a bash command string.

    Skips leading 'cd' parts and compound commands, returning the first
    non-cd executable name (truncated to 12 chars).

    Args:
        cmd: Raw bash command string.

    Returns:
        Short verb token such as 'git', 'pytest', 'python'.
    """
    cmd = (cmd or "").strip()
    for part in re.split(r"&&|\|", cmd):
        part = part.strip()
        words = part.split()
        w = words[0] if words else ""
        if w and w != "cd":
            return re.split(r"[/\\]", w)[-1][:12]
    return "cd"


def tool_signature(name: str, tool_input: dict | None = None) -> str:
    """Convert a tool call into a meaningful signature token.

    Bash -> Bash:{verb}, Skill -> Skill:{name}, Task/Agent -> Agent:{type},
    mcp__a__b -> b, everything else -> name unchanged.

    Args:
        name: Raw tool name from the JSONL transcript.
        tool_input: The input dict passed to the tool call (may be None).

    Returns:
        Short human-readable signature token for the tool invocation.
    """
    inp = tool_input or {}
    if name == "Bash":
        return f"Bash:{_bash_verb(inp.get('command', ''))}"
    if name == "Skill":
        s = (inp.get("skill", "") or "").split(":")[-1][:15]
        return f"Skill:{s}"
    if name in ("Task", "Agent"):
        return f"Agent:{(inp.get('subagent_type', '?') or '?')[:12]}"
    if name.startswith("mcp__"):
        return name.split("__")[-1][:20]
    return name


# ---------------------------------------------------------------------------
# Design change 3: Action-token filter
# ---------------------------------------------------------------------------

def _is_action(tok: str) -> bool:
    """Return True if token is a meaningful action (not pure file-bookkeeping churn).

    Args:
        tok: Signature token to test.

    Returns:
        True if token carries workflow signal.
    """
    return tok not in CHURN


_TURN_BREAK = "__BREAK__"


def _split_on_break(tools: list[str]) -> list[list[str]]:
    """Split a tool list on turn-break sentinels, returning contiguous segments.

    Args:
        tools: Flat list of tool tokens, possibly containing '__BREAK__' sentinels.

    Returns:
        List of non-empty token lists, one per segment.
    """
    segments, current = [], []
    for t in tools:
        if t == _TURN_BREAK:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(t)
    if current:
        segments.append(current)
    return segments or [[]]


def extract_ngrams(seq: Sequence[str], n: int) -> list[tuple]:
    """Return all contiguous n-grams of length n from seq.

    Args:
        seq: Ordered sequence of tool names.
        n: Length of each n-gram window.

    Returns:
        List of n-tuples extracted with a sliding window.
    """
    if len(seq) < n:
        return []
    return [tuple(seq[i:i + n]) for i in range(len(seq) - n + 1)]


def _is_trivial(routine: tuple) -> bool:
    """Return True if the routine is navigational noise not worth tracking.

    A routine is trivial if:
    - All tools are identical (single-tool repetition).
    - All tools belong to the CHURN set (no real action token present).

    Args:
        routine: Tuple of tool signature tokens to evaluate.

    Returns:
        True if trivial, False if meaningful.
    """
    if len(set(routine)) == 1:
        return True  # all-same tool
    # Trivial if it contains NO action token at all
    if not any(_is_action(t) for t in routine):
        return True
    return False


@dataclass
class HabitCandidate:
    """A recurring ordered tool sequence detected across sessions.

    Attributes:
        project: Project name (cwd-derived directory name).
        cue: Contextual trigger — same as project for now; extensible to opening action.
        routine: Ordered tuple of tool names forming the habit.
        count: Total occurrences of this n-gram across all sessions.
        session_count: Number of distinct sessions containing this n-gram.
        last_seen: ISO timestamp of the most recent occurrence.
        strength: Numeric habit strength (count * recency_factor * reward_ratio).
        distinctiveness: count * mean IDF of tokens — sorts rarer workflows higher.
        reward_ratio: Fraction of occurrences NOT followed by a user correction.
        status: Graduation ladder stage — "detected" | "suggested_skill" | "automation".
    """

    project: str
    cue: str
    routine: tuple
    count: int
    session_count: int
    last_seen: str
    strength: float
    distinctiveness: float = 0.0
    reward_ratio: float = 1.0
    status: str = "detected"


def mine_habits(sessions: dict) -> list[HabitCandidate]:
    """Mine habit candidates from a sessions dictionary.

    Accepts both NEW shape (segments + corrected_segments) and OLD shape
    (flat tools list, optionally with '__BREAK__' sentinels for backward compat).

    New shape per session value::

        {
            "project": str,
            "segments": list[list[str]],        # pre-split segments
            "last_seen": str,                   # ISO timestamp
            "corrected_segments": set[int],     # segment indices that ended in correction
        }

    Old shape (backward compat)::

        {
            "project": str,
            "tools": list[str],   # flat list, "__BREAK__" = segment boundary
            "last_seen": str,
        }

    Args:
        sessions: Mapping of session_id -> session dict (new or old shape).

    Returns:
        List of HabitCandidates meeting the threshold, sorted by distinctiveness
        descending.
    """
    ngram_total: Counter = Counter()
    ngram_rewarded: Counter = Counter()     # occurrences from non-corrected segments
    ngram_sessions: dict[tuple, set] = defaultdict(set)
    ngram_project_votes: dict[tuple, Counter] = defaultdict(Counter)
    ngram_last_seen: dict[tuple, str] = {}
    # For IDF: number of session IDs that contain each token
    token_doc_freq: Counter = Counter()

    now_iso = datetime.now(timezone.utc).isoformat()

    for sid, info in sessions.items():
        project = info.get("project", "unknown")
        last_seen = info.get("last_seen", now_iso) or now_iso

        # Resolve segments — new shape vs old shape
        if "segments" in info:
            segments: list[list[str]] = info["segments"] or [[]]
            corrected: set[int] = info.get("corrected_segments") or set()
        else:
            # Old backward-compat: split flat tools list on __BREAK__
            tools = info.get("tools") or []
            segments = _split_on_break(tools)
            corrected = set()

        # Collect per-session unique tokens for IDF
        session_tokens: set[str] = set()
        for segment in segments:
            session_tokens.update(segment)
        for tok in session_tokens:
            token_doc_freq[tok] += 1

        for seg_idx, segment in enumerate(segments):
            is_corrected_seg = seg_idx in corrected
            for n in NGRAM_SIZES:
                for gram in extract_ngrams(segment, n):
                    if _is_trivial(gram):
                        continue
                    ngram_total[gram] += 1
                    if not is_corrected_seg:
                        ngram_rewarded[gram] += 1
                    ngram_sessions[gram].add(sid)
                    ngram_project_votes[gram][project] += 1
                    if gram not in ngram_last_seen or last_seen > ngram_last_seen[gram]:
                        ngram_last_seen[gram] = last_seen

    n_sessions = max(len(sessions), 1)

    def idf(token: str) -> float:
        df = token_doc_freq.get(token, 0)
        if df == 0:
            return 0.0
        # Smoothed IDF: log(1 + N/df) — always > 0, preserves relative ordering,
        # prevents collapse when a token appears in every session.
        return math.log(1 + n_sessions / df)

    habits: list[HabitCandidate] = []
    for gram, count in ngram_total.items():
        sess_count = len(ngram_sessions[gram])
        if count < MIN_COUNT or sess_count < MIN_SESSIONS:
            continue

        project = ngram_project_votes[gram].most_common(1)[0][0]

        # --- Design change 4: IDF distinctiveness ---
        mean_idf = sum(idf(t) for t in gram) / len(gram)
        distinctiveness = count * mean_idf

        # --- Design change 5: Reward ratio ---
        rewarded = ngram_rewarded.get(gram, count)  # default to full if no correction data
        reward_ratio = rewarded / count if count > 0 else 1.0

        # --- Design change 6: Strength with recency + reward ---
        last_seen_str = ngram_last_seen.get(gram, "")
        recency_factor = 1.0
        try:
            if last_seen_str:
                last_dt = datetime.fromisoformat(last_seen_str.replace("Z", "+00:00"))
                now_dt = datetime.now(timezone.utc)
                # clamp age >= 0 so future/skewed timestamps can't inflate strength
                age_days = max(0.0, (now_dt - last_dt).total_seconds() / 86400.0)
                recency_factor = 0.5 ** (age_days / 30.0)
        except Exception:
            recency_factor = 1.0

        strength = count * recency_factor * reward_ratio

        habits.append(HabitCandidate(
            project=project,
            cue=project,
            routine=gram,
            count=count,
            session_count=sess_count,
            last_seen=last_seen_str,
            strength=strength,
            distinctiveness=distinctiveness,
            reward_ratio=reward_ratio,
            status="detected",
        ))

    # Sort by distinctiveness descending (rarer, more action-heavy workflows first)
    habits.sort(key=lambda h: -h.distinctiveness)

    # -----------------------------------------------------------------------
    # Cross-project detection: routines in ≥2 distinct projects → universal patterns
    # -----------------------------------------------------------------------
    cross_habits = _mine_cross_project(
        ngram_total, ngram_rewarded, ngram_sessions, ngram_project_votes,
        ngram_last_seen, idf,
    )
    habits.extend(cross_habits)

    return habits


def _mine_cross_project(
    ngram_total: Counter,
    ngram_rewarded: Counter,
    ngram_sessions: dict,
    ngram_project_votes: dict,
    ngram_last_seen: dict,
    idf,
) -> list[HabitCandidate]:
    """Emit universal habit candidates for routines observed in ≥2 distinct projects.

    A routine that recurs across multiple projects is a strong signal for a
    general skill — not a project-specific quirk. These are emitted as separate
    HabitCandidates with ``project="_cross_project"`` and a distinctiveness bonus
    proportional to the number of projects involved.

    Args:
        ngram_total: Counter of gram -> total occurrences.
        ngram_rewarded: Counter of gram -> non-corrected occurrences.
        ngram_sessions: Mapping gram -> set of session IDs.
        ngram_project_votes: Mapping gram -> Counter of project -> occurrence count.
        ngram_last_seen: Mapping gram -> ISO timestamp string.
        idf: Callable token -> float IDF weight.

    Returns:
        List of cross-project HabitCandidates, sorted by distinctiveness desc.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    cross: list[HabitCandidate] = []

    for gram, proj_votes in ngram_project_votes.items():
        if len(proj_votes) < 2:
            continue  # single-project routine — handled in main loop

        total = ngram_total[gram]
        sess_count = len(ngram_sessions[gram])
        if total < MIN_COUNT or sess_count < MIN_SESSIONS:
            continue

        mean_idf = sum(idf(t) for t in gram) / len(gram)
        # Distinctiveness boosted by project spread: universal patterns rank higher
        n_projects = len(proj_votes)
        distinctiveness = total * mean_idf * (1.0 + 0.5 * (n_projects - 1))

        rewarded = ngram_rewarded.get(gram, total)
        reward_ratio = rewarded / total if total > 0 else 1.0

        last_seen_str = ngram_last_seen.get(gram, now_iso)
        recency_factor = 1.0
        try:
            if last_seen_str:
                last_dt = datetime.fromisoformat(last_seen_str.replace("Z", "+00:00"))
                age_days = max(0.0, (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400.0)
                recency_factor = 0.5 ** (age_days / 30.0)
        except Exception:
            pass

        strength = total * recency_factor * reward_ratio

        cross.append(HabitCandidate(
            project="_cross_project",
            cue="_cross_project",
            routine=gram,
            count=total,
            session_count=sess_count,
            last_seen=last_seen_str,
            strength=strength,
            distinctiveness=distinctiveness,
            reward_ratio=reward_ratio,
            status="detected",
        ))

    cross.sort(key=lambda h: -h.distinctiveness)
    return cross


def save_habits(habits: list[HabitCandidate], path: Path = DEFAULT_HABITS_PATH) -> None:
    """Serialize habit candidates to a JSON file.

    Args:
        habits: List of HabitCandidates to persist.
        path: Destination file path (parent directories created if absent).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = []
    for h in habits:
        d = asdict(h)
        d["routine"] = list(h.routine)  # tuple -> list for JSON serialization
        data.append(d)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_habits(path: Path = DEFAULT_HABITS_PATH) -> list[dict]:
    """Load persisted habit candidates from a JSON file.

    Args:
        path: Source file path.

    Returns:
        List of habit dicts, or empty list if file absent or unreadable.
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Correction detection helpers (for parse_jsonl_for_habits)
# ---------------------------------------------------------------------------

_CORRECTION_PHRASES = ("не", "грешно", "wrong", "стоп", "не така", "no that")


def _is_correction(text: str) -> bool:
    """Return True if a user message looks like a correction of the previous turn.

    Uses a minimal heuristic: short message (<200 chars) containing one of the
    known correction phrases (case-insensitive).

    Args:
        text: User message text.

    Returns:
        True if it looks like a correction.
    """
    if len(text) >= 200:
        return False
    text_lower = text.lower()
    return any(phrase in text_lower for phrase in _CORRECTION_PHRASES)


def parse_jsonl_for_habits(days: int) -> dict:
    """Parse JSONL transcripts and extract tool call sequences per session.

    Walks all project directories under PROJECTS_DIR, filtering by file mtime
    within the last ``days`` days.  Uses tool_signature() for each tool call and
    splits sessions into segments on idle gaps > GAP_MINUTES between consecutive
    assistant turns.  Detects corrected segments when a short correction message
    follows a segment.

    Args:
        days: How many days back to look.

    Returns:
        Mapping of session_id -> NEW shape dict:
        {"project": str, "segments": list[list[str]], "last_seen": str,
         "corrected_segments": set[int]}.
    """
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Internal per-session state while reading
    # {sid: {"project", "segments", "last_seen", "corrected_segments",
    #        "_current_seg", "_last_ts", "_pending_correction"}}
    raw: dict = defaultdict(lambda: {
        "project": "unknown",
        "segments": [],
        "last_seen": "",
        "corrected_segments": set(),
        "_current_seg": [],
        "_last_ts": None,        # datetime or None
        "_pending_correction": False,  # True if last user msg was a correction
    })

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        project_name = project_dir.name
        for jsonl_file in project_dir.glob("*.jsonl"):
            try:
                mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    continue
            except Exception:
                continue
            try:
                with jsonl_file.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue

                        rec_type = rec.get("type")
                        sid = rec.get("sessionId", str(jsonl_file))
                        ts_raw = rec.get("timestamp", "")
                        state = raw[sid]
                        state["project"] = project_name

                        # Parse timestamp once
                        rec_dt: datetime | None = None
                        try:
                            if ts_raw:
                                rec_dt = datetime.fromisoformat(
                                    ts_raw.replace("Z", "+00:00"))
                        except Exception:
                            pass

                        if rec_type == "assistant":
                            msg = rec.get("message", {})
                            content = msg.get("content") or []

                            tools_this_turn = []
                            for item in content:
                                if (isinstance(item, dict)
                                        and item.get("type") == "tool_use"
                                        and item.get("name", "")):
                                    sig = tool_signature(
                                        item["name"],
                                        item.get("input") or {}
                                    )
                                    tools_this_turn.append(sig)

                            if not tools_this_turn:
                                continue

                            # Design change 2: time-gap segmentation
                            last_dt = state["_last_ts"]
                            if (last_dt is not None
                                    and rec_dt is not None
                                    and (rec_dt - last_dt).total_seconds() > GAP_MINUTES * 60):
                                # Gap exceeded — close current segment
                                if state["_current_seg"]:
                                    # If a pending correction was flagged, mark this segment
                                    seg_idx = len(state["segments"])
                                    state["segments"].append(state["_current_seg"])
                                    if state["_pending_correction"]:
                                        state["corrected_segments"].add(seg_idx)
                                    state["_current_seg"] = []
                                    state["_pending_correction"] = False

                            state["_current_seg"].extend(tools_this_turn)
                            if rec_dt is not None:
                                state["_last_ts"] = rec_dt
                            if ts_raw > state["last_seen"]:
                                state["last_seen"] = ts_raw

                        elif rec_type == "user":
                            # Check for correction signal
                            msg = rec.get("message", {})
                            content = msg.get("content") or []
                            text = ""
                            if isinstance(content, str):
                                text = content
                            elif isinstance(content, list):
                                for item in content:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        text += item.get("text", "")
                                    elif isinstance(item, str):
                                        text += item
                            if _is_correction(text):
                                state["_pending_correction"] = True

            except Exception:
                continue

    # Flush remaining open segments
    result: dict = {}
    for sid, state in raw.items():
        if state["_current_seg"]:
            seg_idx = len(state["segments"])
            state["segments"].append(state["_current_seg"])
            if state["_pending_correction"]:
                state["corrected_segments"].add(seg_idx)
        result[sid] = {
            "project": state["project"],
            "segments": state["segments"],
            "last_seen": state["last_seen"],
            "corrected_segments": state["corrected_segments"],
        }

    return result


def main() -> None:
    """CLI entry point for standalone habit mining."""
    parser = argparse.ArgumentParser(description="Habit miner — procedural memory engine")
    parser.add_argument("--days", type=int, default=14, help="Days of history to analyze (default: 14)")
    parser.add_argument("--out", default=str(DEFAULT_HABITS_PATH), help="Output JSON path")
    args = parser.parse_args()

    print(f"[habit_miner] Parsing last {args.days} days...", file=sys.stderr)
    sessions = parse_jsonl_for_habits(args.days)
    habits = mine_habits(sessions)
    save_habits(habits, path=Path(args.out))
    print(f"[habit_miner] {len(habits)} habits found -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
