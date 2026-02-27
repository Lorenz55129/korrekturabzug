"""Rule: Detect DeviceRGB content in the PDF → WARN."""

from __future__ import annotations

import logging
from typing import Any

import fitz  # PyMuPDF

from app.models import RGBCheckResult, RuleStatus

logger = logging.getLogger(__name__)


def check_rgb(doc: fitz.Document, cfg: dict[str, Any]) -> RGBCheckResult:
    """Scan all pages for DeviceRGB and DeviceGray colour space usage.

    Checks:
    - Page resources for /DeviceRGB and /DeviceGray colour spaces
    - Images using DeviceRGB or DeviceGray
    - If found → WARN (prepress should use CMYK)

    This is a lightweight scan; deep content-stream analysis is not performed.
    """
    pages_with_rgb: list[int] = []
    pages_with_gray: list[int] = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        has_rgb = False
        has_gray = False

        # Method 1: Check page xref for DeviceRGB / DeviceGray references
        try:
            xref = page.xref
            raw = doc.xref_object(xref)
            if "/DeviceRGB" in raw:
                has_rgb = True
            if "/DeviceGray" in raw:
                has_gray = True
        except Exception:
            pass

        # Method 2: Check images on the page
        if not has_rgb or not has_gray:
            try:
                image_list = page.get_images(full=True)
                for img_info in image_list:
                    img_xref = img_info[0]
                    try:
                        img_dict = doc.xref_object(img_xref)
                        if not has_rgb and "/DeviceRGB" in img_dict:
                            has_rgb = True
                        if not has_gray and "/DeviceGray" in img_dict:
                            has_gray = True
                        if has_rgb and has_gray:
                            break
                    except Exception:
                        pass
            except Exception:
                pass

        if has_rgb:
            pages_with_rgb.append(page_num + 1)
        if has_gray:
            pages_with_gray.append(page_num + 1)

    has_any = bool(pages_with_rgb) or bool(pages_with_gray)

    if has_any:
        parts: list[str] = []
        if pages_with_rgb:
            parts.append(
                f"DeviceRGB auf Seite(n): {', '.join(str(p) for p in pages_with_rgb)}"
            )
        if pages_with_gray:
            parts.append(
                f"DeviceGray auf Seite(n): {', '.join(str(p) for p in pages_with_gray)}"
            )
        parts.append("Für den Druck sollte CMYK verwendet werden.")
        return RGBCheckResult(
            has_device_rgb=bool(pages_with_rgb),
            has_device_gray=bool(pages_with_gray),
            pages_with_rgb=pages_with_rgb,
            pages_with_gray=pages_with_gray,
            status=RuleStatus.WARN,
            message=". ".join(parts),
        )

    return RGBCheckResult(
        has_device_rgb=False,
        has_device_gray=False,
        pages_with_rgb=[],
        pages_with_gray=[],
        status=RuleStatus.PASS,
        message="Kein DeviceRGB/DeviceGray gefunden.",
    )
