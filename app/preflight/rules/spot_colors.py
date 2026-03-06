"""Rules: Cut-contour and die/Stanze detection via spot colours and layers."""

from __future__ import annotations

import logging
import re
from typing import Any

import fitz  # PyMuPDF
import pikepdf

from app.models import CutContourResult, DieResult, RuleStatus, SpotColorInfo

logger = logging.getLogger(__name__)

_SEPARATOR_RE = re.compile(r"[\s_\-]+")


def _normalize_name(name: str) -> str:
    """Strip, uppercase, and remove separators (space, _, -)."""
    return _SEPARATOR_RE.sub("", name.strip()).upper()


def _name_matches(name: str, allowed: list[str]) -> bool:
    """Case-insensitive, separator-tolerant match against allowed names list.

    Matches after stripping whitespace/underscores/hyphens, so
    "CutKontur", "CUT_KONTUR", "cut-kontur" all match "CUTKONTUR".
    Also tries exact (stripped+upper) match for backward compatibility.
    """
    stripped_upper = name.strip().upper()
    normalized = _normalize_name(name)
    allowed_exact = set()
    allowed_normalized = set()
    for n in allowed:
        allowed_exact.add(n.strip().upper())
        allowed_normalized.add(_normalize_name(n))
    return stripped_upper in allowed_exact or normalized in allowed_normalized


_MAX_XOBJECT_RECURSION = 5


def _extract_spot_colors_pikepdf(pdf_path: str) -> dict[str, dict[str, Any]]:
    """Use pikepdf to walk /ColorSpace dicts and find Separation colours.

    Recursively traverses Form XObject resources to find Separations
    that are only defined inside XObjects (not directly on the page).

    Returns {spot_name_upper: {"original_name": ..., "cmyk": [...] or None}}.
    """
    spots: dict[str, dict[str, Any]] = {}

    with pikepdf.open(pdf_path) as pdf:
        for page in pdf.pages:
            resources = page.get("/Resources")
            if resources is None:
                continue
            visited: set[str] = set()
            _collect_spots_from_resources(resources, spots, visited, depth=0)

    return spots


def _xobj_key(name: str, xobj: Any) -> str:
    """Build a stable key for a resolved XObject to prevent revisiting."""
    try:
        og = xobj.objgen
        if og != (0, 0):
            return f"xref:{og[0]}:{og[1]}"
    except Exception:
        pass
    return f"name:{name}"


_MAX_XOBJECTS_TOTAL = 200  # hard cap to prevent runaway on huge PDFs


def _collect_spots_from_resources(
    resources: Any,
    spots: dict[str, dict[str, Any]],
    visited: set[str],
    depth: int,
) -> None:
    """Collect Separation spot colours from a Resources dict, recursing into Form XObjects."""
    if depth > _MAX_XOBJECT_RECURSION:
        return

    # 1) Scan /ColorSpace for Separations
    cs_dict = resources.get("/ColorSpace") if hasattr(resources, "get") else None
    if cs_dict is not None:
        if hasattr(cs_dict, "items"):
            for _alias, cs_obj in cs_dict.items():
                try:
                    if isinstance(cs_obj, pikepdf.Array):
                        cs = cs_obj
                    elif hasattr(cs_obj, "resolve"):
                        cs = cs_obj.resolve()
                    else:
                        cs = cs_obj
                    if not isinstance(cs, pikepdf.Array) or len(cs) < 2:
                        continue
                    if str(cs[0]) != "/Separation":
                        continue
                    spot_name = str(cs[1]).lstrip("/")
                    cmyk = _try_extract_cmyk(cs)
                    spots[spot_name.upper()] = {
                        "original_name": spot_name,
                        "cmyk": cmyk,
                    }
                except Exception as exc:
                    logger.debug("Could not parse ColorSpace entry: %s", exc)

    # 2) Recurse into Form XObjects
    xobject_dict = resources.get("/XObject") if hasattr(resources, "get") else None
    if xobject_dict is None:
        return
    if not hasattr(xobject_dict, "items"):
        return
    for xobj_name, xobj_ref in xobject_dict.items():
        if len(visited) >= _MAX_XOBJECTS_TOTAL:
            return
        try:
            xobj = xobj_ref.resolve() if hasattr(xobj_ref, "resolve") else xobj_ref
            key = _xobj_key(str(xobj_name), xobj)
            if key in visited:
                continue
            visited.add(key)
            # Only recurse into Form XObjects
            subtype = xobj.get("/Subtype") if hasattr(xobj, "get") else None
            if subtype is None or str(subtype) != "/Form":
                continue
            sub_resources = xobj.get("/Resources")
            if sub_resources is not None:
                _collect_spots_from_resources(
                    sub_resources, spots, visited, depth + 1,
                )
        except Exception as exc:
            logger.debug("Could not recurse into XObject: %s", exc)


