"""Shared utility: canonical reference box for a PDF page."""

from __future__ import annotations

import fitz  # PyMuPDF

PT_TO_MM = 25.4 / 72.0


def get_reference_box(page: fitz.Page) -> tuple[fitz.Rect, str]:
    """Return the canonical reference box for *page*.

    Priority: TrimBox → CropBox → MediaBox.
    Returns ``(rect, box_name)`` where *box_name* is one of
    ``"TrimBox"``, ``"CropBox"``, or ``"MediaBox"``.
    """
    mediabox = page.mediabox
    trimbox = page.trimbox
    cropbox = page.cropbox

    # PyMuPDF returns mediabox as fallback when box is not explicitly set.
    has_trimbox = trimbox != mediabox
    has_cropbox = cropbox != mediabox

    if has_trimbox:
        return trimbox, "TrimBox"
    if has_cropbox:
        return cropbox, "CropBox"
    return mediabox, "MediaBox"
