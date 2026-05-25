"""wiki_freshness_check.py

Scan all projects in the Obsidian wiki ecosystem and report how stale
their knowledge_graph.json and wiki/log.md files are.

Usage:
    python wiki_freshness_check.py
    python wiki_freshness_check.py --threshold 14
    python wiki_freshness_check.py --json

Exit code: 0 = all fresh, 1 = at least one project is stale.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WIKI_MAP_PATH = Path(r"{{WIKI_PATH}}\_shared\wiki-map.json")
DEFAULT_THRESHOLD = 14  # days

STATUS_OK = "✓"   # ✓
STATUS_WARN = "⚠"  # ⚠
STATUS_MISS = "✗"  # ✗

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class ProjectReport:
    """Holds freshness data for a single project."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.status: str = "active"   # active | dormant | missing-wiki
        self.graph_exists: bool = False
        self.graph_age_days: Optional[int] = None
        self.graph_source: str = ""   # "meta", "mtime", "missing"
        self.log_exists: bool = False
        self.log_age_days: Optional[int] = None
        self.log_source: str = ""     # "header", "mtime", "missing"

    def flag(self, threshold: int) -> str:
        """Return a single status character for this project."""
        # Dormant projects are intentionally inactive — never flag them.
        if self.status == "dormant":
            return STATUS_OK
        if not self.graph_exists:
            return STATUS_MISS
        if self.graph_age_days is not None and self.graph_age_days > threshold:
            return STATUS_WARN
        if self.log_age_days is not None and self.log_age_days > threshold:
            return STATUS_WARN
        return STATUS_OK

    def is_stale(self, threshold: int) -> bool:
        # missing-wiki is a separate concern (needs scaffolding), not "stale".
        if self.status in ("dormant", "missing-wiki"):
            return False
        return self.flag(threshold) != STATUS_OK

    def to_dict(self) -> dict:
        return {
            "project": self.name,
            "status": self.status,
            "graph_exists": self.graph_exists,
            "graph_age_days": self.graph_age_days,
            "graph_source": self.graph_source,
            "log_exists": self.log_exists,
            "log_age_days": self.log_age_days,
            "log_source": self.log_source,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_wiki_map(path: Path) -> tuple[dict[str, str], str, str]:
    """Return (mapping, base_code_path, base_wiki_path) from wiki-map.json."""
    raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    return (
        raw.get("mapping", {}),
        raw.get("base_code_path", r"{{CODE_PATH}}"),
        raw.get("base_wiki_path", r"{{WIKI_PATH}}"),
    )


def _days_ago(d: date) -> int:
    return (date.today() - d).days


def _mtime_date(path: Path) -> date:
    return datetime.fromtimestamp(path.stat().st_mtime).date()


# ISO date patterns: "2026-04-11" or "2026-04-11 (updated 2026-04-11 Q4)"
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# Log-entry header dates: "## [YYYY-MM-DD]" / "### YYYY-MM-DD" / "## YYYY-MM-DD"
_DATE_HEADER_RE = re.compile(r"^#{1,6}\s+\[?(\d{4}-\d{2}-\d{2})", re.MULTILINE)


def _parse_iso(text: str) -> Optional[date]:
    """Parse first ISO date in text, ignoring future dates (deadlines/plans)."""
    today = date.today()
    for m in _ISO_DATE_RE.finditer(text):
        try:
            parsed = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if parsed <= today:  # clamp: future dates are not "last activity"
            return parsed
    return None


def check_graph(wiki_root: Path) -> tuple[bool, Optional[int], str]:
    """Return (exists, age_days, source) for knowledge_graph.json."""
    graph_file = wiki_root / "graph" / "knowledge_graph.json"
    if not graph_file.exists():
        return False, None, "missing"

    # Try meta.last_updated, then meta.generated, then file mtime
    try:
        data = json.loads(graph_file.read_text(encoding="utf-8", errors="replace"))
        meta: dict = data.get("meta", {})

        for field in ("last_updated", "generated"):
            val = meta.get(field)
            if val and isinstance(val, str):
                parsed = _parse_iso(val)
                if parsed:
                    return True, _days_ago(parsed), field
    except (json.JSONDecodeError, OSError):
        pass

    # Fallback: file mtime
    age = _days_ago(_mtime_date(graph_file))
    return True, age, "mtime"


def check_log(wiki_dir: Path) -> tuple[bool, Optional[int], str]:
    """Return (exists, age_days, source) for wiki/log.md."""
    log_file = wiki_dir / "log.md"
    if not log_file.exists():
        return False, None, "missing"

    # Find last date — prefer log-entry headers (## [YYYY-MM-DD]); clamp future dates.
    text = log_file.read_text(encoding="utf-8", errors="replace")
    today = date.today()
    header_dates = _DATE_HEADER_RE.findall(text)
    raw_dates = header_dates if header_dates else _ISO_DATE_RE.findall(text)
    valid: list[date] = []
    for d in raw_dates:
        try:
            parsed = date.fromisoformat(d)
        except ValueError:
            continue
        if parsed <= today:  # ignore future deadlines / planned dates
            valid.append(parsed)
    if valid:
        last = max(valid)
        return True, _days_ago(last), "header" if header_dates else "content"

    # Fallback: mtime
    age = _days_ago(_mtime_date(log_file))
    return True, age, "mtime"


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------


def _load_metadata(path: Path) -> dict:
    """Read the optional per-project 'metadata' block from wiki-map.json."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return raw.get("metadata", {}) or {}
    except Exception:
        return {}


def scan_projects(threshold: int) -> list[ProjectReport]:
    mapping, _, base_wiki = load_wiki_map(WIKI_MAP_PATH)
    metadata = _load_metadata(WIKI_MAP_PATH)
    base_wiki_path = Path(base_wiki)
    reports: list[ProjectReport] = []

    for _code_name, wiki_name in sorted(mapping.items()):
        wiki_root = base_wiki_path / wiki_name
        rpt = ProjectReport(wiki_name)
        rpt.status = (metadata.get(wiki_name, {}) or {}).get("status", "active")

        exists, age, src = check_graph(wiki_root)
        rpt.graph_exists = exists
        rpt.graph_age_days = age
        rpt.graph_source = src

        log_exists, log_age, log_src = check_log(wiki_root / "wiki")
        rpt.log_exists = log_exists
        rpt.log_age_days = log_age
        rpt.log_source = log_src

        reports.append(rpt)

    return reports


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------


def _age_str(days: Optional[int], source: str) -> str:
    if days is None:
        return "—"
    suffix = f" ({source})" if source not in ("meta", "header") else ""
    return f"{days}d{suffix}"


def print_table(reports: list[ProjectReport], threshold: int) -> None:
    col_w = [24, 10, 14, 12, 6]
    header = ["Project", "Graph", "Graph age", "Log age", "Flag"]
    sep = "  "

    def row_fmt(cols: list[str]) -> str:
        return sep.join(c.ljust(col_w[i]) for i, c in enumerate(cols))

    print(row_fmt(header))
    print(row_fmt(["-" * w for w in col_w]))

    for rpt in reports:
        graph_status = "ok" if rpt.graph_exists else "MISSING"
        graph_age = _age_str(rpt.graph_age_days, rpt.graph_source)
        log_age = _age_str(rpt.log_age_days, rpt.log_source)
        flag = rpt.flag(threshold)
        print(row_fmt([rpt.name, graph_status, graph_age, log_age, flag]))

    stale_count = sum(r.is_stale(threshold) for r in reports)
    print()
    print(
        f"Threshold: {threshold} days | Projects: {len(reports)} | "
        f"Stale: {stale_count}"
    )


def build_json_payload(reports: list[ProjectReport], threshold: int) -> dict:
    """Return the JSON payload combining legacy and new fields."""
    legacy = {
        "threshold_days": threshold,
        "today": date.today().isoformat(),
        "projects": [r.to_dict() for r in reports],
        "stale_count": sum(r.is_stale(threshold) for r in reports),
    }

    new_fields = {
        "checked": datetime.now().isoformat(timespec="seconds"),
        "stale": [r.to_dict() for r in reports if r.is_stale(threshold)],
        "fresh_count": sum(1 for r in reports if not r.is_stale(threshold)),
        "total": len(reports),
    }

    return {**legacy, **new_fields}


def print_json(reports: list[ProjectReport], threshold: int) -> None:
    print(json.dumps(build_json_payload(reports, threshold), indent=2, ensure_ascii=False))


def write_stale_summary(reports: list[ProjectReport], threshold: int, out_dir: Path) -> None:
    """Write a compact stale-projects.txt for fast Phase 0 reads."""
    stale_path = out_dir / "stale-projects.txt"
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale = [r for r in reports if r.is_stale(threshold)]
    with stale_path.open("w", encoding="utf-8") as f:
        f.write("# stale-projects.txt — auto-generated\n")
        f.write("# Format: project_name|age_or_status|file\n")
        f.write(f"# Updated: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"# Threshold: {threshold} days. Total stale: {len(stale)}/{len(reports)}\n")
        for r in stale:
            # Prefer non-None age; mark missing files explicitly
            if not r.graph_exists:
                f.write(f"{r.name}|missing|graph\n")
            elif r.graph_age_days is not None and r.graph_age_days > threshold:
                f.write(f"{r.name}|{r.graph_age_days}|graph\n")
            elif r.log_age_days is not None and r.log_age_days > threshold:
                f.write(f"{r.name}|{r.log_age_days}|log\n")
            else:
                # Fallback: mark unknown but stale (shouldn't normally hit)
                f.write(f"{r.name}|unknown|?\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Check freshness of Obsidian wiki knowledge graphs and logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        metavar="DAYS",
        help=f"Flag projects older than DAYS days (default: {DEFAULT_THRESHOLD})",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output machine-readable JSON instead of a table",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write JSON output to this file (overwrites)",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()

    if not WIKI_MAP_PATH.exists():
        print(f"ERROR: wiki-map.json not found at {WIKI_MAP_PATH}", file=sys.stderr)
        sys.exit(2)

    reports = scan_projects(args.threshold)

    # Always write compact stale-projects.txt for Phase 0 fast reads
    logs_dir = Path.home() / ".claude" / "logs"
    try:
        write_stale_summary(reports, args.threshold, logs_dir)
    except Exception as exc:
        print(f"WARN: could not write stale-projects.txt: {exc}", file=sys.stderr)

    if args.as_json or args.out:
        out_text = json.dumps(
            build_json_payload(reports, args.threshold),
            indent=2,
            ensure_ascii=False,
        )
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(out_text, encoding="utf-8")
        if args.as_json:
            print(out_text)
    else:
        print_table(reports, args.threshold)

    any_stale = any(r.is_stale(args.threshold) for r in reports)
    sys.exit(1 if any_stale else 0)


if __name__ == "__main__":
    main()
