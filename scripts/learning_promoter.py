"""learning_promoter.py

Scan learnings files across all projects, detect lessons that appear in 2+
projects (by token-overlap similarity), and suggest promotion candidates for
_shared/patterns.md.

DRY-RUN ONLY — never modifies any file.

Usage:
    python learning_promoter.py
    python learning_promoter.py --output suggestions.md
    python learning_promoter.py --min-projects 2 --threshold 0.6
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional
import json


def slugify(text: str) -> str:
    """Make a filename-safe slug from a topic title."""
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return s[:50] or "pattern"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WIKI_MAP_PATH = Path(r"{{WIKI_PATH}}\_shared\wiki-map.json")
DEFAULT_MIN_PROJECTS = 2
DEFAULT_THRESHOLD = 0.30  # Jaccard token-overlap (label-stripped); surfaces real lexical dups, auto-apply gate filters
MIN_CONTENT_TOKENS = 4    # Reject lessons with <N meaningful tokens after label strip

# Frontmatter types that qualify a source file as a learning file
LEARNING_TYPES = {"learning", "gotcha", "pattern", "source"}

# Structural markdown labels = template scaffolding, NOT lesson content.
# A line like "**Result:** success — 80/80 tests pass" must be matched on
# "success 80/80 tests pass", and must never be titled just "Result:".
_STRUCTURAL_LABELS = {
    "result", "results", "status", "date", "fix", "fixed", "problem",
    "solution", "note", "notes", "todo", "done", "summary", "context",
    "issue", "cause", "root cause", "impact", "evidence", "verdict",
    "decision", "outcome", "before", "after", "input", "output", "goal",
    "task", "test", "tests", "example", "examples", "why", "what", "how",
    "резултат", "статус", "дата", "проблем", "решение", "бележка",
    "забележка", "цел", "задача", "тест", "тестове", "извод", "причина",
    "пример", "контекст", "статус", "готово", "поправка", "защо", "какво",
}

# Leading bold label like "**Result:**" or "**Root cause** -"
_LEADING_LABEL_RE = re.compile(r"^\s*[-*]?\s*\*\*([^*]+?)\*\*\s*[:：\-—]?\s*")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Lesson:
    """A single extracted lesson item."""
    project: str
    file_path: Path
    line_number: int
    text: str              # Full raw text of the lesson (may be multi-line)
    key_phrase: str        # First bold phrase or first ~10 words
    match_text: str = ""   # Label-stripped text used for similarity matching

    def location(self) -> str:
        return f"{self.file_path}:{self.line_number}"


@dataclass
class PromotionGroup:
    """Lessons from 2+ projects that are similar enough to promote."""
    topic: str
    lessons: list[Lesson] = field(default_factory=list)
    match_strength: float = 0.0   # avg cross-project jaccard among members

    @property
    def projects(self) -> list[str]:
        seen: list[str] = []
        for les in self.lessons:
            if les.project not in seen:
                seen.append(les.project)
        return seen

    def confidence_score(self) -> float:
        """Confidence blends project count, lesson count, and real similarity."""
        proj_factor = min(0.30, (len(self.projects) - 2) * 0.10 + 0.20)
        count_factor = min(0.15, len(self.lessons) * 0.03)
        sim_factor = min(0.50, self.match_strength * 0.55)  # real signal weight
        return round(min(0.97, 0.10 + proj_factor + count_factor + sim_factor), 2)

    def suggested_entry(self) -> str:
        """Draft a unified patterns.md entry."""
        lines = [f"### {self.topic}"]
        for les in self.lessons:
            summary = les.text.strip().splitlines()[0][:200]
            lines.append(f"- {summary}  *(from {les.project})*")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


def load_wiki_map(path: Path) -> tuple[dict[str, str], str, str]:
    raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    return (
        raw.get("mapping", {}),
        raw.get("base_code_path", r"{{CODE_PATH}}"),
        raw.get("base_wiki_path", r"{{WIKI_PATH}}"),
    )


_FM_TYPE_RE = re.compile(r"^type\s*:\s*(.+)$", re.MULTILINE | re.IGNORECASE)


def _frontmatter_type(text: str) -> Optional[str]:
    """Extract 'type' field value from YAML frontmatter, or None."""
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end == -1:
        return None
    fm = text[3:end]
    m = _FM_TYPE_RE.search(fm)
    if m:
        return m.group(1).strip().strip('"').strip("'").lower()
    return None


def find_learning_files(
    code_name: str,
    wiki_name: str,
    base_code: str,
    base_wiki: str,
) -> list[Path]:
    """Return candidate learning files for a project, in priority order."""
    found: list[Path] = []

    # Priority 1: .ai/learnings.md in code folder
    ai_learnings = Path(base_code) / code_name / ".ai" / "learnings.md"
    if ai_learnings.exists():
        found.append(ai_learnings)

    # Priority 2: wiki/sources/learnings*.md
    sources_dir = Path(base_wiki) / wiki_name / "wiki" / "sources"
    if sources_dir.is_dir():
        for f in sorted(sources_dir.glob("learnings*.md")):
            if f not in found:
                found.append(f)

        # Priority 3 (fallback): any *.md with qualifying frontmatter type
        if not any(p.parent == sources_dir and "learnings" in p.name for p in found):
            for f in sorted(sources_dir.glob("*.md")):
                if f in found:
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                    ftype = _frontmatter_type(text)
                    if ftype and ftype in LEARNING_TYPES:
                        found.append(f)
                except OSError:
                    pass

    return found


# ---------------------------------------------------------------------------
# Lesson extraction
# ---------------------------------------------------------------------------

# Patterns (checked in order):
#   1. Lines starting with  - **Key phrase**
#   2. Lines starting with  **Key phrase**
#   3. Section headers  ### Short title  (≤ 8 words)

_BULLET_BOLD_RE = re.compile(r"^[-*]\s+(\*\*[^*]+\*\*.*)", re.MULTILINE)
_BOLD_LINE_RE = re.compile(r"^(\*\*[^*]+\*\*.*)", re.MULTILINE)
_SECTION_RE = re.compile(r"^###\s+(.+)$", re.MULTILINE)
_BOLD_KEY_RE = re.compile(r"\*\*([^*]+)\*\*")
_DATE_HEADER_RE = re.compile(r"^##\s+\[(\d{4}-\d{2}-\d{2})\]", re.MULTILINE)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _normalize_label(s: str) -> str:
    """Lowercase + strip trailing punctuation for structural-label comparison."""
    return re.sub(r"[:：\-—\s]+$", "", s.strip().lower())


def _strip_leading_label(text: str) -> str:
    """Remove a leading '**Label:**' scaffolding prefix, return the content."""
    m = _LEADING_LABEL_RE.match(text)
    if m and _normalize_label(m.group(1)) in _STRUCTURAL_LABELS:
        return text[m.end():].strip()
    return text.strip()


def _key_phrase(text: str) -> str:
    """Extract a meaningful title: first NON-structural bold phrase, else first words
    of the label-stripped content."""
    for m in _BOLD_KEY_RE.finditer(text):
        phrase = m.group(1).strip()
        if _normalize_label(phrase) not in _STRUCTURAL_LABELS:
            return phrase
    stripped = _strip_leading_label(text)
    words = stripped.split()[:10]
    return " ".join(words)


def extract_lessons(project: str, file_path: Path) -> list[Lesson]:
    """Parse a markdown file and return Lesson objects."""
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    lessons: list[Lesson] = []
    lines = text.splitlines()
    line_index: dict[int, int] = {}  # char_offset -> line_number (1-based)
    offset = 0
    for i, ln in enumerate(lines, start=1):
        line_index[offset] = i
        offset += len(ln) + 1  # +1 for newline

    def char_to_line(pos: int) -> int:
        """Find the 1-based line number for a character position."""
        best = 1
        for off, ln in line_index.items():
            if off <= pos:
                best = ln
        return best

    seen_starts: set[int] = set()

    # Pattern 1 & 2: bullet or plain bold lines
    for pat in (_BULLET_BOLD_RE, _BOLD_LINE_RE):
        for m in pat.finditer(text):
            start = m.start()
            lnum = char_to_line(start)
            if lnum in seen_starts:
                continue
            seen_starts.add(lnum)
            raw = m.group(1)
            match_text = _strip_leading_label(raw)
            # Content gate: reject template-label-only / too-short lessons
            meaningful = _tokenize(match_text) - _STOPWORDS
            if len(meaningful) < MIN_CONTENT_TOKENS:
                continue
            lessons.append(
                Lesson(
                    project=project,
                    file_path=file_path,
                    line_number=lnum,
                    text=raw,
                    key_phrase=_key_phrase(raw),
                    match_text=match_text,
                )
            )

    # Pattern 3: ### section headers (short = gotcha title)
    for m in _SECTION_RE.finditer(text):
        title = m.group(1).strip()
        if len(title.split()) > 12:  # avoid long prose headings
            continue
        if _normalize_label(title) in _STRUCTURAL_LABELS:  # skip "### Result:" etc.
            continue
        if _ISO_DATE_RE.match(title):  # skip date headers "### 2026-05-01"
            continue
        meaningful = _tokenize(title) - _STOPWORDS
        if len(meaningful) < MIN_CONTENT_TOKENS:
            continue
        lnum = char_to_line(m.start())
        if lnum not in seen_starts:
            seen_starts.add(lnum)
            lessons.append(
                Lesson(
                    project=project,
                    file_path=file_path,
                    line_number=lnum,
                    text=title,
                    key_phrase=title,
                    match_text=title,
                )
            )

    return lessons


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> set[str]:
    """Lowercase, strip punctuation, return set of tokens (>= 3 chars)."""
    tokens = re.findall(r"[a-zA-ZЀ-ӿ]{3,}", text.lower())
    return set(tokens)


# Common words to ignore in similarity matching
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "not",
    "but", "all", "use", "при", "за", "не", "да", "се", "на", "от",
    "ako", "ако", "как", "без", "след", "преди",
}


def jaccard(a: str, b: str) -> float:
    ta = _tokenize(a) - _STOPWORDS
    tb = _tokenize(b) - _STOPWORDS
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def group_lessons(
    lessons: list[Lesson],
    threshold: float,
    min_projects: int,
) -> list[PromotionGroup]:
    """Cluster lessons by similarity; return groups spanning >= min_projects."""
    # Union-find approach: group index per lesson
    n = len(lessons)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if lessons[i].project == lessons[j].project:
                continue  # only cross-project similarity matters
            # Match on label-stripped content, not template scaffolding
            a = lessons[i].match_text or lessons[i].text
            b = lessons[j].match_text or lessons[j].text
            score = jaccard(a, b)
            if score >= threshold:
                union(i, j)

    # Collect groups
    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(i)

    groups: list[PromotionGroup] = []
    for root, members in clusters.items():
        if len(members) < 2:
            continue
        member_lessons = [lessons[i] for i in members]
        unique_projects = {les.project for les in member_lessons}
        if len(unique_projects) < min_projects:
            continue

        # Name the group from the most common key phrase
        phrase_counts: dict[str, int] = defaultdict(int)
        for les in member_lessons:
            phrase_counts[les.key_phrase] += 1
        topic = max(phrase_counts, key=lambda k: phrase_counts[k])

        # Real match strength = avg cross-project pairwise jaccard
        pair_scores: list[float] = []
        for a in range(len(member_lessons)):
            for b in range(a + 1, len(member_lessons)):
                if member_lessons[a].project == member_lessons[b].project:
                    continue
                ta = member_lessons[a].match_text or member_lessons[a].text
                tb = member_lessons[b].match_text or member_lessons[b].text
                pair_scores.append(jaccard(ta, tb))
        strength = sum(pair_scores) / len(pair_scores) if pair_scores else 0.0

        grp = PromotionGroup(topic=topic, lessons=member_lessons, match_strength=strength)
        groups.append(grp)

    return groups


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def build_report(
    groups: list[PromotionGroup],
    min_projects: int,
    threshold: float,
) -> str:
    lines: list[str] = [
        "# Learning Promoter — Suggested Promotions to _shared/patterns.md",
        "",
        f"> Similarity threshold: {threshold} | Min projects: {min_projects}",
        f"> DO NOT copy blindly — review each suggestion before adding.",
        "",
    ]

    if not groups:
        lines.append("No cross-project patterns found with the given settings.")
        return "\n".join(lines)

    for i, grp in enumerate(groups, start=1):
        lines.append(f"---")
        lines.append(f"## {i}. Suggested promotion: {grp.topic}")
        lines.append(f"**Projects:** {', '.join(grp.projects)}")
        lines.append("")
        lines.append("### Source lessons (verbatim)")
        for les in grp.lessons:
            lines.append(f"**{les.project}** (`{les.location()}`):")
            lines.append(f"> {les.text.strip()[:300]}")
            lines.append("")
        lines.append("### Suggested entry for `_shared/patterns.md`")
        lines.append("```markdown")
        lines.append(grp.suggested_entry())
        lines.append("```")
        lines.append("")

    lines.append("---")
    lines.append(f"*{len(groups)} promotion candidate(s) found.*")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Find cross-project learning patterns and suggest _shared/patterns.md entries.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--output",
        metavar="FILE",
        help="Write report to FILE instead of stdout",
    )
    p.add_argument(
        "--min-projects",
        type=int,
        default=DEFAULT_MIN_PROJECTS,
        metavar="N",
        help=f"Minimum number of projects a pattern must appear in (default: {DEFAULT_MIN_PROJECTS})",
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        metavar="FLOAT",
        help=f"Jaccard similarity threshold 0.0-1.0 (default: {DEFAULT_THRESHOLD})",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path.home() / ".claude" / "logs" / "promotions-pending.md",
        help="Where to write the pending review file (default: ~/.claude/logs/promotions-pending.md)",
    )
    return p


def write_pending_review(out_path: Path, promotion_groups: list[PromotionGroup]) -> None:
    """Write the structured pending-review file consumed by promotion_apply.py."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"# Pending Promotions — {date.today().isoformat()}\n")
        f.write("*Generated by learning_promoter.py. Review and apply selectively.*\n\n")
        if not promotion_groups:
            f.write("No promotion candidates found this week.\n")
            return

        f.write("## Status\n")
        for i, _ in enumerate(promotion_groups, 1):
            f.write(f"- [ ] Candidate {i}\n")
        f.write("\n---\n\n")

        for i, group in enumerate(promotion_groups, 1):
            confidence = getattr(group, "confidence", None)
            if confidence is None:
                confidence = group.confidence_score() if hasattr(group, "confidence_score") else 0.7
            f.write(f"## Candidate {i}: {group.topic}\n")
            f.write(f"**Confidence:** {confidence:.2f}\n")
            f.write(f"**Found in:** {', '.join(group.projects)} ({len(group.projects)} projects)\n")
            slug = slugify(group.topic)
            f.write(f"**Suggested location:** `J:\\Obsidian\\_shared\\{slug}.md`\n\n")
            f.write("**Source examples:**\n")
            for les in group.lessons[:3]:
                preview = les.text.strip().splitlines()[0][:120] if les.text else ""
                f.write(f"- {les.location()} — \"{preview}\"\n")
            f.write("\n**Suggested entry:**\n```markdown\n")
            f.write(group.suggested_entry())
            f.write("\n```\n\n")
            f.write(f"**Apply:** `python ~/.claude/scripts/promotion_apply.py --candidate {i}`\n")
            f.write(f"**Skip:** `python ~/.claude/scripts/promotion_apply.py --skip {i}`\n\n---\n\n")


