"""Rule: Font embedding check – not-embedded → FAIL, Type3 → WARN."""

from __future__ import annotations

import logging
from typing import Any

import fitz  # PyMuPDF

from app.models import FontCheckResult, FontInfo, RuleStatus

logger = logging.getLogger(__name__)


def check_fonts(doc: fitz.Document, cfg: dict[str, Any]) -> FontCheckResult:
    """Analyse all fonts in *doc*.

    Rules:
    - Not embedded → FAIL
    - Type3 font  → WARN
    - Otherwise   → PASS

    Returns a FontCheckResult with per-font details and aggregate status.
    """
    fonts_found: list[FontInfo] = []
    seen: set[tuple[int, str]] = set()  # (page, font_name) dedup

    for page_num in range(len(doc)):
        page = doc[page_num]
        font_list = page.get_fonts(full=True)

        for font_info in font_list:
            # font_info: (xref, ext, type, basefont, name, encoding, ...)
            xref = font_info[0]
            font_type = font_info[2]  # e.g. "Type1", "TrueType", "Type3", "CIDFontType0", ...
            base_font = font_info[3]  # base font name
            font_name = font_info[4] or base_font or f"xref-{xref}"
            encoding = font_info[5] if len(font_info) > 5 else ""

            # Deduplicate per page
            key = (page_num, font_name)
            if key in seen:
                continue
            seen.add(key)

            is_type3 = font_type.lower() == "type3" if font_type else False

            # Embedding heuristic: PyMuPDF's get_fonts returns info about
            # whether the font has an embedded stream.
            # A font is considered NOT embedded when:
            #   - xref == 0 (no object in the file)
            #   - or the font dict has no embedded stream
            # PyMuPDF marks non-embedded fonts with empty ext field and
            # encoding hints. We check more robustly via the xref.
            is_embedded = _is_font_embedded(doc, xref, font_type)

            status = RuleStatus.PASS
            message = ""

            if not is_embedded:
                status = RuleStatus.FAIL
                message = f"Schrift nicht eingebettet: {font_name}"
            elif is_type3:
                status = RuleStatus.WARN
                message = f"Type3-Schrift: {font_name}"

            fonts_found.append(FontInfo(
                name=font_name,
                page=page_num + 1,
                is_embedded=is_embedded,
                is_type3=is_type3,
                status=status,
                message=message,
            ))

    not_embedded = [f for f in fonts_found if not f.is_embedded]
    type3_fonts = [f for f in fonts_found if f.is_type3]

    # Aggregate status
    if not_embedded:
        agg_status = RuleStatus.FAIL
    elif type3_fonts:
        agg_status = RuleStatus.WARN
    else:
        agg_status = RuleStatus.PASS

    messages: list[str] = []
    if not_embedded:
        names = sorted(set(f.name for f in not_embedded))
        messages.append(f"{len(not_embedded)} Schrift(en) nicht eingebettet: {', '.join(names)}")
    if type3_fonts:
        names = sorted(set(f.name for f in type3_fonts))
        messages.append(f"{len(type3_fonts)} Type3-Schrift(en): {', '.join(names)}")
    if not messages:
        messages.append("Alle Schriften eingebettet.")

    return FontCheckResult(
        fonts=fonts_found,
        total_fonts=len(fonts_found),
        not_embedded_count=len(not_embedded),
        type3_count=len(type3_fonts),
        status=agg_status,
        messages=messages,
    )


def _is_font_embedded(doc: fitz.Document, xref: int, font_type: str | None) -> bool:
    """Check if a font with the given xref is embedded in the PDF.

    A font is embedded if its font descriptor contains a FontFile,
    FontFile2, or FontFile3 stream reference, OR if the font itself
    is a Type3 font (which by definition has inline glyph descriptions).
    """
    if xref == 0:
        return False

    # Type3 fonts are always "embedded" by definition (glyphs are inline)
    if font_type and font_type.lower() == "type3":
        return True

    try:
        font_dict_str = doc.xref_object(xref)
        # Check for FontDescriptor with FontFile/FontFile2/FontFile3
        if "/FontFile" in font_dict_str:
            return True
        if "/FontFile2" in font_dict_str:
            return True
        if "/FontFile3" in font_dict_str:
            return True

        # For CIDFonts, the descendant font may hold the descriptor
        if "/DescendantFonts" in font_dict_str:
            # Try to follow the reference
            try:
                for sub_xref in range(1, doc.xref_length()):
                    sub_str = doc.xref_object(sub_xref)
                    if "/FontDescriptor" in sub_str and "/FontFile" in sub_str:
                        # Check if this descriptor belongs to our font
                        if str(xref) in sub_str or any(
                            key in sub_str for key in ["/FontFile", "/FontFile2", "/FontFile3"]
                        ):
                            return True
            except Exception:
                pass
            # CIDFont without FontFile check: look at DescendantFont's descriptor
            return False

        # If we find a FontDescriptor reference, follow it
        if "/FontDescriptor" in font_dict_str:
            # The FontDescriptor itself doesn't contain FontFile –
            # the font is likely not embedded
            return False

        # No descriptor at all – standard 14 fonts are not embedded but OK
        # Actually, if there's no descriptor and no FontFile, it's not embedded.
        # Standard base-14 fonts (Helvetica, Times, Courier, etc.) are allowed
        # without embedding per PDF spec. However, for prepress purposes,
        # we still flag them as not-embedded.
        return False

    except Exception:
        logger.debug("Could not inspect font xref %d", xref)
        return False
