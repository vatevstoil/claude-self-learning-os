"""hook_telemetry.py — per-hook runtime telemetry (write + read).

RECONSTRUCTED: the published repo omitted this module, but selfreg_monitor and
agentic_os_dashboard both consume it (guarded) to grade hook RUNTIME health from
``~/.claude/logs/hook-telemetry.jsonl``. Every runtime import is wrapped in
try/except, so absence degrades to "no telemetry" (score 100); this module makes
the feature actually work.

Ledger line schema (one JSON object per hook invocation):
    {"ts": "<iso8601>", "hook": "<name>", "ms": <float>, "ok": <bool>}

``record()`` appends a line (call from a hook wrapper). ``summarize()`` reads the
last *window_days* and returns per-hook aggregates consumed by selfreg_monitor:
    {name: {"count": int, "error_rate": float, "p50_ms": float, "p95_ms": float}}
Corrupt lines are skipped, never raised.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict

LEDGER = Path.home() / ".claude" / "logs" / "hook-telemetry.jsonl"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def record(hook: str, ms: float, ok: bool, *, ledger: Path = LEDGER) -> None:
    """Append one telemetry row. Best-effort: never raises into the hook."""
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": _now().isoformat(), "hook": hook, "ms": float(ms), "ok": bool(ok)}
        with ledger.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def summarize(window_days: int = 7, *, ledger: Path = LEDGER) -> Dict[str, Dict[str, Any]]:
    """Per-hook aggregates over the trailing *window_days*. Tolerant of missing
    file and corrupt lines (both degrade to {})."""
    if not ledger.exists():
        return {}
    cutoff = _now() - timedelta(days=window_days)
    buckets: Dict[str, Dict[str, list]] = {}
    try:
        text = ledger.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            ts = datetime.fromisoformat(row["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
            name = str(row["hook"])
            b = buckets.setdefault(name, {"ms": [], "errors": 0})
            b["ms"].append(float(row.get("ms", 0.0)))
            if not bool(row.get("ok", True)):
                b["errors"] += 1
        except Exception:
            continue  # skip corrupt line
    out: Dict[str, Dict[str, Any]] = {}
    for name, b in buckets.items():
        ms_sorted = sorted(b["ms"])
        count = len(ms_sorted)
        out[name] = {
            "count": count,
            "error_rate": (b["errors"] / count) if count else 0.0,
            "p50_ms": _percentile(ms_sorted, 0.50),
            "p95_ms": _percentile(ms_sorted, 0.95),
        }
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(summarize(window_days=int(sys.argv[1]) if len(sys.argv) > 1 else 7),
                     ensure_ascii=False, indent=2))
