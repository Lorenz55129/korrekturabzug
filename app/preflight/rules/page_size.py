"""Rule: Page-box detection and bleed verification."""

from __future__ import annotations

from typing import Any

import fitz  # PyMuPDF

from app.models import BoxInfo, PageSizeResult, RuleStatus

PT_TO_MM = 25.4 / 72.0


def _rect_to_box(name: str, rect: fitz.Rect) -> BoxInfo:
    return BoxInfo(
        name=name,
        x_mm=round(rect.x0 * PT_TO_MM, 2),
        y_mm=round(rect.y0 * PT_TO_MM, 2),
        width_mm=round(rect.width * PT_TO_MM, 2),
        height_mm=round(rect.height * PT_TO_MM, 2),
    )


def check_page_sizes(
    doc: fitz.Document,
    cfg: dict[str, Any],
    target_bleed_mm: float = 10.0,
) -> list[PageSizeResult]:
    """Analyse page boxes and check bleed.

    Args:
        target_bleed_mm: Required minimum bleed in mm from the UI.
            If 0, bleed check is disabled (status always PASS, boxes still reported).
            Falls back to config ``page_size.min_bleed_mm`` if not explicitly given.
    """
    # Use the UI value; only fall back to config if caller passes the default
    min_bleed = target_bleed_mm if target_bleed_mm > 0 else 0.0
    bleed_check_enabled = target_bleed_mm > 0

    results: list[PageSizeResult] = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        boxes: list[BoxInfo] = []

        mediabox = page.mediabox
        boxes.append(_rect_to_box("MediaBox", mediabox))

        # CropBox defaults to MediaBox if not set
        cropbox = page.cropbox
        boxes.append(_rect_to_box("CropBox", cropbox))

        # TrimBox and BleedBox: PyMuPDF exposes them if present
        trimbox = page.trimbox
        bleedbox = page.bleedbox

        has_trimbox = trimbox != mediabox  # PyMuPDF falls back to mediabox
        has_bleedbox = bleedbox != mediabox

        if has_trimbox:
            boxes.append(_rect_to_box("TrimBox", trimbox))
        if has_bleedbox:
            boxes.append(_rect_to_box("BleedBox", bleedbox))

        trim_w = round(trimbox.width * PT_TO_MM, 2) if has_trimbox else round(cropbox.width * PT_TO_MM, 2)
        trim_h = round(trimbox.height * PT_TO_MM, 2) if has_trimbox else round(cropbox.height * PT_TO_MM, 2)

        # Compute bleed (distance from TrimBox/CropBox to BleedBox/MediaBox)
        outer = bleedbox if has_bleedbox else mediabox
        inner = trimbox if has_trimbox else cropbox

        bleed_left = round((inner.x0 - outer.x0) * PT_TO_MM, 2)
        bleed_bottom = round((inner.y0 - outer.y0) * PT_TO_MM, 2)
        bleed_right = round((outer.x1 - inner.x1) * PT_TO_MM, 2)
        bleed_top = round((outer.y1 - inner.y1) * PT_TO_MM, 2)

        bleed_dict = {
            "left": bleed_left,
            "right": bleed_right,
            "top": bleed_top,
            "bottom": bleed_bottom,
        }

        if not bleed_check_enabled:
            # bleed_mm == 0: only report boxes/format, status always PASS
            status = RuleStatus.PASS
            msg = (
                f"Bleed-Check deaktiviert. "
                f"L={bleed_left} R={bleed_right} T={bleed_top} B={bleed_bottom}"
            )
        else:
            min_actual = min(bleed_dict.values())
            if min_actual < min_bleed:
                status = RuleStatus.FAIL
                msg = (
                    f"Bleed unzureichend: min={min_actual:.1f} mm "
                    f"(Soll {min_bleed:.1f} mm). "
                    f"L={bleed_left} R={bleed_right} T={bleed_top} B={bleed_bottom}"
                )
            else:
                status = RuleStatus.PASS
                msg = f"Bleed OK (min {min_actual:.1f} mm >= {min_bleed:.1f} mm)"

        results.append(PageSizeResult(
            page=page_num + 1,
            boxes=boxes,
            trim_width_mm=trim_w,
            trim_height_mm=trim_h,
            bleed_mm=bleed_dict,
            bleed_status=status,
            bleed_message=msg,
        ))

    return results
