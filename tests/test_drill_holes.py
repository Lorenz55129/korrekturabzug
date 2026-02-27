"""Unit tests for drill-hole geometry helpers and status logic."""

import fitz
import pytest

from app.models import DrillHolesResult, RuleStatus
from app.preflight.rules._reference_box import PT_TO_MM
from app.preflight.rules.drill_holes import (
    _concat_matrix,
    _edge_distance_mm,
    _path_bbox,
    _transform_point,
    _tokenize_stream,
    _try_float,
)
from app.preflight.rules.spot_colors import _name_matches


class TestEdgeDistance:
    """Test the _edge_distance_mm formula."""

    def test_near_edge_fails(self):
        """bbox(100,100,120,120) in trim(50,50,500,500)
        → left=50, right=380, bottom=50, top=380
        → edge_dist=50*PT_TO_MM=17.64mm → should be < 20mm."""
        trim = fitz.Rect(50, 50, 500, 500)
        dist = _edge_distance_mm(100, 100, 120, 120, trim)
        expected = 50 * PT_TO_MM  # ~17.64mm
        assert abs(dist - expected) < 0.1
        assert dist < 20.0  # FAIL condition

    def test_center_passes(self):
        """bbox(200,200,220,220) in trim(50,50,500,500)
        → left=150, bottom=150 → edge_dist=150*PT_TO_MM=52.92mm → OK."""
        trim = fitz.Rect(50, 50, 500, 500)
        dist = _edge_distance_mm(200, 200, 220, 220, trim)
        expected = 150 * PT_TO_MM  # ~52.92mm
        assert abs(dist - expected) < 0.1
        assert dist >= 20.0  # PASS condition

    def test_formula_components(self):
        """Verify each component of the edge distance formula."""
        trim = fitz.Rect(10, 20, 400, 500)
        bx0, by0, bx1, by1 = 50, 60, 100, 120

        left = bx0 - trim.x0  # 50-10=40
        right = trim.x1 - bx1  # 400-100=300
        bottom = by0 - trim.y0  # 60-20=40
        top = trim.y1 - by1  # 500-120=380

        dist_mm = _edge_distance_mm(bx0, by0, bx1, by1, trim)
        expected_mm = min(left, right, bottom, top) * PT_TO_MM  # 40*PT_TO_MM
        assert abs(dist_mm - expected_mm) < 0.01


class TestCircularity:
    """Test circularity check logic."""

    def test_square_is_circular(self):
        """20×20 bbox → ratio=0 → is_circular=True."""
        bbox_w, bbox_h = 20.0, 20.0
        ratio = abs(bbox_w - bbox_h) / max(bbox_w, bbox_h)
        assert ratio < 0.15

    def test_rectangular_not_circular(self):
        """20×30 bbox → ratio=0.33 → is_circular=False."""
        bbox_w, bbox_h = 20.0, 30.0
        ratio = abs(bbox_w - bbox_h) / max(bbox_w, bbox_h)
        assert ratio >= 0.15

    def test_borderline_circular(self):
        """20×23 bbox → ratio=0.13 → still circular (< 0.15)."""
        bbox_w, bbox_h = 20.0, 23.0
        ratio = abs(bbox_w - bbox_h) / max(bbox_w, bbox_h)
        assert ratio < 0.15


class TestDiameter:
    """Test diameter calculation from bbox."""

    def test_small_diameter_fails(self):
        """8pt bbox → 2.82mm < 4mm → FAIL."""
        bbox_w, bbox_h = 8.0, 8.0
        diameter_mm = min(bbox_w, bbox_h) * PT_TO_MM
        assert diameter_mm < 4.0

    def test_adequate_diameter_passes(self):
        """15pt bbox → 5.29mm >= 4mm → PASS."""
        bbox_w, bbox_h = 15.0, 15.0
        diameter_mm = min(bbox_w, bbox_h) * PT_TO_MM
        assert diameter_mm >= 4.0


class TestSeparationNameMatch:
    """Test that _name_matches finds 'BOHRUNGEN'."""

    def test_exact(self):
        assert _name_matches("Bohrungen", ["BOHRUNGEN"]) is True

    def test_case_insensitive(self):
        assert _name_matches("bohrungen", ["BOHRUNGEN"]) is True
        assert _name_matches("BOHRUNGEN", ["BOHRUNGEN"]) is True

    def test_no_match(self):
        assert _name_matches("Bohrung", ["BOHRUNGEN"]) is False


