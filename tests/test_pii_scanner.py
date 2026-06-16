"""Tests for pii_scanner.py — checksum-gated PII detection for the ingest path."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def _make_egn(yymmdd: str = "500101", serial: str = "000") -> str:
    """Build a structurally-valid synthetic EGN (correct date + checksum)."""
    base = yymmdd + serial  # 9 digits
    weights = (2, 4, 8, 5, 10, 9, 7, 3, 6)
    cs = sum(int(d) * w for d, w in zip(base, weights)) % 11
    if cs == 10:
        cs = 0
    return base + str(cs)


# Well-known synthetic test values (never real data):
VALID_CARD = "4111111111111111"          # canonical Visa test number (Luhn-valid)
VALID_IBAN = "BG80BNBG96611020345678"     # canonical Bulgarian IBAN example


# ── EGN checksum validator ────────────────────────────────────────────────────

class TestEgn:
    def test_synthetic_egn_validates(self):
        from pii_scanner import is_valid_egn
        assert is_valid_egn(_make_egn("500101", "000")) is True

    def test_random_10_digits_with_bad_date_rejected(self):
        from pii_scanner import is_valid_egn
        # month "99" is not a valid EGN month field
        assert is_valid_egn("9999999999") is False

    def test_wrong_checksum_rejected(self):
        from pii_scanner import is_valid_egn
        good = _make_egn("500101", "000")
        bad = good[:9] + str((int(good[9]) + 1) % 10)
        assert is_valid_egn(bad) is False

    def test_non_digit_rejected(self):
        from pii_scanner import is_valid_egn
        assert is_valid_egn("50010100ab") is False
        assert is_valid_egn("123") is False


# ── Luhn / IBAN validators ────────────────────────────────────────────────────

class TestLuhnIban:
    def test_valid_card_passes_luhn(self):
        from pii_scanner import is_valid_luhn
        assert is_valid_luhn(VALID_CARD) is True

    def test_bad_card_fails_luhn(self):
        from pii_scanner import is_valid_luhn
        assert is_valid_luhn("4111111111111112") is False

    def test_too_short_not_card(self):
        from pii_scanner import is_valid_luhn
        assert is_valid_luhn("12345") is False

    def test_valid_iban(self):
        from pii_scanner import is_valid_iban
        assert is_valid_iban(VALID_IBAN) is True

    def test_iban_with_spaces_valid(self):
        from pii_scanner import is_valid_iban
        assert is_valid_iban("BG80 BNBG 9661 1020 3456 78") is True

    def test_bad_iban_rejected(self):
        from pii_scanner import is_valid_iban
        assert is_valid_iban("BG00BNBG96611020345678") is False


# ── scan() ────────────────────────────────────────────────────────────────────

class TestScan:
    def test_clean_text_no_findings(self):
        from pii_scanner import scan
        assert scan("just a normal note about the project roadmap") == []

    def test_empty_text(self):
        from pii_scanner import scan
        assert scan("") == []
        assert scan(None) == []  # type: ignore

    def test_egn_detected_high(self):
        from pii_scanner import scan, CONF_HIGH
        egn = _make_egn("750316", "926")
        kinds = {(f.kind, f.confidence) for f in scan(f"клиент ЕГН {egn} плати")}
        assert ("egn", CONF_HIGH) in kinds

    def test_invoice_number_not_flagged_as_egn(self):
        from pii_scanner import scan
        # 10-digit invoice with an impossible EGN month → no egn finding
        assert not any(f.kind == "egn" for f in scan("Invoice 9912345678 paid"))

    def test_card_detected_high(self):
        from pii_scanner import scan, CONF_HIGH
        assert any(f.kind == "credit-card" and f.confidence == CONF_HIGH
                   for f in scan(f"card {VALID_CARD} exp 12/29"))

    def test_iban_detected_high(self):
        from pii_scanner import scan
        assert any(f.kind == "iban" for f in scan(f"IBAN: {VALID_IBAN}"))

    def test_private_marker_detected_high(self):
        from pii_scanner import scan, CONF_HIGH
        assert any(f.kind == "private-marker" and f.confidence == CONF_HIGH
                   for f in scan("бележки по {{PRIVATE_NS}} от вчера"))

    def test_email_is_medium(self):
        from pii_scanner import scan, CONF_MEDIUM
        fs = scan("пиши на ivan.petrov@example.com")
        assert any(f.kind == "email" and f.confidence == CONF_MEDIUM for f in fs)

    def test_phone_is_medium(self):
        from pii_scanner import scan, CONF_MEDIUM
        fs = scan("тел +359 88 123 4567 за връзка")
        assert any(f.kind == "phone" and f.confidence == CONF_MEDIUM for f in fs)

    def test_excerpt_is_masked(self):
        from pii_scanner import scan
        egn = _make_egn("500101", "000")
        f = next(f for f in scan(egn) if f.kind == "egn")
        assert egn not in f.excerpt  # raw value never retained verbatim
        assert "*" in f.excerpt


# ── should_block_ingest() ─────────────────────────────────────────────────────

class TestShouldBlock:
    def test_blocks_on_egn(self):
        from pii_scanner import should_block_ingest
        assert should_block_ingest(f"ЕГН {_make_egn()}") is True

    def test_blocks_on_private_marker(self):
        from pii_scanner import should_block_ingest
        assert should_block_ingest("{{PRIVATE_NS}} материали") is True

    def test_does_not_block_clean_text(self):
        from pii_scanner import should_block_ingest
        assert should_block_ingest("обикновена бележка за задачата") is False

    def test_email_alone_does_not_block(self):
        from pii_scanner import should_block_ingest
        # MEDIUM only → not blocked
        assert should_block_ingest("contact a@b.com") is False


# ── redact() ──────────────────────────────────────────────────────────────────

class TestRedact:
    def test_redacts_egn(self):
        from pii_scanner import redact
        egn = _make_egn("500101", "000")
        out = redact(f"клиент {egn} край")
        assert egn not in out
        assert "[REDACTED:egn]" in out

    def test_redacts_private_marker(self):
        from pii_scanner import redact
        out = redact("по {{PRIVATE_NS}} точка")
        assert "{{PRIVATE_NS}}" not in out
        assert "[REDACTED:private-marker]" in out

    def test_leaves_email_intact(self):
        from pii_scanner import redact
        out = redact("mail a@b.com")
        assert "a@b.com" in out  # MEDIUM findings are not redacted

    def test_empty_passthrough(self):
        from pii_scanner import redact
        assert redact("") == ""
        assert redact("nothing sensitive") == "nothing sensitive"
