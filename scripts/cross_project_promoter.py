"""cross_project_promoter.py

Detect when newly-written content in one project's wiki could benefit other
projects, and suggest creating cross-links or _shared/ promotions.

DRY-RUN ONLY — never modifies any file.

Usage:
    python cross_project_promoter.py
    python cross_project_promoter.py --since-days 7
    python cross_project_promoter.py --project Cinemind
    python cross_project_promoter.py --project Cinemind --output suggestions.md
    python cross_project_promoter.py --threshold 0.2
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WIKI_MAP_PATH = Path(r"{{WIKI_PATH}}\_shared\wiki-map.json")
OBSIDIAN_BASE = Path(r"{{WIKI_PATH}}")
SHARED_DIR = OBSIDIAN_BASE / "_shared"

DEFAULT_SINCE_DAYS = 7
DEFAULT_THRESHOLD = 0.3
MAX_FILE_BYTES = 50 * 1024  # 50 KB
TOP_KEYWORDS = 15

# ---------------------------------------------------------------------------
# Stopwords (English + Bulgarian common words + Markdown syntax tokens)
# ---------------------------------------------------------------------------

_STOPWORDS: frozenset[str] = frozenset({
    # English
    "the", "and", "for", "with", "that", "this", "from", "are", "not",
    "but", "all", "use", "can", "has", "have", "been", "was", "its",
    "will", "you", "your", "our", "their", "they", "when", "where",
    "how", "what", "which", "also", "into", "out", "via", "any", "each",
    "should", "would", "could", "then", "than", "more", "add", "new",
    "set", "get", "per", "see", "one", "two", "may", "lets", "using",
    "used", "need", "file", "way", "both", "some", "same", "now",
    # Bulgarian
    "при", "за", "не", "да", "се", "на", "от", "ако", "как", "без",
    "след", "преди", "или", "но", "със", "към", "под", "над", "чрез",
    "тъй", "така", "вече", "като", "това", "тези", "него", "нея",
    # Markdown artefacts
    "true", "false", "null", "none", "http", "https", "www", "com",
    "org", "json", "yaml", "markdown", "code", "docs", "doc",
})

# ---------------------------------------------------------------------------
# Frontmatter helpers (reused from find_pattern.py conventions)
# ---------------------------------------------------------------------------

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FM_TAGS_RE = re.compile(r"^tags\s*:\s*(.+)$", re.MULTILINE)
_FM_TITLE_RE = re.compile(r"^title\s*:\s*(.+)$", re.MULTILINE | re.IGNORECASE)


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Regex-only, no PyYAML dependency."""
    m = _FM_RE.match(content)
    if not m:
        return {}, content
    fm_raw = m.group(1)
    body = content[m.end():]
    fm: dict = {}

    # tags
    tm = _FM_TAGS_RE.search(fm_raw)
    if tm:
        raw = tm.group(1).strip().strip("[]")
        fm["tags"] = [t.strip().strip("\"'") for t in raw.split(",") if t.strip()]

    # title
    ttm = _FM_TITLE_RE.search(fm_raw)
    if ttm:
        fm["title"] = ttm.group(1).strip().strip("\"'")

    return fm, body


def _h1_title(body: str) -> str:
    """Extract first H1 heading from body, else return empty string."""
    m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class WikiFile:
    """A single wiki markdown file with extracted metadata."""
    project: str           # wiki folder name, e.g. "Cinemind"
    path: Path
    rel_path: str          # relative to project wiki dir
    title: str
    tags: list[str]
    keywords: list[str]    # top-N keywords by frequency
    mtime: date
    token_set: frozenset[str]  # for Jaccard; keywords + tags lowercased

    def location_link(self) -> str:
        return f"[[{self.project}]]: {self.rel_path}"


@dataclass
class SharedFile:
    """An existing file under {{WIKI_PATH}}/_shared/."""
    path: Path
    rel_path: str          # relative to SHARED_DIR
    title: str
    tags: list[str]
    token_set: frozenset[str]


@dataclass
class CrossLinkSuggestion:
    """A suggestion to add a link from a new wiki file to an existing shared file."""
    wiki_file: WikiFile
    shared_file: SharedFile
    score: float           # Jaccard similarity


@dataclass
class SisterMatch:
    """A suggestion that another project's file is related to a recent file."""
    source: WikiFile       # the recently-modified file
    sister: WikiFile       # the related file in another project
    score: float


