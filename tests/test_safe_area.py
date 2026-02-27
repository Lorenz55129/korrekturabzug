"""Unit tests for safe area check."""

import fitz
import pytest

from app.models import RuleStatus
from app.preflight.rules.safe_area import _compute_overflow_pt


PT_TO_MM = 25.4 / 72.0


class TestSafeAreaBoxCalculation:
    """Test the SafeAreaBox geometry calculation."""

    def test_a4_5mm_margin(self):
        """A4 TrimBox=(0,0,595.28,841.89) with 5mm margin → safe=(14.17, 14.17, 581.11, 827.72)."""
        margin_mm = 5.0
        margin_pt = margin_mm / PT_TO_MM

        trim = fitz.Rect(0, 0, 595.28, 841.89)
        safe = fitz.Rect(
            trim.x0 + margin_pt,
            trim.y0 + margin_pt,
            trim.x1 - margin_pt,
            trim.y1 - margin_pt,
        )

        # margin_pt should be approximately 14.17
        assert abs(margin_pt - 14.17) < 0.1
        assert abs(safe.x0 - 14.17) < 0.1
        assert abs(safe.y0 - 14.17) < 0.1
        assert abs(safe.x1 - 581.11) < 0.1
        assert abs(safe.y1 - 827.72) < 0.1


class TestComputeOverflow:
    """Test the _compute_overflow_pt helper."""

    def test_inside_returns_zero(self):
        """Bbox fully inside safe area → overflow = 0."""
        safe = fitz.Rect(14.17, 14.17, 581.11, 827.72)
        bbox = fitz.Rect(100, 100, 200, 200)
        assert _compute_overflow_pt(bbox, safe) == 0

    def test_left_overflow(self):
        """Bbox extending left past safe area."""
        safe = fitz.Rect(14.17, 14.17, 581.11, 827.72)
        bbox = fitz.Rect(10, 100, 200, 200)  # x0=10 < safe.x0=14.17
        overflow = _compute_overflow_pt(bbox, safe)
        assert overflow > 0
        assert abs(overflow - 4.17) < 0.1

    def test_right_overflow(self):
        """Bbox extending right past safe area."""
        safe = fitz.Rect(14.17, 14.17, 581.11, 827.72)
        bbox = fitz.Rect(100, 100, 590, 200)  # x1=590 > safe.x1=581.11
        overflow = _compute_overflow_pt(bbox, safe)
        assert overflow > 0
        assert abs(overflow - 8.89) < 0.1

    def test_top_overflow(self):
        """Bbox extending above safe area (y0 < safe.y0)."""
        safe = fitz.Rect(14.17, 14.17, 581.11, 827.72)
        bbox = fitz.Rect(100, 10, 200, 200)  # y0=10 < safe.y0=14.17
        overflow = _compute_overflow_pt(bbox, safe)
        assert overflow > 0

    def test_multiple_overflows_returns_max(self):
        """When overflow on multiple sides, return the max."""
        safe = fitz.Rect(14.17, 14.17, 581.11, 827.72)
        bbox = fitz.Rect(10, 10, 590, 830)  # overflow all sides
        overflow = _compute_overflow_pt(bbox, safe)
        # left=4.17, top=4.17, right=8.89, bottom=2.28
        assert overflow > 0
        assert abs(overflow - 8.89) < 0.1  # max is right overflow