def _try_extract_cmyk(cs_array: pikepdf.Array) -> list[float] | None:
    """Attempt to extract CMYK values from the alternate colour space or tint transform."""
    try:
        # Separation array: [/Separation /Name /AlternateCS tintTransform]
        if len(cs_array) < 4:
            return None
        alt_cs = cs_array[2]
        if hasattr(alt_cs, "resolve"):
            alt_cs = alt_cs.resolve()
        alt_name = str(alt_cs) if not isinstance(alt_cs, pikepdf.Array) else str(alt_cs[0])
        if alt_name != "/DeviceCMYK":
            return None
        # The tint transform is a function; we can't easily evaluate it.
        # A common shortcut: if it's a /FunctionType 2 (exponential), the C1 array is the colour.
        tint_fn = cs_array[3]
        if hasattr(tint_fn, "resolve"):
            tint_fn = tint_fn.resolve()
        if isinstance(tint_fn, pikepdf.Dictionary):
            c1 = tint_fn.get("/C1")
            if c1 is not None:
                vals = [float(v) for v in c1]
                if len(vals) == 4:
                    return [round(v * 100, 1) for v in vals]  # normalise to 0-100
        return None
    except Exception:
        return None


def _is_magenta(cmyk: list[float] | None, expected: list[float]) -> bool | None:
    """Check if the CMYK values match expected Magenta. None = indeterminate."""
    if cmyk is None:
        return None
    tolerance = 5.0  # allow small rounding
    for actual, exp in zip(cmyk, expected):
        if abs(actual - exp) > tolerance:
            return False
    return True


def _find_spot_on_pages_fitz(doc: fitz.Document, spot_name_upper: str) -> list[int]:
    """Heuristic: scan each page's /Resources for the spot colour to know which pages use it."""
    pages: list[int] = []
    for i in range(len(doc)):
        page = doc[i]
        # Quick text-based scan of the page's xref stream for the spot name
        # This is an approximation; full content-stream parsing would be heavier.
        try:
            resources = page.get_text("rawdict", flags=0)  # lightweight
        except Exception:
            pass
        # Alternative: check if the spot name appears in the page resources
        xref = page.xref
        try:
            raw = doc.xref_object(xref)
            if spot_name_upper.lower() in raw.lower() or spot_name_upper in raw:
                pages.append(i + 1)
        except Exception:
            pass
    return pages


def _is_true_overprint(val: Any) -> bool:
    """Check whether a pikepdf value represents boolean True."""
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    return str(val).lower() == "true"


