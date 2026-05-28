"""Tests for the salience scoring module (amygdala analog).

Run with:
    python -m pytest C:\\Users\\Vatev\\.claude\\tests\\test_salience.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "scripts"))


def test_classify_markers_detects_categories():
    from salience import classify_markers

    assert "SECURITY" in classify_markers("found an auth token vulnerability")
    assert "MONEY" in classify_markers("the invoice payment failed")
    assert "ERRORS" in classify_markers("got a traceback exception")


def test_salience_score_security_higher_than_error():
    from salience import salience_score

    assert salience_score("security vulnerability in auth") > salience_score("got an error")


def test_salience_score_multi_category_stacks_and_caps():
    from salience import salience_score

    s = salience_score("security breach in production caused payment errors")  # 4 categories
    assert s == 1.0  # capped


def test_salience_score_benign_zero():
    from salience import salience_score

    assert salience_score("just refactored a helper function") == 0.0


def test_classify_markers_empty():
    from salience import classify_markers

    assert classify_markers("") == []


# --- additional edge-case tests ---

def test_classify_markers_returns_sorted():
    from salience import classify_markers

    result = classify_markers("payment error in production")
    assert result == sorted(result)


def test_classify_markers_case_insensitive():
    from salience import classify_markers

    assert "SECURITY" in classify_markers("AUTH TOKEN EXPOSED")
    assert "MONEY" in classify_markers("INVOICE UNPAID")
    assert "PRODUCTION" in classify_markers("PRODUCTION server down")
    assert "ERRORS" in classify_markers("TRACEBACK in worker")


def test_classify_markers_no_duplicates():
    from salience import classify_markers

    # Many SECURITY keywords — should still appear only once
    result = classify_markers("security auth password token exploit vulnerab breach CVE")
    assert result.count("SECURITY") == 1


def test_salience_score_single_category():
    from salience import salience_score

    # ERRORS alone = 0.3
    assert salience_score("stack trace error exception") == pytest_approx(0.3)


def test_salience_score_two_categories():
    from salience import salience_score

    # SECURITY(0.5) + MONEY(0.5) = 1.0 capped
    assert salience_score("payment token exposed") == 1.0


def test_salience_score_production_plus_errors():
    from salience import salience_score

    # PRODUCTION(0.4) + ERRORS(0.3) = 0.7
    assert salience_score("production outage caused a traceback") == pytest_approx(0.7)


def test_salience_score_bulgarian_money():
    from salience import salience_score
    from salience import classify_markers

    assert "MONEY" in classify_markers("плащане на фактура")
    assert salience_score("плащане на фактура") > 0.0


# helper import used by parametric tests above
try:
    from pytest import approx as pytest_approx
except ImportError:
    # fallback if running without pytest installed — should not happen in CI
    def pytest_approx(x, rel=None, abs=None):  # type: ignore[misc]
        return x
