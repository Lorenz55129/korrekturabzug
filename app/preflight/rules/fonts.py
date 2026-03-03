"""Rule: Font embedding check – not-embedded → FAIL, Type3 → WARN."""

from __future__ import annotations

import logging
import re
from typing import Any

import fitz  # PyMuPDF

from app.models import FontCheckResult, FontInfo, RuleStatus

logger = logging.getLogger(__name__)

# Subset-Präfix: 6 Großbuchstaben gefolgt von "+"  (z.B. "ABCDEF+Helvetica")
_SUBSET_RE = re.compile(r"^[A-Z]{6}\+(.+)$")


def _parse_font_name(raw_name: str) -> tuple[str, bool]:
    """Gibt (sauberer Name, is_subset) zurück.

    Subset-Schriften (Schriftengruppen) haben im PDF einen Präfix
    aus 6 Großbuchstaben + '+', z.B. 'ABCDEF+HelveticaNeue-Light'.
    """
    m = _SUBSET_RE.match(raw_name or "")
    if m:
        return m.group(1), True
    return raw_name, False


def check_fonts(doc: fitz.Document, cfg: dict[str, Any]) -> FontCheckResult:
    """Analysiert alle Schriften in *doc*.

    Regeln:
    - Nicht eingebettet        → FAIL
    - Schriftengruppe (Subset) → PASS  (nur verwendete Zeichen eingebettet)
    - Type3-Schrift            → WARN
    - Sonst vollständig        → PASS
    """
    fonts_found: list[FontInfo] = []
    seen: set[tuple[int, str]] = set()  # (page, font_name) dedup

    for page_num in range(len(doc)):
        page = doc[page_num]
        font_list = page.get_fonts(full=True)

        for font_info in font_list:
            # font_info: (xref, ext, type, basefont, name, encoding, ...)
            xref      = font_info[0]
            ext       = font_info[1]  # leer wenn nicht eingebettet
            font_type = font_info[2]  # "Type1", "TrueType", "Type3", "CIDFontType0", …
            base_font = font_info[3]
            raw_name  = font_info[4] or base_font or f"xref-{xref}"

            # Sauberen Namen + Subset-Erkennung aus Rohname
            font_name, is_subset = _parse_font_name(raw_name)
            # Fallback: Subset-Präfix auch im base_font suchen
            if not is_subset:
                _, is_subset = _parse_font_name(base_font or "")

            # Deduplizierung pro Seite
            key = (page_num, font_name)
            if key in seen:
                continue
            seen.add(key)

            is_type3 = font_type.lower() == "type3" if font_type else False

            # Einbettungs-Check über das ext-Feld (leer = nicht eingebettet)
            # Type3 ist per Definition immer eingebettet
            is_embedded = bool(ext) or is_type3

            # Fallback für CIDFonts, bei denen ext leer sein kann
            if not is_embedded and xref:
                is_embedded = _check_embedding_via_xref(doc, xref)

            status = RuleStatus.PASS
            if not is_embedded:
                status = RuleStatus.FAIL
                message = f"Schrift nicht eingebettet: {font_name}"
            elif is_type3:
                status = RuleStatus.WARN
                message = f"Type3-Schrift: {font_name}"
            elif is_subset:
                message = f"Schriftengruppe eingebettet: {font_name}"
            else:
                message = f"Vollständig eingebettet: {font_name}"

            fonts_found.append(FontInfo(
                name=font_name,
                page=page_num + 1,
                is_embedded=is_embedded,
                is_subset=is_subset,
                is_type3=is_type3,
                status=status,
                message=message,
            ))

    not_embedded  = [f for f in fonts_found if not f.is_embedded]
    subset_fonts  = [f for f in fonts_found if f.is_subset and f.is_embedded]
    type3_fonts   = [f for f in fonts_found if f.is_type3]

    # Gesamtstatus
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
    if subset_fonts and not not_embedded:
        names = sorted(set(f.name for f in subset_fonts))
        messages.append(f"{len(subset_fonts)} Schriftengruppe(n) eingebettet: {', '.join(names)}")
    if not messages:
        messages.append("Alle Schriften vollständig eingebettet.")

    return FontCheckResult(
        fonts=fonts_found,
        total_fonts=len(fonts_found),
        not_embedded_count=len(not_embedded),
        subset_count=len(subset_fonts),
        type3_count=len(type3_fonts),
        status=agg_status,
        messages=messages,
    )


def _check_embedding_via_xref(doc: fitz.Document, xref: int) -> bool:
    """Fallback: FontFile-Eintrag im PDF-Objekt suchen (für CIDFonts)."""
    try:
        font_dict_str = doc.xref_object(xref)
        if any(k in font_dict_str for k in ("/FontFile", "/FontFile2", "/FontFile3")):
            return True
        # CIDFont: FontDescriptor sitzt im DescendantFont
        if "/DescendantFonts" in font_dict_str:
            for sub_xref in range(1, doc.xref_length()):
                try:
                    sub_str = doc.xref_object(sub_xref)
                    if "/FontDescriptor" in sub_str and any(
                        k in sub_str for k in ("/FontFile", "/FontFile2", "/FontFile3")
                    ):
                        return True
                except Exception:
                    continue
    except Exception:
        logger.debug("Konnte Font-xref %d nicht prüfen", xref)
    return False
