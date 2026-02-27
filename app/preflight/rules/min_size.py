"""Rule: Minimum page size check (all pages)."""

from __future__ import annotations

from typing import Any

import fitz  # PyMuPDF

from app.models import MinSizeResult, RuleStatus
from app.preflight.rules._reference_box import PT_TO_MM, get_reference_box


def check_min_size(
    doc: fitz.Document,
    cfg: dict[str, Any],
    min_w_mm: float = 100.0,
    min_h_mm: float = 100.0,
) -> MinSizeResult:
    """Check that every page meets the minimum dimension requirement.

    FAIL if **any** page's reference box (TrimBox → CropBox → MediaBox)
    is smaller than *min_w_mm* × *min_h_mm*.
    """
    pages_failed: list[int] = []
    pages_detail: list[dict] = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        ref_box, box_name = get_reference_box(page)
        w_mm = round(ref_box.width * PT_TO_MM, 2)
        h_mm = round(ref_box.height * PT_TO_MM, 2)
        pages_detail.append({
            "page": page_num + 1,
            "w_mm": w_mm,
            "h_mm": h_mm,
            "box": box_name,
        })
        if w_mm < min_w_mm or h_mm < min_h_mm:
            pages_failed.append(page_num + 1)

    if pages_failed:
        msg = (
            f"Seite(n) {', '.join(str(p) for p in pages_failed)}: "
            f"Endformat unter Minimum {min_w_mm:.0f} × {min_h_mm:.0f} mm"
        )
        return MinSizeResult(
            min_width_mm=min_w_mm,
            min_height_mm=min_h_mm,
            pages_failed=pages_failed,
            pages_detail=pages_detail,
            status=RuleStatus.FAIL,
            message=msg,
        )

    return MinSizeResult(
        min_width_mm=min_w_mm,
        min_height_mm=min_h_mm,
        pages_failed=[],
        pages_detail=pages_detail,
        status=RuleStatus.PASS,
        message=f"Alle Seiten >= {min_w_mm:.0f} × {min_h_mm:.0f} mm",
    )