@dataclass
class PromotionCandidate:
    """A concept seen in 2+ recent project files — candidate for new _shared/ page."""
    keyword_cluster: str   # representative keyword phrase
    appearances: list[WikiFile] = field(default_factory=list)

    @property
    def project_set(self) -> set[str]:
        return {f.project for f in self.appearances}


# ---------------------------------------------------------------------------
# Tokenisation and similarity
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    """Lowercase alpha tokens >= 3 chars, excluding stopwords."""
    raw = re.findall(r"[a-zA-ZЀ-ӿ]{3,}", text.lower())
    return [t for t in raw if t not in _STOPWORDS]


def _keyword_set(tokens: list[str]) -> frozenset[str]:
    return frozenset(Counter(tokens).most_common(TOP_KEYWORDS * 3))


def _top_keywords(tokens: list[str], n: int = TOP_KEYWORDS) -> list[str]:
    return [w for w, _ in Counter(tokens).most_common(n)]


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# ---------------------------------------------------------------------------
# File loading helpers
# ---------------------------------------------------------------------------


def _safe_read(path: Path) -> Optional[str]:
    """Read file respecting MAX_FILE_BYTES limit. Returns None on failure."""
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            print(
                f"  SKIP (>{MAX_FILE_BYTES // 1024} KB): {path}",
                file=sys.stderr,
            )
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"  WARN: cannot read {path}: {exc}", file=sys.stderr)
        return None


def _file_mtime(path: Path) -> date:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date()


def _load_wiki_file(project: str, wiki_dir: Path, md_path: Path) -> Optional[WikiFile]:
    content = _safe_read(md_path)
    if content is None:
        return None

    fm, body = _parse_frontmatter(content)
    title = fm.get("title") or _h1_title(body) or md_path.stem
    tags: list[str] = fm.get("tags") or []

    tokens = _tokenize(body + " " + " ".join(tags) + " " + title)
    keywords = _top_keywords(tokens)
    token_set = frozenset(keywords)

    rel = str(md_path.relative_to(wiki_dir))

    return WikiFile(
        project=project,
        path=md_path,
        rel_path=rel,
        title=title,
        tags=tags,
        keywords=keywords,
        mtime=_file_mtime(md_path),
        token_set=token_set,
    )


def _load_shared_file(md_path: Path) -> Optional[SharedFile]:
    content = _safe_read(md_path)
    if content is None:
        return None

    fm, body = _parse_frontmatter(content)
    title = fm.get("title") or _h1_title(body) or md_path.stem
    tags: list[str] = fm.get("tags") or []

    tokens = _tokenize(body + " " + " ".join(tags) + " " + title)
    token_set = frozenset(_top_keywords(tokens, TOP_KEYWORDS * 2))

    rel = str(md_path.relative_to(SHARED_DIR))

    return SharedFile(
        path=md_path,
        rel_path=rel,
        title=title,
        tags=tags,
        token_set=token_set,
    )


# ---------------------------------------------------------------------------
# Project scanning
# ---------------------------------------------------------------------------


def load_wiki_map() -> tuple[dict[str, str], str, str]:
    raw = json.loads(WIKI_MAP_PATH.read_text(encoding="utf-8", errors="replace"))
    return (
        raw.get("mapping", {}),
        raw.get("base_code_path", r"{{CODE_PATH}}"),
        raw.get("base_wiki_path", r"{{WIKI_PATH}}"),
    )


def collect_recent_files(
    mapping: dict[str, str],
    base_wiki: str,
    since: date,
    project_filter: Optional[str],
) -> list[WikiFile]:
    """Walk all project wikis; return files modified on or after `since`."""
    recent: list[WikiFile] = []
    base = Path(base_wiki)

    for _code, wiki_name in sorted(mapping.items()):
        if project_filter and wiki_name.lower() != project_filter.lower():
            continue
        wiki_dir = base / wiki_name / "wiki"
        if not wiki_dir.is_dir():
            continue
        for md_path in wiki_dir.rglob("*.md"):
            try:
                mtime = _file_mtime(md_path)
            except OSError:
                continue
            if mtime >= since:
                wf = _load_wiki_file(wiki_name, wiki_dir, md_path)
                if wf is not None:
                    recent.append(wf)

    return recent


def collect_all_wiki_files(
    mapping: dict[str, str],
    base_wiki: str,
) -> list[WikiFile]:
    """Load ALL project wiki files (for sister-project matching). Cached in memory."""
    all_files: list[WikiFile] = []
    base = Path(base_wiki)

    for _code, wiki_name in sorted(mapping.items()):
        wiki_dir = base / wiki_name / "wiki"
        if not wiki_dir.is_dir():
            continue
        for md_path in wiki_dir.rglob("*.md"):
            wf = _load_wiki_file(wiki_name, wiki_dir, md_path)
            if wf is not None:
                all_files.append(wf)

    return all_files


