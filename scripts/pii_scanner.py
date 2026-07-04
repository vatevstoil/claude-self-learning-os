#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pii_scanner.py — block personal/identifying data from entering the RAG store.

The local SQLite vector store is PLAINTEXT on disk and feeds cross-session
recall, so ingesting a document that carries someone's national ID, bank
account, card number or a sealed legal-case marker is a real privacy leak — it
was exactly such content (a legal case file) that leaked once before.

`local_rag.contains_secret` already covers API keys / tokens / passwords. This
module is the COMPLEMENT: structured personal data, validated with real
checksums (Bulgarian EGN weight checksum, IBAN mod-97, card Luhn) so a random
10-digit invoice number is NOT mistaken for an EGN. Two confidence tiers:

  HIGH  — EGN, credit card, IBAN, private/legal namespace markers.
          `should_block_ingest()` returns True on any of these: do not store.
  MEDIUM— email, phone. Flagged by `scan()` for auditing, but not blocked by
          default (they are common and frequently legitimate in notes).

Pure, stdlib-only, dependency-free. Import and call; nothing here does I/O.

CLI:
    python pii_scanner.py --text "..."        # scan a literal string
    echo "..." | python pii_scanner.py        # scan stdin
    python pii_scanner.py --file notes.md      # scan a file
Exit 0 = clean, 3 = HIGH-confidence finding(s), 1 = cannot read input.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
except Exception:
    pass

# ---------------------------------------------------------------------------
# Private / legal namespace markers — sealed personal knowledge that must NEVER
# be ingested into the searchable store. Mirrors visibility_guard / the public
# release denylist. Bare author identity (Stoil/Vatev/Zoya alone) is NOT here —
# only the multi-word / namespace forms are the actual leak vector.
# ---------------------------------------------------------------------------
_PRIVATE_MARKERS_FALLBACK = (
    "{{PRIVATE_NS}}", "{{PRIVATE_NS}}", "{{PRIVATE_NS}}", "{{PRIVATE_NS}}",
    "{{PRIVATE_NS}}", "{{PRIVATE_NS}}", "{{PRIVATE_NS}}", "{{PRIVATE_NS}}", "{{PRIVATE_NS}}",
)


def _load_private_markers() -> tuple[str, ...]:
    """Pull private markers from visibility_guard (single source of truth) and
    merge with the hardcoded fallback, so this stays in sync as namespaces are
    added there. Bare author-identity words are excluded (too broad). Falls back
    to the hardcoded tuple if visibility_guard cannot be imported."""
    markers = set(_PRIVATE_MARKERS_FALLBACK)
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.expanduser("~"), ".claude", "scripts"))
        from visibility_guard import SAFETY_DEFAULT
        for n in SAFETY_DEFAULT:
            if n not in {"Stoil", "Zoya", "Lichno"}:  # bare words → too broad
                markers.add(n)
    except Exception:
        pass
    return tuple(markers)


_PRIVATE_MARKERS = _load_private_markers()

CONF_HIGH = "high"
CONF_MEDIUM = "medium"


@dataclass(frozen=True)
class Finding:
    """One PII hit. `excerpt` is already truncated; full secrets are never kept."""
    kind: str
    confidence: str
    excerpt: str