def main() -> None:
    args = build_parser().parse_args()

    if not WIKI_MAP_PATH.exists():
        print(f"ERROR: wiki-map.json not found at {WIKI_MAP_PATH}", file=sys.stderr)
        sys.exit(2)

    mapping, base_code, base_wiki = load_wiki_map(WIKI_MAP_PATH)

    all_lessons: list[Lesson] = []
    project_file_counts: dict[str, int] = {}

    for code_name, wiki_name in sorted(mapping.items()):
        files = find_learning_files(code_name, wiki_name, base_code, base_wiki)
        project_file_counts[wiki_name] = len(files)
        for fpath in files:
            lessons = extract_lessons(wiki_name, fpath)
            all_lessons.extend(lessons)

    total_projects_with_data = sum(1 for v in project_file_counts.values() if v > 0)
    print(
        f"Scanned {len(mapping)} projects | "
        f"{total_projects_with_data} with learning files | "
        f"{len(all_lessons)} lessons extracted",
        file=sys.stderr,
    )

    if not all_lessons:
        print("No lessons found. Nothing to promote.", file=sys.stderr)
        sys.exit(0)

    groups = group_lessons(all_lessons, args.threshold, args.min_projects)
    report = build_report(groups, args.min_projects, args.threshold)

    # Always write the structured pending-review file (parseable by promotion_apply.py)
    write_pending_review(args.out, groups)
    print(f"Pending review written to {args.out} ({len(groups)} candidate(s))", file=sys.stderr)

    if args.output:
        out_path = Path(args.output)
        out_path.write_text(report, encoding="utf-8")
        print(f"Report written to {out_path}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(report.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