def _check_path_geometry(
    doc: fitz.Document,
    pages: list[int],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Validate cut contour path geometry using PyMuPDF get_drawings().

    Identifies Magenta paths via RGB heuristic (CMYK 0,100,0,0 → RGB 1,0,1).
    Returns dict with: stroke_width_pt, is_unfilled, is_closed, stroke_ok, paths_found.
    """
    max_stroke_pt = cfg.get("cut_contour", {}).get("max_stroke_width_pt", 0.25)

    all_widths: list[float] = []
    any_filled = False
    any_unclosed = False

    for page_num in pages:
        page = doc[page_num - 1]  # pages are 1-indexed
        for drawing in page.get_drawings():
            color = drawing.get("color")
            if not color:
                continue
            r, g, b = color[0], color[1], color[2]
            # Magenta heuristic: CMYK(0,100,0,0) → RGB(1,0,1)
            if not (r > 0.8 and g < 0.2 and b > 0.8):
                continue

            width = drawing.get("width") or 0.0
            fill = drawing.get("fill")
            close_path = drawing.get("closePath", False)

            all_widths.append(width)
            if fill is not None:
                any_filled = True
            if not close_path:
                any_unclosed = True

    if not all_widths:
        return {
            "stroke_width_pt": None,
            "is_unfilled": None,
            "is_closed": None,
            "stroke_ok": None,
            "paths_found": False,
        }

    max_width = max(all_widths)
    return {
        "stroke_width_pt": round(max_width, 3),
        "is_unfilled": not any_filled,
        "is_closed": not any_unclosed,
        "stroke_ok": max_width <= max_stroke_pt,
        "paths_found": True,
    }


def _check_overprint_cutcontour(pdf_path: str, pages: list[int]) -> bool | None:
    """Check if overprint (/OP or /op) is set in ExtGState on cut-contour pages.

    Returns True if overprint found, False if absent, None if check failed.
    """
    try:
        with pikepdf.open(pdf_path) as pdf:
            for page_num in pages:
                page = pdf.pages[page_num - 1]
                resources = page.get("/Resources")
                if resources is None:
                    continue
                ext_gstate = resources.get("/ExtGState")
                if ext_gstate is None or not hasattr(ext_gstate, "items"):
                    continue
                for _name, gs_ref in ext_gstate.items():
                    try:
                        gs = gs_ref.resolve() if hasattr(gs_ref, "resolve") else gs_ref
                        op_stroke = gs.get("/OP")
                        op_fill = gs.get("/op")
                        if _is_true_overprint(op_stroke) or _is_true_overprint(op_fill):
                            return True
                    except Exception:
                        continue
        return False
    except Exception as exc:
        logger.debug("Overprint check failed for cut contour: %s", exc)
        return None


def check_cut_contour(
    pdf_path: str,
    doc: fitz.Document,
    cfg: dict[str, Any],
) -> CutContourResult:
    cc_cfg = cfg.get("cut_contour", {})
    allowed = cc_cfg.get("allowed_names", [])
    expected_cmyk = cc_cfg.get("expected_cmyk", [0, 100, 0, 0])
    max_stroke_pt = cc_cfg.get("max_stroke_width_pt", 0.25)

    spots = _extract_spot_colors_pikepdf(pdf_path)
    messages: list[str] = []

    # Find matching spot colour
    matched_name: str | None = None
    matched_info: dict[str, Any] | None = None

    for upper_name, info in spots.items():
        if _name_matches(upper_name, allowed):
            matched_name = upper_name
            matched_info = info
            break

    if matched_name is None:
        return CutContourResult(
            found=False,
            status=RuleStatus.FAIL,
            messages=["No cut-contour spot colour found. "
                      f"Expected one of: {allowed}"],
        )

    cmyk = matched_info["cmyk"] if matched_info else None
    is_mag = _is_magenta(cmyk, expected_cmyk)

    spot_info = SpotColorInfo(
        name=matched_info["original_name"] if matched_info else matched_name,
        cmyk=cmyk,
        is_expected_magenta=is_mag,
    )

    if is_mag is None:
        messages.append(
            "WARN: Could not determine alternate CMYK for cut-contour spot colour. "
            "Cannot verify Magenta."
        )
        status = RuleStatus.WARN
    elif not is_mag:
        messages.append(
            f"WARN: Cut-contour spot colour CMYK {cmyk} does not match "
            f"expected Magenta {expected_cmyk}."
        )
        status = RuleStatus.WARN
    else:
        messages.append("Cut-contour spot colour is Magenta – OK.")
        status = RuleStatus.PASS

    pages = _find_spot_on_pages_fitz(doc, matched_name)
    if not pages:
        pages = list(range(1, len(doc) + 1))  # fallback: assume all

    # Path geometry validation
    geom = _check_path_geometry(doc, pages, cfg)
    stroke_width_pt = geom["stroke_width_pt"]
    is_unfilled = geom["is_unfilled"]
    is_closed = geom["is_closed"]

    if geom["paths_found"]:
        if geom["stroke_ok"] is False:
            messages.append(
                f"Strichstärke {stroke_width_pt:.3f} pt > {max_stroke_pt} pt "
                f"(erwartet ≤ {max_stroke_pt} pt)"
            )
            status = RuleStatus.WARN
        else:
            messages.append(f"Strichstärke {stroke_width_pt:.3f} pt – OK")

        if is_unfilled is False:
            messages.append("Cutcontour-Pfad hat Füllung (muss ungefüllt sein)")
            status = RuleStatus.WARN
        else:
            messages.append("Pfad ungefüllt – OK")

        if is_closed is False:
            messages.append("Cutcontour-Pfad nicht geschlossen")
            status = RuleStatus.WARN
        else:
            messages.append("Pfad geschlossen – OK")
    else:
        messages.append(
            "Pfad-Geometrie konnte nicht geprüft werden (keine Magenta-Pfade erkannt)"
        )

    # Overprint check
    is_overprint = _check_overprint_cutcontour(pdf_path, pages)
    if is_overprint is None:
        messages.append("Überdrucken konnte nicht geprüft werden")
    elif not is_overprint:
        messages.append("Überdrucken (Overprint) nicht gesetzt")
        status = RuleStatus.WARN
    else:
        messages.append("Überdrucken gesetzt – OK")

    return CutContourResult(
        found=True,
        spot_color=spot_info,
        pages=pages,
        stroke_width_pt=stroke_width_pt,
        is_unfilled=is_unfilled,
        is_closed=is_closed,
        is_overprint=is_overprint,
        status=status,
        messages=messages,
    )


def check_die(
    pdf_path: str,
    doc: fitz.Document,
    cfg: dict[str, Any],
) -> DieResult:
    die_cfg = cfg.get("die", {})
    allowed = die_cfg.get("allowed_names", [])

    spots = _extract_spot_colors_pikepdf(pdf_path)
    messages: list[str] = []

    matched_name: str | None = None
    for upper_name, info in spots.items():
        if _name_matches(upper_name, allowed):
            matched_name = info.get("original_name", upper_name)
            break

    # Also check OCG (optional content groups / layers)
    layer_match: str | None = None
    try:
        with pikepdf.open(pdf_path) as pdf:
            ocprops = pdf.Root.get("/OCProperties")
            if ocprops:
                ocgs = ocprops.get("/OCGs", [])
                for ocg in ocgs:
                    resolved = ocg.resolve() if hasattr(ocg, "resolve") else ocg
                    name = str(resolved.get("/Name", "")).strip()
                    if _name_matches(name, allowed):
                        layer_match = name
                        break
    except Exception as exc:
        logger.debug("OCG check failed: %s", exc)

    found = matched_name is not None or layer_match is not None
    final_name = matched_name or layer_match

    if not found:
        return DieResult(
            found=False,
            status=RuleStatus.FAIL,
            messages=[f"No die/Stanze separation or layer found. Expected one of: {allowed}"],
        )

    pages: list[int] = []
    if matched_name:
        pages = _find_spot_on_pages_fitz(doc, matched_name.upper())
    if not pages:
        pages = list(range(1, len(doc) + 1))

    messages.append(f"Die/Stanze found: '{final_name}'.")
    return DieResult(
        found=True,
        name_matched=final_name,
        pages=pages,
        status=RuleStatus.PASS,
        messages=messages,
    )
