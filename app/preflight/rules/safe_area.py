"""Rule: Safe-area (Sicherheitsabstand) check – max WARN, never FAIL."""

from __future__ import annotations

import logging
from typing import Any

import fitz  # PyMuPDF

from app.models import RuleStatus, SafeAreaResult, SafeAreaViolation
from app.preflight.rules._reference_box import PT_TO_MM, get_reference_box

logger = logging.getLogger(__name__)


def check_safe_area(
    doc: fitz.Document,
    cfg: dict[str, Any],
    safe_margin_mm: float = 5.0,
) -> SafeAreaResult:
    """Check that text (and heuristically vectors) stay inside the safe area.

    SafeAreaBox = reference box shrunk by *safe_margin_mm* on every side.
    Status is **max WARN** – never FAIL.
    """
    margin_pt = safe_margin_mm / PT_TO_MM  # mm → pt
    violations: list[SafeAreaViolation] = []
    detection_limited = False
    messages: list[str] = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        ref_box, _box_name = get_reference_box(page)

        safe = fitz.Rect(
            ref_box.x0 + margin_pt,
            ref_box.y0 + margin_pt,
            ref_box.x1 - margin_pt,
            ref_box.y1 - margin_pt,
        )

        # ── Text blocks ─────────────────────────────────
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        blocks = text_dict.get("blocks", [])
        text_blocks = [b for b in blocks if b.get("type") == 0]  # type 0 = text

        for tb in text_blocks:
            bbox = fitz.Rect(tb["bbox"])
            overflow = _compute_overflow_pt(bbox, safe)
            if overflow > 0:
                violations.append(SafeAreaViolation(
                    page=page_num + 1,
                    description="Textblock",
                    bbox_mm=[
                        round(bbox.x0 * PT_TO_MM, 1),
                        round(bbox.y0 * PT_TO_MM, 1),
                        round(bbox.x1 * PT_TO_MM, 1),
                        round(bbox.y1 * PT_TO_MM, 1),
                    ],
                    overflow_mm=round(overflow * PT_TO_MM, 2),
                    status=RuleStatus.WARN,
                ))

        # ── Drawings (vector heuristic) ─────────────────
        try:
            drawings = page.get_drawings()
        except Exception:
            drawings = []

        for drw in drawings:
            drw_rect = fitz.Rect(drw.get("rect", (0, 0, 0, 0)))
            if drw_rect.is_empty or drw_rect.is_infinite:
                continue
            overflow = _compute_overflow_pt(drw_rect, safe)
            if overflow > 0:
                violations.append(SafeAreaViolation(
                    page=page_num + 1,
                    description="Zeichnungsobjekt",
                    bbox_mm=[
                        round(drw_rect.x0 * PT_TO_MM, 1),
                        round(drw_rect.y0 * PT_TO_MM, 1),
                        round(drw_rect.x1 * PT_TO_MM, 1),
                        round(drw_rect.y1 * PT_TO_MM, 1),
                    ],
                    overflow_mm=round(overflow * PT_TO_MM, 2),
                    status=RuleStatus.WARN,
                ))

        # ── detection_limited logic ─────────────────────
        # Only flag if page has content but zero text blocks.
        page_has_content = bool(drawings) or _page_has_xobjects_or_images(page)
        if page_has_content and not text_blocks:
            detection_limited = True

    if detection_limited:
        messages.append(
            "Safe-Area-Prüfung eingeschränkt: "
            "Nicht alle Seiten enthalten auswertbare Textblöcke."
        )

    status = RuleStatus.PASS
    if violations or detection_limited:
        status = RuleStatus.WARN
        if violations:
            messages.insert(0,
                f"{len(violations)} Objekt(e) außerhalb Sicherheitsabstand "
                f"({safe_margin_mm:.1f} mm)"
            )

    return SafeAreaResult(
        safe_margin_mm=safe_margin_mm,
        violations=violations,
        detection_limited=detection_limited,
        status=status,
        messages=messages,
    )


# ── Helpers ──────────────────────────────────────────────

def _compute_overflow_pt(bbox: fitz.Rect, safe: fitz.Rect) -> float:
    """Return max overflow in pt (0 if fully inside)."""
    overflows = [
        safe.x0 - bbox.x0,   # left overflow
        safe.y0 - bbox.y0,   # top overflow
        bbox.x1 - safe.x1,   # right overflow
        bbox.y1 - safe.y1,   # bottom overflow
    ]
    return max(0, max(overflows))


def _page_has_xobjects_or_images(page: fitz.Page) -> bool:
    """Quick check whether page has XObjects or images."""
    try:
        images = page.get_images(full=False)
        if images:
            return True
    except Exception:
        pass
    try:
        # Check for Form XObjects in resources
        xref = page.xref
        raw = page.parent.xref_object(xref)
        if "/XObject" in raw:
            return True
    except Exception:
        pass
    return False