def collect_shared_files() -> list[SharedFile]:
    """Load all .md files under {{WIKI_PATH}}/_shared/ recursively."""
    shared: list[SharedFile] = []
    if not SHARED_DIR.is_dir():
        return shared
    for md_path in SHARED_DIR.rglob("*.md"):
        sf = _load_shared_file(md_path)
        if sf is not None:
            shared.append(sf)
    return shared


# ---------------------------------------------------------------------------
# Analysis passes
# ---------------------------------------------------------------------------


def find_cross_link_suggestions(
    recent: list[WikiFile],
    shared_files: list[SharedFile],
    threshold: float,
) -> list[CrossLinkSuggestion]:
    """For each recent file, find shared/ files with Jaccard >= threshold."""
    suggestions: list[CrossLinkSuggestion] = []
    for wf in recent:
        for sf in shared_files:
            score = jaccard(wf.token_set, sf.token_set)
            if score >= threshold:
                suggestions.append(CrossLinkSuggestion(wf, sf, score))
    # Sort: highest score first
    suggestions.sort(key=lambda s: s.score, reverse=True)
    return suggestions


def find_sister_matches(
    recent: list[WikiFile],
    all_files: list[WikiFile],
    threshold: float,
) -> list[SisterMatch]:
    """For each recent file, find files in OTHER projects with Jaccard >= threshold."""
    recent_keys = {f.path for f in recent}
    matches: list[SisterMatch] = []
    for wf in recent:
        for candidate in all_files:
            if candidate.project == wf.project:
                continue  # same project
            if candidate.path in recent_keys:
                continue  # avoid both being "recent" and matching each other twice
            score = jaccard(wf.token_set, candidate.token_set)
            if score >= threshold:
                matches.append(SisterMatch(source=wf, sister=candidate, score=score))
    matches.sort(key=lambda m: m.score, reverse=True)
    return matches


