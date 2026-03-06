"""Preflight engine – orchestrates all rule checks."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from app.config import load_config
from app.models import PreflightResult, RuleStatus
from app.preflight.rules.drill_holes import check_drill_holes
from app.preflight.rules.fonts import check_fonts
from app.preflight.rules.image_dpi import check_image_dpi
from app.preflight.rules.min_size import check_min_size
from app.preflight.rules.overprint_check import check_overprint
from app.preflight.rules.page_size import check_page_sizes
from app.preflight.rules.rgb_check import check_rgb
from app.preflight.rules.safe_area import check_safe_area
from app.preflight.rules.spot_colors import check_cut_contour, check_die
from app.preflight.rules.spotcolor_list import check_spot_colors_list

logger = logging.getLogger(__name__)


def run_preflight(
    pdf_path: str | Path,
    config_override: dict[str, Any] | None = None,
    has_contour_cut: bool = False,
    bleed_mm: float = 10.0,
    product_profile: str | None = None,
    safe_margin_mm: float = 0.0,
    has_drill_holes: bool = False,
    scale: int = 1,
) -> PreflightResult:
    """Run the full preflight analysis on *pdf_path* and return structured results.

    No modifications are made to the PDF – read-only analysis.

    Args:
        has_contour_cut: If True, check for cut contour and die separations.
                         If False, skip those checks entirely.
        bleed_mm: Target bleed in mm from the UI.  0 = disable bleed check
                  (only report boxes/format, status always PASS).
        scale: Maßstab-Faktor (1 = 1:1, 10 = 1:10). Beeinflusst DPI-Mindest-
               anforderung, Bleed- und Safe-Area-Schwellwerte im PDF.
    """
    pdf_path = str(pdf_path)
    cfg = config_override or load_config()

    doc = fitz.open(pdf_path)
    try:
        result = PreflightResult(
            filename=Path(pdf_path).name,
            total_pages=len(doc),
            contour_check_enabled=has_contour_cut,
            product_profile=product_profile,
            drill_holes_check_enabled=has_drill_holes,
            scale=scale,
        )

        # 1) Page sizes & bleed
        # Bei Maßstab 1:10 ist das Bleed im PDF 10× kleiner als im Druck
        effective_bleed_mm = bleed_mm / scale if scale > 1 else bleed_mm
        result.page_sizes = check_page_sizes(doc, cfg, target_bleed_mm=effective_bleed_mm)

        # 2) Image DPI
        # Bei Maßstab 1:10 muss die DPI im PDF 10× höher sein (z.B. min 720 statt 72)
        all_images, total_count = check_image_dpi(doc, cfg, scale=scale)
        result.images = all_images
        result.images_total_count = total_count

        # 3) Font check
        result.font_check = check_fonts(doc, cfg)

        # 4) Spot colour listing (with allowlist for product profiles)
        if product_profile == "wahlplakate":
            profile_cfg = cfg.get("product_profiles", {}).get("wahlplakate", {})
            allowed = profile_cfg.get("allowed_spot_names", [])
            result.spot_color_list = check_spot_colors_list(
                pdf_path, cfg, allowed_spot_names=allowed,
            )
        else:
            result.spot_color_list = check_spot_colors_list(pdf_path, cfg)

        # 5) RGB / Gray alarm
        result.rgb_check = check_rgb(doc, cfg)

        # 6) Minimum size (profile-specific)
        if product_profile == "wahlplakate":
            profile_cfg = cfg.get("product_profiles", {}).get("wahlplakate", {})
            min_size_mm = profile_cfg.get("min_size_mm", [100, 100])
            result.min_size = check_min_size(
                doc, cfg, min_w_mm=min_size_mm[0], min_h_mm=min_size_mm[1],
            )

        # 7) Safe area (when margin > 0)
        # Bei Maßstab 1:10 ist der Sicherheitsabstand im PDF entsprechend kleiner
        effective_safe_margin_mm = safe_margin_mm / scale if scale > 1 else safe_margin_mm
        if safe_margin_mm > 0:
            result.safe_area = check_safe_area(doc, cfg, effective_safe_margin_mm)

        # 8) Overprint check (profile-specific)
        if product_profile == "wahlplakate":
            result.overprint_check = check_overprint(pdf_path, cfg)

        # 9) Cut contour – only if enabled
        if has_contour_cut:
            result.cut_contour = check_cut_contour(pdf_path, doc, cfg)
            # 10) Die / Stanze
            #   For wahlplakate: Stanze is optional → cap at WARN (never FAIL)
            if product_profile == "wahlplakate":
                result.die = check_die(pdf_path, doc, cfg)
                if result.die and result.die.status == RuleStatus.FAIL:
                    result.die.status = RuleStatus.WARN
                    result.die.messages = [
                        m.replace("No die/Stanze", "Stanze/Die nicht gefunden (optional bei Wahlplakate)")
                        for m in result.die.messages
                    ]
            else:
                result.die = check_die(pdf_path, doc, cfg)
        else:
            result.cut_contour = None
            result.die = None

        # 11) Drill holes – only if enabled
        if has_drill_holes:
            result.drill_holes = check_drill_holes(pdf_path, doc, cfg)
        else:
            result.drill_holes = None

        # Derive overall
        result.compute_overall()
        return result

    finally:
        doc.close()
