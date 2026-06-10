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
