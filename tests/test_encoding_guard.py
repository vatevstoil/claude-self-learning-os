"""Tests for encoding_guard.py -- mojibake detection and repair.

The real-world bug: Cyrillic text written as UTF-8 then read back as cp1251,
producing a sequence of Cyrillic-Extended + punctuation + Latin-Supplement
characters. The repair path is the exact inverse: encode back to cp1251, decode
as UTF-8.

Fixtures use NEUTRAL placeholder words generated programmatically (no personal
or private data) — the public release ships this test file verbatim, so the
sample text must never carry a real name. _to_mojibake reproduces the exact
corruption; words were chosen so the round-trip is lossless (verified).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def _to_mojibake(s: str) -> str:
    """Reproduce the corruption: UTF-8 bytes of *s* misread as cp1251."""
    return s.encode("utf-8").decode("cp1251", errors="replace")


# ---------------------------------------------------------------------------
# Canonical mojibake strings -- built from NEUTRAL sample words
# ---------------------------------------------------------------------------
SAMPLE_1 = "тест_образец"        # "test_sample" — underscore, all-lowercase
SAMPLE_2 = "примерен текст"      # "sample text" — embedded space
MOJIBAKE_1 = _to_mojibake(SAMPLE_1)
MOJIBAKE_2 = _to_mojibake(SAMPLE_2)
# Expected clean results
FIXED_1 = SAMPLE_1
FIXED_1_CODES = [ord(c) for c in SAMPLE_1]


# ---------------------------------------------------------------------------
# fix_mojibake
# ---------------------------------------------------------------------------

def test_fix_mojibake_known_example():
    """The canonical example from the task spec: MOJIBAKE_1 -> {{PRIVATE_NS}}."""
    from encoding_guard import fix_mojibake
    result = fix_mojibake(MOJIBAKE_1)
    # Compare by codepoints (avoids any terminal encoding issue)
    assert [ord(c) for c in result] == FIXED_1_CODES, (
        f"Expected {{PRIVATE_NS}} codepoints, got: {[hex(ord(c)) for c in result]}"
    )


def test_fix_mojibake_second_known_key():
    """Second mojibake key from cross-recall-metrics.json must repair to Cyrillic."""
    from encoding_guard import fix_mojibake
    result = fix_mojibake(MOJIBAKE_2)
    # Must contain real core Cyrillic (U+0410-U+044F)
    core_cyrillic = [c for c in result if 0x0410 <= ord(c) <= 0x044F]
    assert len(core_cyrillic) >= 3, (
        f"Expected core Cyrillic chars in result, got codepoints: "
        f"{[hex(ord(c)) for c in result]}"
    )


def test_fix_mojibake_clean_string_unchanged():
    """A clean ASCII string must be returned unmodified."""
    from encoding_guard import fix_mojibake
    assert fix_mojibake("hello world") == "hello world"


def test_fix_mojibake_real_bulgarian_unchanged():
    """Normal Bulgarian text must survive fix_mojibake without corruption."""
    from encoding_guard import fix_mojibake
    # Разходка (codepoints in core BG range)
    bg_codes = [0x420, 0x430, 0x437, 0x445, 0x43e, 0x434, 0x43a, 0x430]
    bg = "".join(chr(c) for c in bg_codes)
    result = fix_mojibake(bg)
    # Core BG chars must still be present
    assert any(0x0410 <= ord(c) <= 0x044F for c in result)


def test_fix_mojibake_idempotent():
    """Calling fix_mojibake twice on already-fixed text is a no-op."""
    from encoding_guard import fix_mojibake
    fixed_once = fix_mojibake(MOJIBAKE_1)
    fixed_twice = fix_mojibake(fixed_once)
    assert fixed_once == fixed_twice


def test_fix_mojibake_empty_string():
    from encoding_guard import fix_mojibake
    assert fix_mojibake("") == ""


def test_fix_mojibake_pure_ascii():
    from encoding_guard import fix_mojibake
    s = "CasinoScore AI"
    assert fix_mojibake(s) == s


# ---------------------------------------------------------------------------
# looks_mojibake
# ---------------------------------------------------------------------------

def test_looks_mojibake_detects_known_broken():
    """MOJIBAKE_1 must be flagged."""
    from encoding_guard import looks_mojibake
    assert looks_mojibake(MOJIBAKE_1) is True


def test_looks_mojibake_detects_second_key():
    """MOJIBAKE_2 must be flagged."""
    from encoding_guard import looks_mojibake
    assert looks_mojibake(MOJIBAKE_2) is True


def test_looks_mojibake_false_on_clean_ascii():
    from encoding_guard import looks_mojibake
    assert looks_mojibake("Facturka.bg") is False


def test_looks_mojibake_false_on_normal_bulgarian():
    """Critical: normal Bulgarian must NOT trigger detection.
    'Разходка' starts with Р(U+0420) followed by а(U+0430) -- core BG range.
    """
    from encoding_guard import looks_mojibake
    # Разходка -- codepoints all in core BG range
    bg_codes = [0x420, 0x430, 0x437, 0x445, 0x43e, 0x434, 0x43a, 0x430]
    bg = "".join(chr(c) for c in bg_codes)
    assert looks_mojibake(bg) is False


def test_looks_mojibake_false_on_normal_bulgarian_mixed():
    """'Системата работи правилно' -- normal BG sentence."""
    from encoding_guard import looks_mojibake
    # С(0x421) + и(0x438) + с(0x441) + т(0x442)... all core
    text_codes = [
        0x421, 0x438, 0x441, 0x442, 0x435, 0x43c, 0x430, 0x442, 0x430,
        0x20,
        0x440, 0x430, 0x431, 0x43e, 0x442, 0x438,
    ]
    text = "".join(chr(c) for c in text_codes)
    assert looks_mojibake(text) is False


def test_looks_mojibake_false_on_empty():
    from encoding_guard import looks_mojibake
    assert looks_mojibake("") is False


def test_looks_mojibake_false_on_project_names():
    """Real ASCII project names from the metrics file must not be flagged."""
    from encoding_guard import looks_mojibake
    for name in ["Facturka.bg", "CasinoScore AI", "StroyOffice Pro",
                 "Cinemind", "higgsfield.ai", "Davinci Plugin", "Reed"]:
        assert looks_mojibake(name) is False, f"False positive on: {name!r}"


# ---------------------------------------------------------------------------
# guard
# ---------------------------------------------------------------------------

def test_guard_fixes_broken():
    from encoding_guard import guard
    result = guard(MOJIBAKE_1)
    assert [ord(c) for c in result] == FIXED_1_CODES


def test_guard_passes_clean_unchanged():
    from encoding_guard import guard
    s = "Facturka.bg"
    assert guard(s) == s


def test_guard_passes_bulgarian_unchanged():
    """Normal BG text must pass through guard without being corrupted."""
    from encoding_guard import guard
    bg_codes = [0x420, 0x430, 0x437, 0x445, 0x43e, 0x434, 0x43a, 0x430]
    bg = "".join(chr(c) for c in bg_codes)
    result = guard(bg)
    # Core chars must still be present
    assert any(0x0410 <= ord(c) <= 0x044F for c in result)


def test_guard_idempotent_on_fixed_text():
    """After fix, calling guard again must return the same result."""
    from encoding_guard import guard
    once = guard(MOJIBAKE_1)
    twice = guard(once)
    assert once == twice


# ---------------------------------------------------------------------------
# scan_json_keys
# ---------------------------------------------------------------------------

def test_scan_json_keys_finds_broken_key():
    from encoding_guard import scan_json_keys
    obj = {MOJIBAKE_1: {"surfaced": 8, "engaged": 0}}
    found = scan_json_keys(obj)
    assert MOJIBAKE_1 in found


def test_scan_json_keys_finds_nested():
    from encoding_guard import scan_json_keys
    obj = {"per_project": {MOJIBAKE_2: {"x": 1}}}
    found = scan_json_keys(obj)
    assert MOJIBAKE_2 in found


def test_scan_json_keys_clean_object_empty():
    from encoding_guard import scan_json_keys
    obj = {"Facturka.bg": {"surfaced": 2}, "CasinoScore AI": {"surfaced": 12}}
    assert scan_json_keys(obj) == []


def test_scan_json_keys_in_list():
    from encoding_guard import scan_json_keys
    obj = ["ok", MOJIBAKE_1, "also ok"]
    found = scan_json_keys(obj)
    assert len(found) == 1
    assert found[0] == MOJIBAKE_1


def test_scan_json_keys_real_metrics_structure():
    """Simulate the real cross-recall-metrics.json structure with 2 bad keys."""
    from encoding_guard import scan_json_keys
    data = {
        "per_project": {
            "Facturka.bg": {"surfaced": 24, "engaged": 5},
            MOJIBAKE_1: {"surfaced": 8, "engaged": 0},
            MOJIBAKE_2: {"surfaced": 10, "engaged": 0},
        }
    }
    found = scan_json_keys(data)
    assert len(found) == 2


# ---------------------------------------------------------------------------
# CLI check (integration via subprocess)
# ---------------------------------------------------------------------------

def test_cli_check_finds_mojibake(tmp_path):
    """CLI must find and report the two mojibake keys."""
    import json
    import subprocess

    data = {
        "per_project": {
            "Facturka.bg": {"surfaced": 2},
            MOJIBAKE_1: {"surfaced": 8},
            MOJIBAKE_2: {"surfaced": 10},
        }
    }
    f = tmp_path / "test.json"
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    script = Path.home() / ".claude" / "scripts" / "encoding_guard.py"
    result = subprocess.run(
        [sys.executable, str(script), "check", str(f)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0
    assert "Found" in result.stdout or "mojibake" in result.stdout.lower()


def test_cli_check_clean_file_no_results(tmp_path):
    """CLI on a clean file should report no mojibake."""
    import json
    import subprocess

    data = {"per_project": {"Facturka.bg": {"surfaced": 2}}}
    f = tmp_path / "clean.json"
    f.write_text(json.dumps(data), encoding="utf-8")

    script = Path.home() / ".claude" / "scripts" / "encoding_guard.py"
    result = subprocess.run(
        [sys.executable, str(script), "check", str(f)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0
    assert "No mojibake" in result.stdout
