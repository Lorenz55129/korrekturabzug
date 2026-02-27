"""Rule: Effective DPI calculation for raster images."""

from __future__ import annotations

from typing import Any

import fitz  # PyMuPDF

from app.models import ImageDPIResult, RuleStatus

PT_TO_INCH = 1.0 / 72.0
PT_TO_MM = 25.4 / 72.0


def compute_effective_dpi(
    pixel_width: int,
    pixel_height: int,
    placed_width_pt: float,
    placed_height_pt: float,
) -> tuple[float, float]:
    """Return (dpi_x, dpi_y) for a placed raster image."""
    if placed_width_pt <= 0 or placed_height_pt <= 0:
        return (0.0, 0.0)
    dpi_x = pixel_width / (placed_width_pt * PT_TO_INCH)
    dpi_y = pixel_height / (placed_height_pt * PT_TO_INCH)
    return (round(dpi_x, 1), round(dpi_y, 1))


def check_image_dpi(doc: fitz.Document, cfg: dict[str, Any]) -> tuple[list[ImageDPIResult], int]:
    """Return (all_images_list, total_instance_count).

    Each placement of an image on a page is one *instance*.  A single xref
    placed three times produces three entries.  ``total_instance_count``
    equals ``len(all_images_list)`` so that Summary counters are always
    consistent (FAIL ≤ total).
    """
    min_dpi = cfg.get("image_resolution", {}).get("min_dpi", 72)
    all_images: list[ImageDPIResult] = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        image_list = page.get_images(full=True)

        for img_idx, img_info in enumerate(image_list):
            xref = img_info[0]

            try:
                pix = fitz.Pixmap(doc, xref)
                pixel_w = pix.width
                pixel_h = pix.height
                pix = None  # free memory
            except Exception:
                # Inline image or unreadable – skip gracefully
                continue

            # Find placement rectangle(s) for this image on the page
            img_rects = page.get_image_rects(xref)
            if not img_rects:
                continue

            for rect in img_rects:
                placed_w = abs(rect.width)
                placed_h = abs(rect.height)
                dpi_x, dpi_y = compute_effective_dpi(pixel_w, pixel_h, placed_w, placed_h)
                eff_dpi = min(dpi_x, dpi_y)

                if eff_dpi < min_dpi:
                    status = RuleStatus.FAIL
                    msg = f"Image DPI {eff_dpi:.0f} < {min_dpi} (required)"
                else:
                    status = RuleStatus.PASS
                    msg = ""

                all_images.append(ImageDPIResult(
                    page=page_num + 1,
                    image_index=img_idx,
                    x_pt=round(rect.x0, 2),
                    y_pt=round(rect.y0, 2),
                    width_pt=round(placed_w, 2),
                    height_pt=round(placed_h, 2),
                    width_mm=round(placed_w * PT_TO_MM, 1),
                    height_mm=round(placed_h * PT_TO_MM, 1),
                    pixel_width=pixel_w,
                    pixel_height=pixel_h,
                    effective_dpi_x=dpi_x,
                    effective_dpi_y=dpi_y,
                    effective_dpi=round(eff_dpi, 1),
                    min_dpi=min_dpi,
                    status=status,
                    message=msg,
                ))

    # total = instances (= len(all_images)), guarantees FAIL ≤ total
    return all_images, len(all_images)
