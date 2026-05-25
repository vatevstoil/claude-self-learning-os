"""Boris draft generator.

Turns recurring user corrections (from boris-candidates.json) into
ready-to-review CLAUDE.md rule drafts. Never writes to any CLAUDE.md
directly -- produces drafts only. Human approves before any rule lands.

Usage:
    python boris_draft.py
    python boris_draft.py --min-count 4
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_CANDIDATES = Path.home() / ".claude" / "logs" / "boris-candidates.json"
_DEFAULT_OUT_DIR = Path.home() / ".claude" / "logs" / "boris-drafts"

# ---------------------------------------------------------------------------
# Negation prefixes to strip when building a rule hint (Bulgarian + English)
# ---------------------------------------------------------------------------

_NEG_PREFIXES: list[str] = [
    "не прави",
    "не така,",
    "не така",
    "не използвай",
    "не забравяй",
    "не го",
    "не е ок",
    "не е вярно",
    "не е",
    "не мога",
    "не се",
    "не пропускай",
    "не ",
    "wrong ",
    "no ",
    "stop ",
    "нищо не работи",
    "нищо ",
]


def summarize_corrections(examples: list[str]) -> str:
    """Condense correction examples into a short candidate rule line.

    Takes the most informative example (longest), strips leading negation
    words, and returns a lowercased imperative hint.  The result is a
    best-effort heuristic -- not a perfect rule -- so the human reviewer can
    edit it before approving.

    Args:
        examples: List of raw correction strings from boris-candidates.json.

    Returns:
        A short, lowercased rule hint string.  Never empty.
    """
    # Coerce to clean list of strings (tolerate null / non-string elements)
    examples = [str(e) for e in (examples or []) if e is not None and str(e).strip()]
    if not examples:
        return "review corrections and define rule"

    # Pick the most informative example (longest non-empty string).
    candidate = max(examples, key=lambda x: len(x.strip()))
    text = candidate.strip().lower()

    # Strip known negation prefixes.
    for prefix in _NEG_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    # Trim to a sensible length so the rule hint stays readable.
    if len(text) > 120:
        # Cut at the last word boundary before 120 chars.
        cut = text[:120].rsplit(" ", 1)[0]
        text = cut + " ..."

    return text if text else "review corrections and define rule"


def _encode_to_path_hint(project_key: str) -> str:
    """Convert a boris-candidates project key to a human-readable path hint.

    Boris encodes paths as ``<Drive>--<Org>-<Project>``:
    - Leading ``X--`` maps to ``X:\\``
    - Double dashes ``--`` inside map to ``\\``
    - The first single dash after the org segment separates org from project,
      so ``J--Antigraviti-Facturka-bg``  -> ``J:\\Antigraviti\\Facturka-bg``
      and ``J--Antigraviti-Davinci-Plugin-DCTL`` -> ``J:\\Antigraviti\\Davinci-Plugin-DCTL``

    The heuristic: after stripping the drive prefix and expanding ``--`` to
    ``\\``, split each resulting path segment on its *first* single dash only
    to separate ``Org`` from ``Project``; single dashes inside the project
    name are preserved (e.g. ``Facturka-bg`` or ``Davinci-Plugin-DCTL``).

    Args:
        project_key: Raw key from boris-candidates.json.

    Returns:
        A Windows-style path hint string.
    """
    # Case-insensitive match for drive prefix "X--rest"
    drive_match = re.match(r"^([A-Za-z])--(.+)$", project_key)
    if not drive_match:
        return project_key

    drive = drive_match.group(1).upper()
    rest = drive_match.group(2)

    # Expand any remaining "--" (nested double-dash separators) to "\".
    rest = rest.replace("--", "\\")

    # For each path segment (split by "\"), further split on the FIRST
    # single dash to separate org from project name.
    # e.g. "Antigraviti-Facturka-bg"      -> ["Antigraviti", "Facturka-bg"]
    #      "Antigraviti-Davinci-Plugin-DCTL" -> ["Antigraviti", "Davinci-Plugin-DCTL"]
    #      "Obsidian-Resurch-Claude-Trading" -> ["Obsidian", "Resurch-Claude-Trading"]
    parts = rest.split("\\")
    expanded: list[str] = []
    for part in parts:
        dash_idx = part.find("-")
        if dash_idx > 0:
            expanded.append(part[:dash_idx])
            expanded.append(part[dash_idx + 1:])
        else:
            expanded.append(part)

    return drive + ":\\" + "\\".join(expanded)


def _safe_filename(project_key: str) -> str:
    """Return a filesystem-safe filename stem from a project key.

    Strips characters that are invalid in Windows filenames.

    Args:
        project_key: Raw project key.

    Returns:
        A safe filename stem (no extension).
    """
    safe = re.sub(r'[<>:"/\\|?*]', "_", project_key)
    return safe


def generate_draft(project: str, info: dict) -> str:  # type: ignore[type-arg]
    """Return a markdown draft string for a single project.

    The draft contains:
    - Project name
    - Proposed rule line (generated by summarize_corrections)
    - Verbatim evidence (all examples)
    - Target CLAUDE.md path hint

    This output is for human review only.  The tool never writes to any
    CLAUDE.md automatically.

    Args:
        project: Project key (as stored in boris-candidates.json).
        info: Dict with keys ``count`` (int) and ``examples`` (list[str]).

    Returns:
        A markdown string ready to save as a draft file.
    """
    raw_examples = info.get("examples") or []
    # Tolerate null / non-string elements in examples
    examples: list[str] = [str(e) for e in raw_examples if e is not None and str(e).strip()]
    try:
        count: int = int(info.get("count", len(examples)))
    except (TypeError, ValueError):
        count = len(examples)
    rule_hint = summarize_corrections(examples)
    path_hint = _encode_to_path_hint(project)
    target_path = f"{path_hint}\\CLAUDE.md"

    evidence_lines = "\n".join(f"- {ex}" for ex in examples)

    draft = f"""\