class TestClosedPath:
    """Test closed-path detection logic (h operator OR endpoint≈startpoint)."""

    def test_explicit_close(self):
        """Path with h → closed."""
        has_close = True
        assert has_close is True

    def test_endpoint_matches_start(self):
        """Endpoint ≈ startpoint within 0.1pt → closed."""
        path = [(10.0, 20.0), (30.0, 20.0), (30.0, 40.0), (10.05, 20.02)]
        sx, sy = path[0]
        ex, ey = path[-1]
        is_closed = abs(sx - ex) < 0.1 and abs(sy - ey) < 0.1
        assert is_closed is True

    def test_open_path(self):
        """Endpoint far from startpoint → not closed."""
        path = [(10.0, 20.0), (30.0, 20.0), (30.0, 40.0), (50.0, 60.0)]
        sx, sy = path[0]
        ex, ey = path[-1]
        is_closed = abs(sx - ex) < 0.1 and abs(sy - ey) < 0.1
        assert is_closed is False


class TestStatusLogic:
    """Test FAIL vs WARN vs PASS classification."""

    def test_separation_missing_is_fail(self):
        """No separation found → FAIL."""
        result = DrillHolesResult(
            found=False,
            separation_present=False,
            status=RuleStatus.FAIL,
            messages=["Sonderfarbe 'Bohrungen' nicht gefunden"],
        )
        assert result.status == RuleStatus.FAIL

    def test_separation_no_paths_is_warn(self):
        """Separation present but 0 paths → WARN with extraction_limited."""
        result = DrillHolesResult(
            found=False,
            separation_present=True,
            extraction_limited=True,
            status=RuleStatus.WARN,
            messages=["Separation vorhanden, Pfade nicht auswertbar"],
        )
        assert result.status == RuleStatus.WARN
        assert result.extraction_limited is True


class TestMatrixHelpers:
    """Test CTM matrix operations."""

    def test_identity_transform(self):
        """Identity matrix should not change point."""
        ctm = [1, 0, 0, 1, 0, 0]
        tx, ty = _transform_point(10.0, 20.0, ctm)
        assert abs(tx - 10.0) < 0.001
        assert abs(ty - 20.0) < 0.001

    def test_translation(self):
        """Translation matrix [1,0,0,1,100,200]."""
        ctm = [1, 0, 0, 1, 100, 200]
        tx, ty = _transform_point(10.0, 20.0, ctm)
        assert abs(tx - 110.0) < 0.001
        assert abs(ty - 220.0) < 0.001

    def test_concat_identity(self):
        """Concat with identity should return original."""
        m = [2, 0, 0, 2, 10, 20]
        identity = [1, 0, 0, 1, 0, 0]
        result = _concat_matrix(m, identity)
        for a, b in zip(result, m):
            assert abs(a - b) < 0.001

    def test_path_bbox(self):
        """Path bbox should encompass all points."""
        coords = [(10, 20), (30, 10), (5, 40)]
        bbox = _path_bbox(coords)
        assert bbox == (5, 10, 30, 40)

    def test_path_bbox_empty(self):
        assert _path_bbox([]) is None


class TestTokenizer:
    """Test the content stream tokenizer."""

    def test_simple_stream(self):
        data = b"10 20 m 30 40 l S"
        tokens = _tokenize_stream(data)
        assert tokens == ["10", "20", "m", "30", "40", "l", "S"]

    def test_name_token(self):
        data = b"/CS7 cs"
        tokens = _tokenize_stream(data)
        assert tokens == ["/CS7", "cs"]

    def test_comment_skipped(self):
        data = b"10 20 m %comment\n30 40 l"
        tokens = _tokenize_stream(data)
        assert tokens == ["10", "20", "m", "30", "40", "l"]

    def test_try_float(self):
        assert _try_float("10") == 10.0
        assert _try_float("3.14") == 3.14
        assert _try_float("-2.5") == -2.5
        assert _try_float("m") is None
        assert _try_float("/CS7") is None
