"""Unit tests for minimum page size check."""

from unittest.mock import MagicMock, patch

import pytest

from app.models import MinSizeResult, RuleStatus


def _make_mock_doc(page_rects: list[tuple[float, float, float, float]]):
    """Create a mock fitz.Document with pages of given MediaBox rects (pt)."""
    doc = MagicMock()
    doc.__len__ = lambda self: len(page_rects)
    pages = []
    for rect in page_rects:
        page = MagicMock()
        # Simulate fitz.Rect-like object
        mock_rect = MagicMock()
        mock_rect.x0 = rect[0]
        mock_rect.y0 = rect[1]
        mock_rect.x1 = rect[2]
        mock_rect.y1 = rect[3]
        mock_rect.width = rect[2] - rect[0]
        mock_rect.height = rect[3] - rect[1]
        # mediabox == trimbox == cropbox → only MediaBox
        page.mediabox = mock_rect
        page.trimbox = mock_rect
        page.cropbox = mock_rect
        pages.append(page)
    doc.__getitem__ = lambda self, idx: pages[idx]
    return doc


PT_TO_MM = 25.4 / 72.0


class TestMinSize:
    """Tests for check_min_size rule."""

    def test_large_page_passes(self):
        """200×300 mm page → PASS for 100×100 mm minimum."""
        from app.preflight.rules.min_size import check_min_size

        w_pt = 200.0 / PT_TO_MM
        h_pt = 300.0 / PT_TO_MM
        doc = _make_mock_doc([(0, 0, w_pt, h_pt)])
        result = check_min_size(doc, {}, min_w_mm=100, min_h_mm=100)
        assert result.status == RuleStatus.PASS
        assert result.pages_failed == []
        assert len(result.pages_detail) == 1

    def test_small_page_fails(self):
        """50×200 mm page → FAIL (width < 100)."""
        from app.preflight.rules.min_size import check_min_size

        w_pt = 50.0 / PT_TO_MM
        h_pt = 200.0 / PT_TO_MM
        doc = _make_mock_doc([(0, 0, w_pt, h_pt)])
        result = check_min_size(doc, {}, min_w_mm=100, min_h_mm=100)
        assert result.status == RuleStatus.FAIL
        assert result.pages_failed == [1]

    def test_exact_boundary_passes(self):
        """100×100 mm page (exact boundary) → PASS."""
        from app.preflight.rules.min_size import check_min_size

        w_pt = 100.0 / PT_TO_MM
        h_pt = 100.0 / PT_TO_MM
        doc = _make_mock_doc([(0, 0, w_pt, h_pt)])
        result = check_min_size(doc, {}, min_w_mm=100, min_h_mm=100)
        assert result.status == RuleStatus.PASS
        assert result.pages_failed == []

    def test_multi_page_second_fails(self):
        """Page 1 OK (200×300), Page 2 too small (50×80) → FAIL, pages_failed=[2]."""
        from app.preflight.rules.min_size import check_min_size

        w1 = 200.0 / PT_TO_MM
        h1 = 300.0 / PT_TO_MM
        w2 = 50.0 / PT_TO_MM
        h2 = 80.0 / PT_TO_MM
        doc = _make_mock_doc([(0, 0, w1, h1), (0, 0, w2, h2)])
        result = check_min_size(doc, {}, min_w_mm=100, min_h_mm=100)
        assert result.status == RuleStatus.FAIL
        assert result.pages_failed == [2]
        assert len(result.pages_detail) == 2

    def test_height_too_small_fails(self):
        """Width OK but height too small → FAIL."""
        from app.preflight.rules.min_size import check_min_size

        w_pt = 200.0 / PT_TO_MM
        h_pt = 50.0 / PT_TO_MM
        doc = _make_mock_doc([(0, 0, w_pt, h_pt)])
        result = check_min_size(doc, {}, min_w_mm=100, min_h_mm=100)
        assert result.status == RuleStatus.FAIL
        assert 1 in result.pages_failed
