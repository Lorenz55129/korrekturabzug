"""Pydantic data models for preflight results and API contracts."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────

class RuleStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


# ── Page / Box info ─────────────────────────────────────

class BoxInfo(BaseModel):
    """Dimensions of a single PDF box in mm."""
    name: str = Field(..., description="Box type: MediaBox, CropBox, TrimBox, BleedBox")
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float


class PageSizeResult(BaseModel):
    page: int
    boxes: list[BoxInfo]
    trim_width_mm: Optional[float] = None
    trim_height_mm: Optional[float] = None
    bleed_mm: Optional[dict[str, float]] = Field(
        None, description="Bleed per side: top/bottom/left/right in mm"
    )
    bleed_status: RuleStatus = RuleStatus.PASS
    bleed_message: str = ""


# ── Image resolution ────────────────────────────────────

class ImageDPIResult(BaseModel):
    page: int
    image_index: int
    x_pt: float
    y_pt: float
    width_pt: float
    height_pt: float
    width_mm: float = 0.0
    height_mm: float = 0.0
    pixel_width: int
    pixel_height: int
    effective_dpi_x: float
    effective_dpi_y: float
    effective_dpi: float = Field(..., description="min(dpi_x, dpi_y)")
    min_dpi: int = 72
    status: RuleStatus = RuleStatus.PASS
    message: str = ""


# ── Font check ───────────────────────────────────────────

class FontInfo(BaseModel):
    """Information about a single font found in the PDF."""
    name: str
    page: int
    is_embedded: bool = True
    is_subset: bool = False
    is_type3: bool = False
    status: RuleStatus = RuleStatus.PASS
    message: str = ""


class FontCheckResult(BaseModel):
    """Aggregate font check result."""
    fonts: list[FontInfo] = Field(default_factory=list)
    total_fonts: int = 0
    not_embedded_count: int = 0
    subset_count: int = 0
    type3_count: int = 0
    status: RuleStatus = RuleStatus.PASS
    messages: list[str] = Field(default_factory=list)


# ── Spotcolor listing ────────────────────────────────────

class SpotColorEntry(BaseModel):
    """A single spot colour found in the PDF."""
    name: str
    cmyk: Optional[list[float]] = None
    pages: list[int] = Field(default_factory=list)


class SpotColorListResult(BaseModel):
    """List of all spot colours with optional name check."""
    spot_colors: list[SpotColorEntry] = Field(default_factory=list)
    total_count: int = 0
    disallowed_spots: list[str] = Field(default_factory=list)
    status: RuleStatus = RuleStatus.PASS
    messages: list[str] = Field(default_factory=list)


# ── RGB / Gray alarm ─────────────────────────────────────

class RGBCheckResult(BaseModel):
    """Result of checking for DeviceRGB and DeviceGray content."""
    has_device_rgb: bool = False
    has_device_gray: bool = False
    pages_with_rgb: list[int] = Field(default_factory=list)
    pages_with_gray: list[int] = Field(default_factory=list)
    status: RuleStatus = RuleStatus.PASS
    message: str = ""


# ── Spot colour / separation (contour/die) ───────────────

class SpotColorInfo(BaseModel):
    name: str
    cmyk: Optional[list[float]] = None
    is_expected_magenta: Optional[bool] = None


class CutContourResult(BaseModel):
    found: bool = False
    spot_color: Optional[SpotColorInfo] = None
    pages: list[int] = Field(default_factory=list)
    stroke_width_pt: Optional[float] = None
    is_unfilled: Optional[bool] = None
    is_closed: Optional[bool] = None
    is_overprint: Optional[bool] = None
    status: RuleStatus = RuleStatus.FAIL
    messages: list[str] = Field(default_factory=list)


class DieResult(BaseModel):
    found: bool = False
    name_matched: Optional[str] = None
    pages: list[int] = Field(default_factory=list)
    status: RuleStatus = RuleStatus.FAIL
    messages: list[str] = Field(default_factory=list)


# ── Minimum size ─────────────────────────────────────────

class MinSizeResult(BaseModel):
    """Result of minimum dimension check (all pages)."""
    min_width_mm: float = 100.0
    min_height_mm: float = 100.0
    pages_failed: list[int] = Field(default_factory=list)
    pages_detail: list[dict] = Field(default_factory=list)
    status: RuleStatus = RuleStatus.PASS
    message: str = ""


# ── Safe area ────────────────────────────────────────────

class SafeAreaViolation(BaseModel):
    """A single text/object that extends outside the safe area."""
    page: int
    description: str = ""
    bbox_mm: list[float] = Field(default_factory=list)
    overflow_mm: float = 0.0
    status: RuleStatus = RuleStatus.WARN


class SafeAreaResult(BaseModel):
    """Result of safe area margin check (max WARN, never FAIL)."""
    safe_margin_mm: float = 5.0
    violations: list[SafeAreaViolation] = Field(default_factory=list)
    detection_limited: bool = False
    status: RuleStatus = RuleStatus.PASS
    messages: list[str] = Field(default_factory=list)


# ── Drill holes ──────────────────────────────────────────

class DrillHoleInfo(BaseModel):
    """A single detected drill hole."""
    page: int
    hole_id: str = ""
    diameter_mm: float = 0.0
    edge_distance_mm: float = 0.0
    center_mm: list[float] = Field(default_factory=list)
    spot_name: str = ""
    is_circular: bool = True
    status: RuleStatus = RuleStatus.PASS
    note: str = ""


class DrillHolesResult(BaseModel):
    """Aggregate result for drill hole checks."""
    found: bool = False
    spot_color: Optional[SpotColorInfo] = None
    separation_present: bool = False
    extraction_limited: bool = False
    holes: list[DrillHoleInfo] = Field(default_factory=list)
    total_count: int = 0
    status: RuleStatus = RuleStatus.PASS
    messages: list[str] = Field(default_factory=list)


# ── Overprint check ──────────────────────────────────────

class OverprintCheckResult(BaseModel):
    """Result of overprint detection (WARN when used, PASS otherwise)."""
    overprint_used: bool = False
    pages_with_overprint: list[int] = Field(default_factory=list)
    status: RuleStatus = RuleStatus.PASS
    message: str = ""


# ── Aggregate ────────────────────────────────────────────

class PreflightResult(BaseModel):
    """Complete preflight report for a customer PDF."""
    filename: str
    total_pages: int
    scale: int = 1  # Maßstab-Faktor: 1 = 1:1, 10 = 1:10
    page_sizes: list[PageSizeResult] = Field(default_factory=list)
    images: list[ImageDPIResult] = Field(default_factory=list)
    images_total_count: int = 0
    font_check: Optional[FontCheckResult] = None
    spot_color_list: Optional[SpotColorListResult] = None
    rgb_check: Optional[RGBCheckResult] = None
    contour_check_enabled: bool = True
    cut_contour: Optional[CutContourResult] = None
    cutcontour_hint: Optional[str] = None  # spot name found when check is disabled
    die: Optional[DieResult] = None
    # Wahlplakate / profile fields
    product_profile: Optional[str] = None
    safe_area: Optional[SafeAreaResult] = None
    drill_holes: Optional[DrillHolesResult] = None
    drill_holes_check_enabled: bool = False
    min_size: Optional[MinSizeResult] = None
    overprint_check: Optional[OverprintCheckResult] = None
    overall_status: RuleStatus = RuleStatus.PASS

    def compute_overall(self) -> None:
        """Derive overall status from individual results."""
        statuses: list[RuleStatus] = []
        for ps in self.page_sizes:
            statuses.append(ps.bleed_status)
        for img in self.images:
            statuses.append(img.status)
        if self.font_check is not None:
            statuses.append(self.font_check.status)
        if self.spot_color_list is not None:
            statuses.append(self.spot_color_list.status)
        if self.rgb_check is not None:
            statuses.append(self.rgb_check.status)
        if self.cut_contour is not None:
            statuses.append(self.cut_contour.status)
        if self.die is not None:
            statuses.append(self.die.status)
        if self.safe_area is not None:
            statuses.append(self.safe_area.status)
        if self.drill_holes is not None:
            statuses.append(self.drill_holes.status)
        if self.min_size is not None:
            statuses.append(self.min_size.status)
        if self.overprint_check is not None:
            statuses.append(self.overprint_check.status)

        if RuleStatus.FAIL in statuses:
            self.overall_status = RuleStatus.FAIL
        elif RuleStatus.WARN in statuses:
            self.overall_status = RuleStatus.WARN
        else:
            self.overall_status = RuleStatus.PASS


# ── API request / response ──────────────────────────────

class ProofRequest(BaseModel):
    customer_name: str
    order_number: str
    version_number: str
    date: Optional[str] = None
    comment: Optional[str] = None
    quantity: Optional[int] = None
    has_contour_cut: bool = False
    material_execution: Optional[str] = None
    bleed_mm: float = Field(default=10.0, ge=0, le=30,
                            description="Target bleed in mm. 0 = disable bleed check.")
    product_profile: Optional[str] = None
    safe_margin_mm: float = Field(default=0.0, ge=0, le=30,
                                   description="Safe area margin in mm. 0 = disabled.")
    has_drill_holes: bool = False
    scale: int = Field(default=1, description="Maßstab-Faktor: 1 = 1:1, 10 = 1:10")


class ProofResponse(BaseModel):
    preflight: PreflightResult
    proof_pdf_url: str
    proof_pdf_filename: str = ""
    tech_svg_url: str = ""
    tech_svg_filename: str = ""
    detail_json_url: Optional[str] = None
    detail_json_filename: Optional[str] = None
    jobticket_json_url: Optional[str] = None
    jobticket_json_filename: Optional[str] = None
