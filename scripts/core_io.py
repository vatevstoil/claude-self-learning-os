"""core_io.py — shared, stdlib-only IO helpers for the self-learning OS.

Reconstructed from the in-repo fallback definitions (ab_eval.py,
incident_fix_proposer.py) because the published repo omitted this module while
7 scripts import it directly (integrity_guard, habit_ledger, incident_tracker,
outcome_kpi, pii_scanner, suggestion_feedback, ab_eval). Contract matches those
fallbacks exactly so behaviour is identical whether or not this file is present.

No third-party deps, no network — safe to import anywhere.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_utc() -> datetime:
    """Timezone-aware current UTC timestamp."""
    return datetime.now(tz=timezone.utc)


def load_json_tolerant(path: Any, default: Any) -> Any:
    """Load a JSON file tolerantly; return *default* on missing/corrupt/unreadable."""
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_write_json(path: Any, data: Any) -> None:
    """Write JSON atomically via temp-file + os.replace (never leaves a partial file)."""
    p = Path(path)
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
