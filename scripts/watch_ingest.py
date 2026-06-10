#!/usr/bin/env python3
"""watch_ingest.py — Capture a YouTube video's TRANSCRIPT into a research KB's
raw/transcripts/ folder, ready for the `kb-ingest` skill to summarize.

This is the deterministic *capture* half of the Watch→kb-ingest pipeline. It is
intentionally transcript-only (no video/frame download): kb-ingest produces a
text summary, so frames would be wasted bandwidth. For visual Q&A use the full
`/watch` skill instead.

Reuses the `watch` skill's VTT parser (transcribe.py) so dedup/timestamp logic
stays in one place.

Usage:
    python watch_ingest.py <url> [<url> ...] [--kb "<base path>"] [--author NAME]

    --kb      Base dir of the research KB (default: Claude Code Resurch).
              Transcript is written to <kb>/raw/transcripts/<slug>.md
    --author  Override channel/author (else taken from yt-dlp info.json).

Exit codes: 0 = all captured · 1 = at least one failed (e.g. no captions).
Prints one written path per line on success (consumed by /watch-ingest command).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Reuse the watch skill's battle-tested VTT parser (dedup + timestamps).
_WATCH_SCRIPTS = Path.home() / ".claude" / "skills" / "watch" / "scripts"
sys.path.insert(0, str(_WATCH_SCRIPTS))
try:
    from transcribe import parse_vtt, format_transcript  # type: ignore
except Exception as exc:  # pragma: no cover - environment guard
    print(f"watch_ingest: cannot import watch transcribe.py ({exc})", file=sys.stderr)
    sys.exit(1)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

DEFAULT_KB = Path(r"{{RESEARCH_PATH}}\Claude Code Resurch")
SUB_LANGS = "en,en-US,en-GB,en-orig"


def slugify(title: str, channel: str = "") -> str:
    """kebab-case slug matching the existing raw/transcripts naming style
    (lowercase, hyphenated, optionally channel-prefixed, length-capped)."""
    def norm(s: str) -> str:
        s = (s or "").lower()
        s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
        return re.sub(r"-{2,}", "-", s)

    ch = norm(channel)
    ti = norm(title) or "video"
    base = f"{ch}-{ti}" if ch and not ti.startswith(ch) else ti
    return base[:90].rstrip("-") + ".md"


def fetch_subs(url: str, out_dir: Path) -> dict:
    """yt-dlp subtitles-only fetch (NO video download). Returns
    {vtt: Path|None, info: dict}."""
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is not installed.")
    tmpl = str(out_dir / "sub.%(ext)s")
    cmd = [
        "yt-dlp", "--skip-download",
        "--write-subs", "--write-auto-subs",
        "--sub-langs", SUB_LANGS,
        "--sub-format", "vtt", "--convert-subs", "vtt",
        "--write-info-json", "--no-playlist", "--ignore-errors",
        "-o", tmpl, "--", url,
    ]
    subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr)

    vtts = sorted(out_dir.glob("sub*.vtt"))
    preferred = [c for c in vtts if ".en" in c.name]
    vtt = (preferred or vtts or [None])[0]

    info: dict = {}
    info_path = out_dir / "sub.info.json"
    if info_path.exists():
        try:
            raw = json.loads(info_path.read_text(encoding="utf-8"))
            info = {
                "title": raw.get("title") or "Untitled",
                "channel": raw.get("uploader") or raw.get("channel") or "",
                "url": raw.get("webpage_url") or url,
                "description": (raw.get("description") or "").strip(),
            }
        except Exception as exc:
            print(f"watch_ingest: info.json parse failed: {exc}", file=sys.stderr)
    if not info:
        info = {"title": "Untitled", "channel": "", "url": url, "description": ""}
    return {"vtt": vtt, "info": info}


def build_raw(info: dict, transcript: str, author_override: str = "") -> str:
    """Render the raw/transcripts/*.md format kb-ingest expects.
    CRITICAL: line 1 = '# RAW: <title>', line 2 = 'Source: <url> -- <author>'
    (kb-ingest reads source_url from line 2)."""
    author = author_override or info.get("channel") or "Unknown"
    desc = info.get("description", "")
    if len(desc) > 1200:
        desc = desc[:1200].rsplit(" ", 1)[0] + " …"
    return (
        f"# RAW: {info['title']}\n"
        f"Source: {info['url']} -- {author}\n"
        f"Type: YouTube Transcript\n"
        f"Processed: wiki/summaries/\n\n"
        f"---\n\n"
        f"## Описание\n\n{desc or '(no description)'}\n\n"
        f"## Transcript\n\n{transcript}\n"
    )


def capture_one(url: str, kb: Path, author: str) -> Path | None:
    raw_dir = kb / "raw" / "transcripts"
    raw_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        res = fetch_subs(url, Path(td))
        if not res["vtt"]:
            print(f"watch_ingest: NO captions for {url} "
                  f"(needs Whisper — use full /watch skill)", file=sys.stderr)
            return None
        segments = parse_vtt(str(res["vtt"]))
        if not segments:
            print(f"watch_ingest: empty transcript for {url}", file=sys.stderr)
            return None
        transcript = format_transcript(segments)
        info = res["info"]
        dest = raw_dir / slugify(info["title"], info.get("channel", ""))
        if dest.exists():
            print(f"watch_ingest: already exists, skipping: {dest.name}", file=sys.stderr)
            return dest
        dest.write_text(build_raw(info, transcript, author), encoding="utf-8")
        return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+", help="YouTube URL(s)")
    ap.add_argument("--kb", default=str(DEFAULT_KB), help="KB base dir")
    ap.add_argument("--author", default="", help="Override author/channel")
    args = ap.parse_args()

    kb = Path(args.kb)
    failures = 0
    for url in args.urls:
        try:
            dest = capture_one(url, kb, args.author)
            if dest:
                print(dest)  # stdout: path for the /watch-ingest command to pass on
            else:
                failures += 1
        except Exception as exc:
            print(f"watch_ingest: ERROR on {url}: {exc}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