def find_promotion_candidates(
    recent: list[WikiFile],
    threshold: float,
) -> list[PromotionCandidate]:
    """Find keyword clusters seen in 2+ recent project files = promotion candidates."""
    # Group recent files by project; cluster files that are similar across projects
    n = len(recent)
    parent = list(range(n))

    def find_root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find_root(i), find_root(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if recent[i].project == recent[j].project:
                continue
            if jaccard(recent[i].token_set, recent[j].token_set) >= threshold:
                union(i, j)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters[find_root(i)].append(i)

    candidates: list[PromotionCandidate] = []
    for root, members in clusters.items():
        if len(members) < 2:
            continue
        member_files = [recent[i] for i in members]
        unique_projects = {f.project for f in member_files}
        if len(unique_projects) < 2:
            continue

        # Representative keyword: most common token across cluster members
        all_tokens: list[str] = []
        for f in member_files:
            all_tokens.extend(f.keywords)
        top = [w for w, _ in Counter(all_tokens).most_common(5)]
        cluster_label = " + ".join(top[:3])

        candidates.append(
            PromotionCandidate(keyword_cluster=cluster_label, appearances=member_files)
        )

    return candidates


def find_orphan_shared_files(
    shared_files: list[SharedFile],
    all_wiki_files: list[WikiFile],
) -> list[SharedFile]:
    """Find _shared/ files that no project wiki currently mentions (no backlinks)."""
    # Build a set of shared stems for fast lookup
    # Check if the shared file name (stem) appears in any wiki file's raw token set
    orphans: list[SharedFile] = []
    for sf in shared_files:
        stem_tokens = frozenset(_tokenize(sf.path.stem.replace("-", " ")))
        if not stem_tokens:
            continue
        mentioned = any(
            len(stem_tokens & wf.token_set) >= max(1, len(stem_tokens) // 2)
            for wf in all_wiki_files
        )
        if not mentioned:
            orphans.append(sf)
    return orphans


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _fmt_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def build_report(
    run_date: date,
    since: date,
    project_filter: Optional[str],
    threshold: float,
    recent: list[WikiFile],
    cross_link_suggestions: list[CrossLinkSuggestion],
    sister_matches: list[SisterMatch],
    promotion_candidates: list[PromotionCandidate],
    orphan_shared: list[SharedFile],
) -> str:
    lines: list[str] = []

    header_filter = f" | project: {project_filter}" if project_filter else ""
    lines += [
        f"# Cross-Project Promotion Suggestions -- {_fmt_date(run_date)}",
        "",
        f"> Since: {_fmt_date(since)} | Threshold: {threshold}{header_filter}",
        f"> DRY-RUN ONLY -- no files were modified.",
        f"> Recent files scanned: {len(recent)}",
        "",
    ]

    if not recent:
        lines.append("No recently-modified wiki files found in the given window.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Section 1: Per-file cross-link suggestions
    # ------------------------------------------------------------------
    lines.append("---")
    lines.append("## 1. Cross-link suggestions (recent file -> existing _shared/)")
    lines.append("")

    if not cross_link_suggestions:
        lines.append("No matching _shared/ files found for recent files.")
        lines.append("")
    else:
        # Group by source file
        by_source: dict[Path, list[CrossLinkSuggestion]] = defaultdict(list)
        for s in cross_link_suggestions:
            by_source[s.wiki_file.path].append(s)

        for idx, (src_path, suggs) in enumerate(by_source.items(), start=1):
            wf = suggs[0].wiki_file
            lines.append(
                f"### 1.{idx}. New file in [[{wf.project}]]: {wf.rel_path}"
            )
            lines.append(f"   Modified: {_fmt_date(wf.mtime)}")
            lines.append(f"   Title: {wf.title}")
            if wf.keywords:
                lines.append(f"   Keywords: {', '.join(wf.keywords[:8])}")
            lines.append("   Suggested cross-links:")
            for s in sorted(suggs, key=lambda x: x.score, reverse=True):
                lines.append(
                    f"   - [[../../_shared/{s.shared_file.rel_path}]]"
                    f"  (score: {s.score:.2f})"
                )
            lines.append("")

    # ------------------------------------------------------------------
    # Section 2: Sister-project matches
    # ------------------------------------------------------------------
    lines.append("---")
    lines.append("## 2. Sister-project related work")
    lines.append("")

    if not sister_matches:
        lines.append("No sister-project matches found.")
        lines.append("")
    else:
        # Group by source file; cap at 3 sisters per source
        by_source_s: dict[Path, list[SisterMatch]] = defaultdict(list)
        for m in sister_matches:
            by_source_s[m.source.path].append(m)

        for idx, (src_path, matches) in enumerate(by_source_s.items(), start=1):
            src = matches[0].source
            lines.append(
                f"### 2.{idx}. [[{src.project}]]: {src.rel_path}"
            )
            lines.append(f"   Title: {src.title}")
            lines.append(f"   Keywords: {', '.join(src.keywords[:8])}")
            lines.append("   Sister projects with related work:")
            for m in matches[:3]:
                lines.append(
                    f"   - [[{m.sister.project}]]: {m.sister.rel_path}"
                    f"  ({m.sister.title}) -- score: {m.score:.2f}"
                )
                lines.append(
                    f"     Consider cross-link: [[../../{m.sister.project}/wiki/{m.sister.rel_path}]]"
                )
            lines.append("")

    # ------------------------------------------------------------------
    # Section 3: Promotion candidates
    # ------------------------------------------------------------------
    lines.append("---")
    lines.append("## 3. Promotion candidates (pattern in 2+ recent project files)")
    lines.append("")

    if not promotion_candidates:
        lines.append("No promotion candidates found in this window.")
        lines.append("")
    else:
        for idx, cand in enumerate(promotion_candidates, start=1):
            projects = sorted(cand.project_set)
            lines.append(f"### 3.{idx}. Concept cluster: \"{cand.keyword_cluster}\"")
            lines.append(f"   Mentioned in {len(projects)} projects: {', '.join(projects)}")
            lines.append("   Sources:")
            for wf in cand.appearances:
                lines.append(f"   - [[{wf.project}]]/wiki/{wf.rel_path}  ({wf.title})")
            slug = cand.keyword_cluster.replace(" + ", "-").replace(" ", "-").lower()
            lines.append(
                f"   Suggested: create _shared/{slug}.md"
            )
            lines.append("")

    # ------------------------------------------------------------------
    # Section 4: Orphan _shared/ files
    # ------------------------------------------------------------------
    lines.append("---")
    lines.append("## 4. Orphan _shared/ files (no detected project backlinks)")
    lines.append("")

    if not orphan_shared:
        lines.append("All _shared/ files appear to be referenced by at least one project.")
        lines.append("")
    else:
        for sf in sorted(orphan_shared, key=lambda f: f.rel_path):
            lines.append(f"- _shared/{sf.rel_path}  ({sf.title})")
        lines.append("")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    lines.append("---")
    lines.append("## Summary")
    lines.append("")
    lines.append(
        f"| Section | Count |"
    )
    lines.append(f"|---------|-------|")
    lines.append(f"| Recent files scanned | {len(recent)} |")
    n_cross = len({s.wiki_file.path for s in cross_link_suggestions})
    lines.append(f"| Files with _shared/ link suggestions | {n_cross} |")
    n_sister = len({m.source.path for m in sister_matches})
    lines.append(f"| Files with sister-project matches | {n_sister} |")
    lines.append(f"| Promotion candidates | {len(promotion_candidates)} |")
    lines.append(f"| Orphan _shared/ files | {len(orphan_shared)} |")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Detect wiki content that could benefit other projects "
            "and suggest cross-links or _shared/ promotions. DRY-RUN ONLY."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--since-days",
        type=int,
        default=DEFAULT_SINCE_DAYS,
        metavar="N",
        help=f"Look at files modified in the last N days (default: {DEFAULT_SINCE_DAYS})",
    )
    p.add_argument(
        "--project",
        metavar="WIKI_NAME",
        default=None,
        help="Limit recent-file scan to a single wiki project (e.g. Cinemind)",
    )
    p.add_argument(
        "--output",
        metavar="FILE",
        default=None,
        help="Write markdown report to FILE instead of stdout (legacy; overrides --out)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path.home() / ".claude" / "logs" / "cross-links-pending.md",
        help=(
            "Where to write the pending review file "
            "(default: ~/.claude/logs/cross-links-pending.md)"
        ),
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        metavar="FLOAT",
        help=f"Jaccard similarity threshold 0.0-1.0 (default: {DEFAULT_THRESHOLD})",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()

    if not WIKI_MAP_PATH.exists():
        print(
            f"ERROR: wiki-map.json not found at {WIKI_MAP_PATH}",
            file=sys.stderr,
        )
        sys.exit(2)

    mapping, _base_code, base_wiki = load_wiki_map()

    today = date.today()
    since = date.fromordinal(today.toordinal() - args.since_days)

    print(
        f"Scanning wikis | since: {_fmt_date(since)} | "
        f"threshold: {args.threshold}"
        + (f" | project: {args.project}" if args.project else ""),
        file=sys.stderr,
    )

    # Collect recent files (filtered by --project if given)
    recent = collect_recent_files(mapping, base_wiki, since, args.project)
    print(f"  Recent files: {len(recent)}", file=sys.stderr)

    if not recent:
        print(
            "No recently-modified wiki files found. "
            "Try increasing --since-days.",
            file=sys.stderr,
        )
        # Still emit an empty report so piping/output always works
        report = build_report(
            today, since, args.project, args.threshold,
            [], [], [], [], [],
        )
        _write_report(report, args.output)
        _write_pending_review(args.out, today, [], [], [], [])
        sys.exit(0)

    # Load ALL wiki files for sister matching and orphan detection
    print("  Loading all wiki files for sister matching...", file=sys.stderr)
    all_wiki_files = collect_all_wiki_files(mapping, base_wiki)
    print(f"  Total wiki files: {len(all_wiki_files)}", file=sys.stderr)

    # Load all _shared/ files
    shared_files = collect_shared_files()
    print(f"  Shared files: {len(shared_files)}", file=sys.stderr)

    # Run analysis passes
    cross_links = find_cross_link_suggestions(recent, shared_files, args.threshold)
    sisters = find_sister_matches(recent, all_wiki_files, args.threshold)
    promotions = find_promotion_candidates(recent, args.threshold)
    orphans = find_orphan_shared_files(shared_files, all_wiki_files)

    report = build_report(
        today, since, args.project, args.threshold,
        recent, cross_links, sisters, promotions, orphans,
    )

    _write_report(report, args.output)
    _write_pending_review(args.out, today, cross_links, sisters, promotions, orphans)
    sys.exit(0)


def _write_pending_review(
    out_path: Path,
    run_date: date,
    cross_links: list[CrossLinkSuggestion],
    sisters: list[SisterMatch],
    promotions: list[PromotionCandidate],
    orphans: list[SharedFile],
) -> None:
    """Write a structured human-readable pending review file with checkboxes.

    Aggregates cross-link suggestions, sister-project matches, and promotion
    candidates into a single ranked list of "suggestions" for weekly review.
    """
    # Build a unified list of suggestion dicts so the output format is uniform.
    suggestions: list[dict] = []

    for s in cross_links:
        suggestions.append({
            "kind": "cross-link",
            "topic": s.shared_file.title or s.shared_file.rel_path,
            "score": s.score,
            "source_project": s.wiki_file.project,
            "source_file": s.wiki_file.rel_path,
            "target_project": "_shared",
            "target_file": s.shared_file.rel_path,
            "keywords": list(s.wiki_file.token_set & s.shared_file.token_set)[:10],
            "action": (
                f"Add link in [[{s.wiki_file.project}]]/{s.wiki_file.rel_path} "
                f"to _shared/{s.shared_file.rel_path}"
            ),
        })

    for m in sisters:
        suggestions.append({
            "kind": "sister",
            "topic": m.sister.title or m.sister.rel_path,
            "score": m.score,
            "source_project": m.source.project,
            "source_file": m.source.rel_path,
            "target_project": m.sister.project,
            "target_file": m.sister.rel_path,
            "keywords": list(m.source.token_set & m.sister.token_set)[:10],
            "action": (
                f"Cross-link [[{m.source.project}]]/{m.source.rel_path} "
                f"<-> [[{m.sister.project}]]/{m.sister.rel_path}, "
                f"or promote shared concept to _shared/"
            ),
        })

    for cand in promotions:
        projects = sorted(cand.project_set)
        suggestions.append({
            "kind": "promotion",
            "topic": cand.keyword_cluster,
            "score": float(len(projects)),  # number of projects as a "score"
            "source_project": ", ".join(projects),
            "source_file": "; ".join(
                f"{wf.project}/{wf.rel_path}" for wf in cand.appearances
            ),
            "target_project": "_shared",
            "target_file": (
                cand.keyword_cluster.replace(" + ", "-").replace(" ", "-").lower()
                + ".md"
            ),
            "keywords": cand.keyword_cluster.split(" + "),
            "action": (
                f"Promote shared concept '{cand.keyword_cluster}' to _shared/ "
                f"and back-link from each project wiki"
            ),
        })

    # Sort by score desc, but keep promotions on top regardless
    suggestions.sort(
        key=lambda s: (s["kind"] != "promotion", -s["score"])
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"# Pending Cross-Links -- {run_date.isoformat()}\n")
        f.write(
            "*Generated by cross_project_promoter.py. Review weekly.*\n\n"
        )

        if not suggestions and not orphans:
            f.write("No cross-link suggestions found.\n")
            print(
                f"Pending review written to {out_path} (empty)",
                file=sys.stderr,
            )
            return

        # Status checklist
        f.write("## Status\n")
        for i, _ in enumerate(suggestions, 1):
            f.write(f"- [ ] Suggestion {i}\n")
        if orphans:
            f.write(f"- [ ] Review {len(orphans)} orphan _shared/ file(s)\n")
        f.write("\n---\n\n")

        # Detailed sections
        for i, sug in enumerate(suggestions, 1):
            f.write(f"## Suggestion {i}: {sug['topic']}\n")
            f.write(f"**Kind:** {sug['kind']}\n")
            f.write(f"**Score:** {sug['score']:.2f}\n")
            f.write(
                f"**Source:** {sug['source_project']} -- "
                f"`{sug['source_file']}`\n"
            )
            f.write(
                f"**Target:** {sug['target_project']} -- "
                f"`{sug['target_file']}` (could benefit)\n\n"
            )

            if sug.get("keywords"):
                kws = [k for k in sug["keywords"] if k]
                if kws:
                    f.write(f"**Matched keywords:** {', '.join(kws[:10])}\n\n")

            f.write("**Suggested action:**\n")
            f.write(f"- {sug['action']}\n")
            f.write(
                f"- Or promote to `_shared/` if applicable to many projects\n"
            )
            f.write("\n---\n\n")

        if orphans:
            f.write("## Orphan _shared/ files (no detected backlinks)\n\n")
            for sf in sorted(orphans, key=lambda x: x.rel_path):
                f.write(f"- `_shared/{sf.rel_path}` ({sf.title})\n")
            f.write("\n")

    print(
        f"Pending review written to {out_path} "
        f"({len(suggestions)} suggestions, {len(orphans)} orphans)",
        file=sys.stderr,
    )


def _write_report(report: str, output_path: Optional[str]) -> None:
    if output_path:
        out = Path(output_path)
        out.write_text(report, encoding="utf-8")
        print(f"Report written to {out}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(report.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
