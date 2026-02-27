"""Unit tests for Pydantic models and overall status computation."""

from app.models import (
    CutContourResult,
    DieResult,
    DrillHolesResult,
    FontCheckResult,
    FontInfo,
    ImageDPIResult,
    MinSizeResult,
    OverprintCheckResult,
    PageSizeResult,
    PreflightResult,
    ProofRequest,
    RGBCheckResult,
    RuleStatus,
    SafeAreaResult,
    SafeAreaViolation,
    SpotColorListResult,
)


class TestOverallStatus:
    def test_all_pass_with_contour(self):
        r = PreflightResult(
            filename="test.pdf",
            total_pages=1,
            page_sizes=[PageSizeResult(page=1, boxes=[], bleed_status=RuleStatus.PASS, bleed_message="ok")],
            contour_check_enabled=True,
            cut_contour=CutContourResult(found=True, status=RuleStatus.PASS),
            die=DieResult(found=True, status=RuleStatus.PASS),
        )
        r.compute_overall()
        assert r.overall_status == RuleStatus.PASS

    def test_all_pass_without_contour(self):
        """When contour checks are disabled (None), overall should still be PASS."""
        r = PreflightResult(
            filename="test.pdf",
            total_pages=1,
            page_sizes=[PageSizeResult(page=1, boxes=[], bleed_status=RuleStatus.PASS, bleed_message="ok")],
            contour_check_enabled=False,
            cut_contour=None,
            die=None,
        )
        r.compute_overall()
        assert r.overall_status == RuleStatus.PASS

    def test_warn_propagates(self):
        r = PreflightResult(
            filename="test.pdf",
            total_pages=1,
            page_sizes=[PageSizeResult(page=1, boxes=[], bleed_status=RuleStatus.PASS, bleed_message="ok")],
            cut_contour=CutContourResult(found=True, status=RuleStatus.WARN),
            die=DieResult(found=True, status=RuleStatus.PASS),
        )
        r.compute_overall()
        assert r.overall_status == RuleStatus.WARN

    def test_fail_takes_precedence(self):
        r = PreflightResult(
            filename="test.pdf",
            total_pages=1,
            page_sizes=[PageSizeResult(page=1, boxes=[], bleed_status=RuleStatus.WARN, bleed_message="low")],
            cut_contour=CutContourResult(found=False, status=RuleStatus.FAIL),
            die=DieResult(found=True, status=RuleStatus.PASS),
        )
        r.compute_overall()
        assert r.overall_status == RuleStatus.FAIL

    def test_image_fail_propagates(self):
        r = PreflightResult(
            filename="test.pdf",
            total_pages=1,
            images=[
                ImageDPIResult(
                    page=1, image_index=0, x_pt=0, y_pt=0, width_pt=72, height_pt=72,
                    pixel_width=50, pixel_height=50, effective_dpi_x=50, effective_dpi_y=50,
                    effective_dpi=50, min_dpi=72, status=RuleStatus.FAIL, message="low",
                ),
            ],
            cut_contour=CutContourResult(found=True, status=RuleStatus.PASS),
            die=DieResult(found=True, status=RuleStatus.PASS),
        )
        r.compute_overall()
        assert r.overall_status == RuleStatus.FAIL

    def test_contour_disabled_does_not_contribute(self):
        """A bleed WARN with contour disabled should result in WARN, not FAIL."""
        r = PreflightResult(
            filename="test.pdf",
            total_pages=1,
            page_sizes=[PageSizeResult(page=1, boxes=[], bleed_status=RuleStatus.WARN, bleed_message="low")],
            contour_check_enabled=False,
            cut_contour=None,
            die=None,
        )
        r.compute_overall()
        assert r.overall_status == RuleStatus.WARN

    def test_font_fail_propagates(self):
        """Not-embedded font → FAIL should propagate to overall."""
        r = PreflightResult(
            filename="test.pdf",
            total_pages=1,
            page_sizes=[PageSizeResult(page=1, boxes=[], bleed_status=RuleStatus.PASS, bleed_message="ok")],
            font_check=FontCheckResult(
                fonts=[FontInfo(name="Arial", page=1, is_embedded=False, status=RuleStatus.FAIL)],
                total_fonts=1,
                not_embedded_count=1,
                status=RuleStatus.FAIL,
            ),
        )
        r.compute_overall()
        assert r.overall_status == RuleStatus.FAIL

    def test_font_type3_warn_propagates(self):
        """Type3 font → WARN should propagate to overall."""
        r = PreflightResult(
            filename="test.pdf",
            total_pages=1,
            page_sizes=[PageSizeResult(page=1, boxes=[], bleed_status=RuleStatus.PASS, bleed_message="ok")],
            font_check=FontCheckResult(
                fonts=[FontInfo(name="Custom", page=1, is_embedded=True, is_type3=True, status=RuleStatus.WARN)],
                total_fonts=1,
                type3_count=1,
                status=RuleStatus.WARN,
            ),
        )
        r.compute_overall()
        assert r.overall_status == RuleStatus.WARN

    def test_rgb_warn_propagates(self):
        """DeviceRGB → WARN should propagate to overall."""
        r = PreflightResult(
            filename="test.pdf",
            total_pages=1,
            page_sizes=[PageSizeResult(page=1, boxes=[], bleed_status=RuleStatus.PASS, bleed_message="ok")],
            rgb_check=RGBCheckResult(
                has_device_rgb=True,
                pages_with_rgb=[1],
                status=RuleStatus.WARN,
                message="RGB",
            ),
        )
        r.compute_overall()
        assert r.overall_status == RuleStatus.WARN

    def test_spotcolor_warn_propagates(self):
        """Spotcolor with process-colour name → WARN should propagate."""
        r = PreflightResult(
            filename="test.pdf",
            total_pages=1,
            page_sizes=[PageSizeResult(page=1, boxes=[], bleed_status=RuleStatus.PASS, bleed_message="ok")],
            spot_color_list=SpotColorListResult(
                total_count=1,
                status=RuleStatus.WARN,
                messages=["Prozessfarben als Spotfarbe"],
            ),
        )
        r.compute_overall()
        assert r.overall_status == RuleStatus.WARN

    def test_all_new_checks_pass(self):
        """All new checks PASS → overall PASS."""
        r = PreflightResult(
            filename="test.pdf",
            total_pages=1,
            page_sizes=[PageSizeResult(page=1, boxes=[], bleed_status=RuleStatus.PASS, bleed_message="ok")],
            font_check=FontCheckResult(total_fonts=5, status=RuleStatus.PASS),
            spot_color_list=SpotColorListResult(total_count=2, status=RuleStatus.PASS),
            rgb_check=RGBCheckResult(has_device_rgb=False, status=RuleStatus.PASS),
            contour_check_enabled=False,
        )
        r.compute_overall()
        assert r.overall_status == RuleStatus.PASS

    def test_safe_area_warn_propagates(self):
        """SafeArea WARN → overall WARN."""
        r = PreflightResult(
            filename="test.pdf",
            total_pages=1,
            page_sizes=[PageSizeResult(page=1, boxes=[], bleed_status=RuleStatus.PASS, bleed_message="ok")],
            safe_area=SafeAreaResult(
                safe_margin_mm=5.0,
                violations=[SafeAreaViolation(page=1, description="Textblock", overflow_mm=1.5)],
                status=RuleStatus.WARN,
            ),
        )
        r.compute_overall()
        assert r.overall_status == RuleStatus.WARN

    def test_drill_holes_fail_propagates(self):
        """DrillHoles FAIL → overall FAIL."""
        r = PreflightResult(
            filename="test.pdf",
            total_pages=1,
            page_sizes=[PageSizeResult(page=1, boxes=[], bleed_status=RuleStatus.PASS, bleed_message="ok")],
            drill_holes=DrillHolesResult(
                found=False,
                separation_present=False,
                status=RuleStatus.FAIL,
                messages=["Sonderfarbe 'Bohrungen' nicht gefunden"],
            ),
        )
        r.compute_overall()
        assert r.overall_status == RuleStatus.FAIL

    def test_min_size_fail_propagates(self):
        """MinSize FAIL → overall FAIL."""
        r = PreflightResult(
            filename="test.pdf",
            total_pages=1,
            page_sizes=[PageSizeResult(page=1, boxes=[], bleed_status=RuleStatus.PASS, bleed_message="ok")],
            min_size=MinSizeResult(
                min_width_mm=100,
                min_height_mm=100,
                pages_failed=[1],
                status=RuleStatus.FAIL,
                message="Seite(n) 1: Endformat unter Minimum",
            ),
        )
        r.compute_overall()
        assert r.overall_status == RuleStatus.FAIL

    def test_overprint_warn_propagates(self):
        """Overprint WARN → overall WARN."""
        r = PreflightResult(
            filename="test.pdf",
            total_pages=1,
            page_sizes=[PageSizeResult(page=1, boxes=[], bleed_status=RuleStatus.PASS, bleed_message="ok")],
            overprint_check=OverprintCheckResult(
                overprint_used=True,
                pages_with_overprint=[1],
                status=RuleStatus.WARN,
                message="Überdrucken aktiv",
            ),
        )
        r.compute_overall()
        assert r.overall_status == RuleStatus.WARN

    def test_all_wahlplakate_checks_pass(self):
        """All Wahlplakate-specific checks PASS → overall PASS."""
        r = PreflightResult(
            filename="test.pdf",
            total_pages=1,
            product_profile="wahlplakate",
            page_sizes=[PageSizeResult(page=1, boxes=[], bleed_status=RuleStatus.PASS, bleed_message="ok")],
            min_size=MinSizeResult(status=RuleStatus.PASS),
            safe_area=SafeAreaResult(status=RuleStatus.PASS),
            overprint_check=OverprintCheckResult(status=RuleStatus.PASS),
            drill_holes=DrillHolesResult(found=True, separation_present=True, status=RuleStatus.PASS),
            contour_check_enabled=False,
        )
        r.compute_overall()
        assert r.overall_status == RuleStatus.PASS


class TestProofRequestNewFields:
    """Test ProofRequest with new Wahlplakate fields."""

    def test_default_values(self):
        req = ProofRequest(
            customer_name="Test",
            order_number="123",
            version_number="v1",
        )
        assert req.product_profile is None
        assert req.safe_margin_mm == 0.0
        assert req.has_drill_holes is False

    def test_wahlplakate_fields(self):
        req = ProofRequest(
            customer_name="Test",
            order_number="123",
            version_number="v1",
            product_profile="wahlplakate",
            safe_margin_mm=5.0,
            has_drill_holes=True,
            bleed_mm=5.0,
        )
        assert req.product_profile == "wahlplakate"
        assert req.safe_margin_mm == 5.0
        assert req.has_drill_holes is True
        assert req.bleed_mm == 5.0