# ---------------------------------------------------------------------------
# Candidate regexes — these only FIND candidates; checksum validators below
# decide whether a candidate is real (keeps the false-positive rate near zero).
# ---------------------------------------------------------------------------
_EGN_CAND = re.compile(r"(?<!\d)(\d{10})(?!\d)")
_CARD_CAND = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_IBAN_CAND = re.compile(r"\b([A-Z]{2}\d{2}[A-Z0-9]{11,30})\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Bulgarian phone: +359 / 00359 / national 0-prefixed mobile & land, 8-9 digits.
_PHONE = re.compile(
    r"(?<!\d)(?:\+359|00359|0)\s?(?:\d[ \-]?){7,9}\d(?!\d)"
)

_EGN_WEIGHTS = (2, 4, 8, 5, 10, 9, 7, 3, 6)
# Month field encodes the century: 01-12 → 1900s, 21-32 → 1800s, 41-52 → 2000s.
_EGN_MONTH_RANGES = ((1, 12), (21, 32), (41, 52))


# ---------------------------------------------------------------------------
# Checksum validators
# ---------------------------------------------------------------------------

def is_valid_egn(egn: str) -> bool:
    """True if `egn` is a structurally valid Bulgarian EGN (date + checksum).

    Validating the weighted checksum AND the embedded YYMMDD date makes a random
    10-digit string (invoice no., order id) almost never validate as an EGN.
    """
    if not (len(egn) == 10 and egn.isdigit()):
        return False
    digits = [int(c) for c in egn]
    month = int(egn[2:4])
    day = int(egn[4:6])
    if not any(lo <= month <= hi for lo, hi in _EGN_MONTH_RANGES):
        return False
    if not (1 <= day <= 31):
        return False
    checksum = sum(d * w for d, w in zip(digits[:9], _EGN_WEIGHTS)) % 11
    if checksum == 10:
        checksum = 0
    return checksum == digits[9]


def is_valid_luhn(number: str) -> bool:
    """True if the digit string passes the Luhn checksum (credit cards)."""
    digits = [int(c) for c in number if c.isdigit()]
    if not (13 <= len(digits) <= 19):
        return False
    total = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def is_valid_iban(iban: str) -> bool:
    """True if `iban` passes the ISO 13616 mod-97 check."""
    s = iban.replace(" ", "").upper()
    if not (15 <= len(s) <= 34) or not s[:2].isalpha() or not s[2:4].isdigit():
        return False
    rearranged = s[4:] + s[:4]
    converted = ""
    for ch in rearranged:
        if ch.isdigit():
            converted += ch
        elif ch.isalpha():
            converted += str(ord(ch) - 55)  # A=10 .. Z=35
        else:
            return False
    try:
        return int(converted) % 97 == 1
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _mask(s: str) -> str:
    """Mask a matched value so the Finding excerpt never carries the raw datum."""
    s = s.strip()
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def scan(text: str) -> list[Finding]:
    """Return every PII finding in `text` (validated; checksum-gated where applicable)."""
    if not text:
        return []
    findings: list[Finding] = []

    for marker in _PRIVATE_MARKERS:
        if marker in text:
            findings.append(Finding("private-marker", CONF_HIGH, marker))

    for m in _EGN_CAND.finditer(text):
        if is_valid_egn(m.group(1)):
            findings.append(Finding("egn", CONF_HIGH, _mask(m.group(1))))

    for m in _CARD_CAND.finditer(text):
        raw = m.group(0)
        if is_valid_luhn(raw):
            findings.append(Finding("credit-card", CONF_HIGH, _mask(raw)))

    for m in _IBAN_CAND.finditer(text):
        if is_valid_iban(m.group(1)):
            findings.append(Finding("iban", CONF_HIGH, _mask(m.group(1))))

    for m in _EMAIL.finditer(text):
        findings.append(Finding("email", CONF_MEDIUM, _mask(m.group(0))))

    for m in _PHONE.finditer(text):
        digits = sum(c.isdigit() for c in m.group(0))
        if digits >= 9:  # avoid flagging short numeric runs
            findings.append(Finding("phone", CONF_MEDIUM, _mask(m.group(0))))

    return findings


def should_block_ingest(text: str) -> bool:
    """True iff `text` carries any HIGH-confidence PII that must not be stored."""
    return any(f.confidence == CONF_HIGH for f in scan(text))


def redact(text: str) -> str:
    """Replace HIGH-confidence PII with ``[REDACTED:<kind>]`` markers.

    Lets a caller ingest otherwise-useful content with the sensitive spans
    stripped. MEDIUM findings (email/phone) are left intact by design.
    """
    if not text:
        return text
    out = text
    for marker in _PRIVATE_MARKERS:
        out = out.replace(marker, "[REDACTED:private-marker]")

    def _sub_validated(pattern: re.Pattern, validator, label: str, src: str) -> str:
        result = []
        last = 0
        for m in pattern.finditer(src):
            token = m.group(1) if m.groups() else m.group(0)
            if validator(token):
                result.append(src[last:m.start()])
                result.append(f"[REDACTED:{label}]")
                last = m.end()
        result.append(src[last:])
        return "".join(result)

    out = _sub_validated(_EGN_CAND, is_valid_egn, "egn", out)
    out = _sub_validated(_CARD_CAND, is_valid_luhn, "credit-card", out)
    out = _sub_validated(_IBAN_CAND, is_valid_iban, "iban", out)
    return out


# ---------------------------------------------------------------------------
# Retro DB scan (report-only, never mutates the DB)
# ---------------------------------------------------------------------------

_DEFAULT_DB = str(Path.home() / ".claude" / "local_rag.db")
_DEFAULT_REPORT = str(Path.home() / ".claude" / "logs" / "pii-retro-scan.json")


def scan_db(
    db_path: str | None = None,
    report_path: str | None = None,
) -> dict:
    """Scan every row in local_rag.db for PII and write a report JSON.

    Opens the database read-only (uri mode) so no WAL checkpoint or lock is
    acquired.  Iterates rows via a server-side cursor to avoid loading all
    48 k rows into memory at once.  HIGH findings are collected with masked
    values; MEDIUM findings are counted only (no raw values in output).

    Args:
        db_path: Path to the SQLite database.  Defaults to ~/.claude/local_rag.db.
            Pass an explicit path in tests for full isolation.
        report_path: Destination for the JSON report.  Defaults to
            ~/.claude/logs/pii-retro-scan.json.  Pass an explicit path in tests.

    Returns:
        The report dict (same structure written to disk):
        {
          "generated": "<ISO-8601 UTC>",
          "db": "<resolved db path>",
          "scanned": <int>,
          "high": [{"ns": …, "id": …, "kind": …, "masked_snippet": …}, …],
          "medium_counts": {"email": N, "phone": N, …},
          "high_count": <int>,
          "medium_count": <int>,
        }
    """
    import sqlite3
    from datetime import datetime, timezone
    from pathlib import Path as _Path

    db = str(db_path) if db_path else _DEFAULT_DB
    report = str(report_path) if report_path else _DEFAULT_REPORT

    # Open read-only via URI — never acquires a write lock, WAL checkpoint, or
    # busy lock.  Falls back gracefully if the file is absent (empty report).
    db_uri = f"file:{db}?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True, timeout=10)
    except sqlite3.OperationalError as exc:
        # DB absent or inaccessible — produce a valid empty report.
        report_data: dict = {
            "generated": datetime.now(tz=timezone.utc).isoformat(),
            "db": db,
            "scanned": 0,
            "high": [],
            "medium_counts": {},
            "high_count": 0,
            "medium_count": 0,
            "error": str(exc),
        }
        _write_report(report, report_data)
        print(f"pii-retro-scan: DB not accessible ({exc}) — empty report written.")
        return report_data

    high_findings: list[dict] = []
    medium_counts: dict[str, int] = {}
    scanned = 0

    try:
        cursor = conn.cursor()
        # Fetch only ns, id, text — vec/meta blobs not needed, saves I/O.
        # NOTE: sqlite3 has no server-side cursors; fetchmany() bounds the
        # Python-object working set per batch, not the page cache.
        cursor.execute("SELECT ns, id, text FROM vectors")
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                ns, vid, text = row[0], row[1], (row[2] or "")
                scanned += 1
                for finding in scan(text):
                    if finding.confidence == CONF_HIGH:
                        high_findings.append({
                            "ns": ns,
                            "id": vid,
                            "kind": finding.kind,
                            # excerpt is already masked by scan() via _mask()
                            "masked_snippet": finding.excerpt,
                        })
                    else:
                        medium_counts[finding.kind] = medium_counts.get(finding.kind, 0) + 1
    finally:
        conn.close()

    report_data = {
        "generated": datetime.now(tz=timezone.utc).isoformat(),
        "db": db,
        "scanned": scanned,
        "high": high_findings,
        "medium_counts": medium_counts,
        "high_count": len(high_findings),
        "medium_count": sum(medium_counts.values()),
    }
    _write_report(report, report_data)
    return report_data


def _write_report(report_path: str, data: dict) -> None:
    """Write the retro-scan report atomically via core_io if available."""
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(_Path(__file__).resolve().parent))
        from core_io import atomic_write_json
        atomic_write_json(report_path, data)
    except Exception:
        # Fallback: stdlib-only atomic write (core_io unavailable in stripped envs).
        import json
        import os
        import tempfile
        from pathlib import Path as _Path
        p = _Path(report_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, str(p))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text", help="Literal string to scan.")
    ap.add_argument("--file", help="Path to a file to scan.")
    ap.add_argument(
        "--scan-db",
        nargs="?",
        const=_DEFAULT_DB,
        metavar="DB_PATH",
        help=(
            "Retro-scan every row in the local_rag SQLite DB for PII.  "
            f"Default DB: {_DEFAULT_DB}.  "
            "Writes a JSON report to logs/pii-retro-scan.json.  "
            "REPORT-ONLY: never modifies the DB.  "
            "Exit code: 0 always (even when HIGH findings are present — "
            "this is an audit tool, not a gate)."
        ),
    )
    ap.add_argument(
        "--scan-db-report",
        metavar="REPORT_PATH",
        default=None,
        help=f"Override report output path (used with --scan-db). Default: {_DEFAULT_REPORT}",
    )
    args = ap.parse_args()

    # --scan-db mode: retro audit, report-only, always exit 0.
    if args.scan_db is not None:
        result = scan_db(db_path=args.scan_db, report_path=args.scan_db_report)
        print(
            f"pii-retro-scan: scanned={result['scanned']} "
            f"HIGH={result['high_count']} MEDIUM={result['medium_count']} "
            f"report={args.scan_db_report or _DEFAULT_REPORT}"
        )
        sys.exit(0)

    if args.text is not None:
        text = args.text
    elif args.file:
        try:
            with open(args.file, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            print(f"pii_scanner: cannot read {args.file}: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        text = sys.stdin.read()

    findings = scan(text)
    if not findings:
        print("pii_scanner: CLEAN — no PII detected.")
        sys.exit(0)

    high = [f for f in findings if f.confidence == CONF_HIGH]
    for f in findings:
        print(f"  [{f.confidence}] {f.kind}: {f.excerpt}", file=sys.stderr)
    if high:
        print(f"\n{len(high)} HIGH-confidence finding(s) — block ingest.", file=sys.stderr)
        sys.exit(3)
    print(f"\n{len(findings)} medium finding(s) — review.", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
