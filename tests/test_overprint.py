"""Unit tests for overprint check."""

import pytest

from app.models import OverprintCheckResult, RuleStatus
from app.preflight.rules.overprint_check import _is_true


class TestIsTrue:
    """Test the _is_true helper for pikepdf boolean values."""

    def test_none(self):
        assert _is_true(None) is False

    def test_bool_true(self):
        assert _is_true(True) is True

    def test_bool_false(self):
        assert _is_true(False) is False

    def test_string_true(self):
        assert _is_true("true") is True
        assert _is_true("True") is True
        assert _is_true("TRUE") is True

    def test_string_false(self):
        assert _is_true("false") is False
        assert _is_true("something") is False


class TestOverprintResults:
    """Test OverprintCheckResult status logic."""

    def test_no_overprint_is_pass(self):
        result = OverprintCheckResult(
            overprint_used=False,
            pages_with_overprint=[],
            status=RuleStatus.PASS,
            message="Kein Überdrucken erkannt.",
        )
        assert result.status == RuleStatus.PASS
        assert result.overprint_used is False

    def test_overprint_detected_is_warn(self):
        result = OverprintCheckResult(
            overprint_used=True,
            pages_with_overprint=[1, 3],
            status=RuleStatus.WARN,
            message="Überdrucken aktiv auf Seite(n): 1, 3",
        )
        assert result.status == RuleStatus.WARN
        assert result.overprint_used is True
        assert result.pages_with_overprint == [1, 3]
