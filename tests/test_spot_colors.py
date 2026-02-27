"""Unit tests for spot-colour name matching and Magenta detection."""

import pytest

from app.preflight.rules.spot_colors import (
    _is_magenta,
    _name_matches,
    _normalize_name,
)


class TestNormalizeName:
    """Test the separator-stripping normalization."""

    def test_simple(self):
        assert _normalize_name("KONTUR") == "KONTUR"

    def test_underscore(self):
        assert _normalize_name("CUT_KONTUR") == "CUTKONTUR"

    def test_hyphen(self):
        assert _normalize_name("CUT-KONTUR") == "CUTKONTUR"

    def test_space(self):
        assert _normalize_name("CUT KONTUR") == "CUTKONTUR"

    def test_mixed(self):
        assert _normalize_name("  cut_kontur  ") == "CUTKONTUR"

    def test_multiple_separators(self):
        assert _normalize_name("CUT__KONTUR") == "CUTKONTUR"
        assert _normalize_name("CUT - KONTUR") == "CUTKONTUR"


class TestNameMatches:
    def test_exact_match(self):
        allowed = ["KONTUR", "CUTCONTOUR", "KONTURSCHNITT"]
        assert _name_matches("KONTUR", allowed) is True
        assert _name_matches("CUTCONTOUR", allowed) is True
        assert _name_matches("KONTURSCHNITT", allowed) is True

    def test_case_insensitive(self):
        allowed = ["KONTUR", "CUTCONTOUR", "KONTURSCHNITT"]
        assert _name_matches("kontur", allowed) is True
        assert _name_matches("Kontur", allowed) is True
        assert _name_matches("CutContour", allowed) is True
        assert _name_matches("Konturschnitt", allowed) is True

    def test_no_match(self):
        allowed = ["KONTUR", "CUTCONTOUR", "KONTURSCHNITT"]
        assert _name_matches("SCHNITT", allowed) is False
        assert _name_matches("CUT", allowed) is False
        assert _name_matches("KONTUR2", allowed) is False

    def test_whitespace_trimmed(self):
        allowed = ["KONTUR"]
        assert _name_matches("  KONTUR  ", allowed) is True

    def test_die_names(self):
        allowed = ["STANZE", "DIE", "DIELINE"]
        assert _name_matches("Stanze", allowed) is True
        assert _name_matches("die", allowed) is True
        assert _name_matches("DieLine", allowed) is True
        assert _name_matches("STANZER", allowed) is False

    def test_cutkontur_variants(self):
        """'cutkontur' and its separator variants should all match."""
        allowed = ["CUTKONTUR", "CUT_KONTUR", "CUT-KONTUR"]
        # Exact matches
        assert _name_matches("CUTKONTUR", allowed) is True
        assert _name_matches("CUT_KONTUR", allowed) is True
        assert _name_matches("CUT-KONTUR", allowed) is True
        # Normalized matches (separator-tolerant)
        assert _name_matches("cutkontur", allowed) is True
        assert _name_matches("CutKontur", allowed) is True
        assert _name_matches("cut_kontur", allowed) is True
        assert _name_matches("cut-kontur", allowed) is True
        assert _name_matches("cut kontur", allowed) is True

    def test_cutkontur_single_entry_matches_all_variants(self):
        """A single 'CUTKONTUR' in allowed should match all separator variants."""
        allowed = ["CUTKONTUR"]
        assert _name_matches("CUTKONTUR", allowed) is True
        assert _name_matches("CutKontur", allowed) is True
        assert _name_matches("CUT_KONTUR", allowed) is True
        assert _name_matches("CUT-KONTUR", allowed) is True
        assert _name_matches("cut kontur", allowed) is True

    def test_bohrungen_still_works(self):
        """Ensure existing separation names still match."""
        allowed = ["BOHRUNGEN"]
        assert _name_matches("Bohrungen", allowed) is True
        assert _name_matches("BOHRUNGEN", allowed) is True
        assert _name_matches("bohrungen", allowed) is True


class TestIsMagenta:
    def test_exact_magenta(self):
        expected = [0, 100, 0, 0]
        assert _is_magenta([0, 100, 0, 0], expected) is True

    def test_within_tolerance(self):
        expected = [0, 100, 0, 0]
        assert _is_magenta([1.0, 98.0, 2.0, 1.0], expected) is True

    def test_outside_tolerance(self):
        expected = [0, 100, 0, 0]
        assert _is_magenta([0, 50, 0, 0], expected) is False  # M=50
        assert _is_magenta([100, 0, 0, 0], expected) is False  # Cyan

    def test_none_cmyk(self):
        expected = [0, 100, 0, 0]
        assert _is_magenta(None, expected) is None

    def test_black_not_magenta(self):
        expected = [0, 100, 0, 0]
        assert _is_magenta([0, 0, 0, 100], expected) is False