# Boris Draft -- {project}

> **STATUS: PENDING HUMAN REVIEW**
> Do NOT apply this rule without reading and editing it first.

## Proposed Rule

```
{rule_hint}
```

## Target File

`{target_path}`

_(If the path looks wrong due to key encoding, adjust manually.)_

## Evidence

Correction count in window: **{count}**

Examples (verbatim):

{evidence_lines}

## Instructions

1. Edit the proposed rule above to match project conventions.
2. Copy the final rule line into `{target_path}`.
3. Delete this draft once applied (or keep for audit trail).
"""
    return draft


def write_drafts(
    boris_path: Path,
    out_dir: Path,
    min_count: int = 4,
) -> list[Path]:
    """Write draft files for all projects meeting the count threshold.

    Args:
        boris_path: Path to boris-candidates.json.
        out_dir: Directory to write ``{project}.md`` drafts into.
        min_count: Minimum correction count to generate a draft (inclusive).

    Returns:
        List of Paths that were actually written.  Empty list on any error
        reading the input file.
    """
    try:
        raw = boris_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, dict):
        return []
    projects = data.get("projects") or {}
    if not isinstance(projects, dict):
        return []
    written: list[Path] = []

    def _safe_count(val: dict) -> int:
        try:
            return int(val.get("count", 0))
        except (TypeError, ValueError):
            return 0

    qualifying = {
        key: val for key, val in projects.items()
        if isinstance(val, dict) and _safe_count(val) >= min_count
    }

    if not qualifying:
        return []

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return []

    for project_key, info in qualifying.items():
        content = generate_draft(project_key, info)
        safe_name = _safe_filename(project_key)
        dest = out_dir / f"{safe_name}.md"
        try:
            dest.write_text(content, encoding="utf-8")
            written.append(dest)
        except OSError:
            continue

    return written


def main() -> None:
    """Entry point: read default candidates, write drafts, report to stderr."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate CLAUDE.md rule drafts from Boris correction candidates."
    )
    parser.add_argument(
        "--min-count",
        type=int,
        default=4,
        help="Minimum correction count to generate a draft (default: 4).",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=_DEFAULT_CANDIDATES,
        help=f"Path to boris-candidates.json (default: {_DEFAULT_CANDIDATES}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_DEFAULT_OUT_DIR,
        help=f"Directory for draft files (default: {_DEFAULT_OUT_DIR}).",
    )
    args = parser.parse_args()

    written = write_drafts(
        boris_path=args.input,
        out_dir=args.output_dir,
        min_count=args.min_count,
    )

    print(f"Boris drafts written: {len(written)}", file=sys.stderr)
    for path in written:
        print(f"  {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
