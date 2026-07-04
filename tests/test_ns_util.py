"""Tests for ns_util — namespace sanitization + alias canonicalization."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def test_cyrillic_transliteration():
    from ns_util import sanitize_ns
    assert sanitize_ns("Клошар") == "Kloshar"


def test_ascii_passthrough():
    from ns_util import sanitize_ns
    assert sanitize_ns("DCTL") == "DCTL"
    assert sanitize_ns("_claude_meta") == "_claude_meta"
    assert sanitize_ns("_meta") == "_meta"


def test_space_becomes_dash():
    from ns_util import sanitize_ns
    # "AI Video" → sanitize space→dash → "AI-Video" (also the canonical)
    assert sanitize_ns("AI Video") == "AI-Video"


def test_alias_canonicalization():
    from ns_util import sanitize_ns
    # All Trading variants converge
    assert sanitize_ns("Claude Trading") == "Trading"
    assert sanitize_ns("Claude-Trading") == "Trading"
    assert sanitize_ns("Trading") == "Trading"
    # Spelling / suffix variants
    assert sanitize_ns("Facturka.bg") == "Fakturka.bg"
    assert sanitize_ns("CasinoScore AI") == "CasinoScore"
    assert sanitize_ns("Web-Design") == "WebDesign"
    assert sanitize_ns("Web Designe") == "WebDesign"
    assert sanitize_ns("Davinci Plugin") == "Davinci-Plugin"
    assert sanitize_ns("shared") == "_shared"


def test_canonical_names_are_stable():
    from ns_util import sanitize_ns
    # Applying twice is idempotent (canonical → canonical)
    for n in ["Trading", "Fakturka.bg", "CasinoScore", "WebDesign", "_shared"]:
        assert sanitize_ns(sanitize_ns(n)) == sanitize_ns(n)


def test_empty_defaults():
    from ns_util import sanitize_ns
    assert sanitize_ns("") == "default"


# ---------------------------------------------------------------------------
# Mojibake input (cp1251-misread UTF-8 from Windows piped hook stdin)
# Fixtures use NEUTRAL words only — this file ships publicly (see
# test_encoding_guard.py for the same policy).
# ---------------------------------------------------------------------------

def _to_mojibake(s: str) -> str:
    """Reproduce the hook-stdin corruption: UTF-8 bytes misread as cp1251."""
    return s.encode("utf-8").decode("cp1251")


def test_mojibake_input_repaired_before_translit():
    from ns_util import sanitize_ns
    # Without repair these would degrade to R/S husks like "RSRSSRRS"
    assert sanitize_ns(_to_mojibake("Клошар")) == "Kloshar"
    assert sanitize_ns(_to_mojibake("примерен проект")) == "primeren-proekt"


def test_mojibake_repair_idempotent():
    from ns_util import sanitize_ns
    out = sanitize_ns(_to_mojibake("Клошар"))
    assert sanitize_ns(out) == out


def test_genuine_cyrillic_not_misrepaired():
    from ns_util import sanitize_ns
    # The repair must never fire on clean Cyrillic input
    assert sanitize_ns("Клошар") == "Kloshar"
    assert sanitize_ns("примерен проект") == "primeren-proekt"


def test_research_wiki_alias_convergence():
    from ns_util import sanitize_ns
    # Hook-side map values and natural transliteration must converge on the
    # SAME canonical ns, else save and recall fragment.
    # 2026-07-03: Stoil aliases merged into canonical "{{PRIVATE_NS}}" (walled in
    # visibility_guard alongside the legacy names).
    assert sanitize_ns("{{PRIVATE_NS}}") == "{{PRIVATE_NS}}"
    assert sanitize_ns("{{PRIVATE_NS}}") == "{{PRIVATE_NS}}"
    assert sanitize_ns("{{PRIVATE_NS}}") == "{{PRIVATE_NS}}"
    assert sanitize_ns("Delo-Zoya") == "{{PRIVATE_NS}}"
    assert sanitize_ns("Petar-Danov") == "PetarDanov"
