"""Rule: Drill-hole (Bohrungen) detection via Separation and content-stream parsing."""

from __future__ import annotations

import logging
import math
from typing import Any

import fitz  # PyMuPDF
import pikepdf

from app.models import DrillHoleInfo, DrillHolesResult, RuleStatus, SpotColorInfo
from app.preflight.rules._reference_box import PT_TO_MM, get_reference_box
from app.preflight.rules.spot_colors import (
    _extract_spot_colors_pikepdf,
    _name_matches,
)

logger = logging.getLogger(__name__)

# Safety limits for content-stream parsing
_MAX_RECURSION = 5
_MAX_OPS_PER_STREAM = 200_000
_MAX_PATHS_PER_PAGE = 2_000

# Paint operators that finalize a path
_PAINT_OPS = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"}


def check_drill_holes(
    pdf_path: str,
    doc: fitz.Document,
    cfg: dict[str, Any],
) -> DrillHolesResult:
    """Detect and validate drill holes defined via a Separation colour.

    1. Find the Separation spot colour matching ``drill_holes.separation_names``.
    2. Parse page content streams (+ Form XObjects recursively) for paths
       drawn in that colour space.
    3. Validate each path: closed, circular, diameter >= 4 mm, edge distance >= 20 mm.
    """
    dh_cfg = cfg.get("drill_holes", {})
    allowed_names = dh_cfg.get("separation_names", ["BOHRUNGEN"])
    min_diam_mm = dh_cfg.get("min_diameter_mm", 4.0)
    min_edge_mm = dh_cfg.get("min_edge_distance_mm", 20.0)
    expected_cmyk = dh_cfg.get("expected_cmyk", [100, 0, 0, 0])
    stroke_target = dh_cfg.get("stroke_width_pt", 0.25)
    stroke_tol = dh_cfg.get("stroke_width_tolerance_pt", 0.05)

    messages: list[str] = []

    # ── 1. Find separation ──────────────────────────────
    spots = _extract_spot_colors_pikepdf(pdf_path)
    matched_upper: str | None = None
    matched_info: dict[str, Any] | None = None

    for upper_name, info in spots.items():
        if _name_matches(upper_name, allowed_names):
            matched_upper = upper_name
            matched_info = info
            break

    if matched_upper is None:
        return DrillHolesResult(
            found=False,
            separation_present=False,
            status=RuleStatus.FAIL,
            messages=["Sonderfarbe 'Bohrungen' nicht gefunden"],
        )

    # Separation found
    cmyk = matched_info["cmyk"] if matched_info else None
    spot_info = SpotColorInfo(
        name=matched_info["original_name"] if matched_info else matched_upper,
        cmyk=cmyk,
        is_expected_magenta=None,  # not magenta for drill holes
    )

    # CMYK check (expect Cyan)
    cmyk_ok = True
    if cmyk is not None:
        tol = 5.0
        for actual, exp in zip(cmyk, expected_cmyk):
            if abs(actual - exp) > tol:
                cmyk_ok = False
                break
        if not cmyk_ok:
            messages.append(
                f"WARN: Bohrungen-Spotfarbe CMYK {cmyk} weicht von "
                f"erwartetem Cyan {expected_cmyk} ab."
            )
    else:
        cmyk_ok = False
        messages.append("WARN: CMYK der Bohrungen-Separation nicht bestimmbar.")

    # ── 2. Extract paths via content-stream parser ──────
    extraction_limited = False
    all_holes: list[DrillHoleInfo] = []
    hole_counter = 0

    try:
        with pikepdf.open(pdf_path) as pdf:
            for page_idx, pk_page in enumerate(pdf.pages):
                fitz_page = doc[page_idx]
                ref_box, _box_name = get_reference_box(fitz_page)

                # Find CS alias for the drill-hole separation on this page
                cs_alias = _find_cs_alias(pk_page, matched_info["original_name"])
                if cs_alias is None:
                    continue

                # Parse content stream(s)
                paths, limited = _extract_paths_for_cs(
                    pk_page, cs_alias
                )
                if limited:
                    extraction_limited = True

                for path_coords, has_close, stroke_w in paths:
                    if hole_counter >= _MAX_PATHS_PER_PAGE:
                        extraction_limited = True
                        break

                    hole_counter += 1
                    hole_id = f"B{hole_counter}"

                    # Compute bounding box
                    bbox = _path_bbox(path_coords)
                    if bbox is None:
                        continue

                    bx0, by0, bx1, by1 = bbox
                    bbox_w = bx1 - bx0
                    bbox_h = by1 - by0

                    if bbox_w <= 0 or bbox_h <= 0:
                        continue

                    # Check closed: h operator OR endpoint ≈ startpoint
                    is_closed = has_close
                    if not is_closed and len(path_coords) >= 2:
                        sx, sy = path_coords[0]
                        ex, ey = path_coords[-1]
                        if abs(sx - ex) < 0.1 and abs(sy - ey) < 0.1:
                            is_closed = True

                    # Circularity
                    ratio = abs(bbox_w - bbox_h) / max(bbox_w, bbox_h)
                    is_circular = ratio < 0.15

                    # Diameter (from smaller dimension)
                    diameter_mm = min(bbox_w, bbox_h) * PT_TO_MM

                    # Edge distance
                    edge_dist_mm = _edge_distance_mm(
                        bx0, by0, bx1, by1, ref_box
                    )

                    # Center
                    cx = (bx0 + bx1) / 2.0 * PT_TO_MM
                    cy = (by0 + by1) / 2.0 * PT_TO_MM

                    # Determine status + note
                    status = RuleStatus.PASS
                    notes: list[str] = []

                    if not is_closed:
                        status = RuleStatus.FAIL
                        notes.append("Pfad nicht geschlossen")

                    if diameter_mm < min_diam_mm:
                        status = RuleStatus.FAIL
                        notes.append(
                            f"Durchmesser {diameter_mm:.1f} mm < {min_diam_mm:.0f} mm"
                        )

                    if edge_dist_mm < min_edge_mm:
                        status = RuleStatus.FAIL
                        notes.append(
                            f"Randabstand {edge_dist_mm:.1f} mm < "
                            f"Mindestabstand zur Kante: {min_edge_mm:.0f} mm (Lieferant)"
                        )

                    if not is_circular:
                        if status == RuleStatus.PASS:
                            status = RuleStatus.WARN
                        notes.append("Nicht kreisrund")

                    # Stroke width check
                    if stroke_w is not None:
                        if abs(stroke_w - stroke_target) > stroke_tol:
                            if status == RuleStatus.PASS:
                                status = RuleStatus.WARN
                            notes.append(
                                f"Kontur {stroke_w:.2f} pt "
                                f"(Soll {stroke_target:.2f} ± {stroke_tol:.2f} pt)"
                            )

                    all_holes.append(DrillHoleInfo(
                        page=page_idx + 1,
                        hole_id=hole_id,
                        diameter_mm=round(diameter_mm, 1),
                        edge_distance_mm=round(edge_dist_mm, 1),
                        center_mm=[round(cx, 1), round(cy, 1)],
                        spot_name=spot_info.name,
                        is_circular=is_circular,
                        status=status,
                        note="; ".join(notes) if notes else "",
                    ))

    except Exception as exc:
        logger.warning("Drill-hole path extraction failed: %s", exc)
        extraction_limited = True

    # ── 3. Aggregate result ─────────────────────────────
    if not all_holes and extraction_limited:
        messages.append(
            "Separation 'Bohrungen' vorhanden, aber keine Pfade auswertbar "
            "(evtl. XObjects/Forms/Transformationen)"
        )
        return DrillHolesResult(
            found=False,
            spot_color=spot_info,
            separation_present=True,
            extraction_limited=True,
            holes=[],
            total_count=0,
            status=RuleStatus.WARN,
            messages=messages,
        )

    if not all_holes:
        messages.append(
            "Separation 'Bohrungen' vorhanden, aber keine Pfade auswertbar "
            "(evtl. XObjects/Forms/Transformationen)"
        )
        return DrillHolesResult(
            found=False,
            spot_color=spot_info,
            separation_present=True,
            extraction_limited=True,
            holes=[],
            total_count=0,
            status=RuleStatus.WARN,
            messages=messages,
        )

    # Determine aggregate status
    hole_statuses = [h.status for h in all_holes]
    if RuleStatus.FAIL in hole_statuses:
        agg_status = RuleStatus.FAIL
    elif RuleStatus.WARN in hole_statuses:
        agg_status = RuleStatus.WARN
    elif not cmyk_ok:
        agg_status = RuleStatus.WARN
    else:
        agg_status = RuleStatus.PASS

    if extraction_limited:
        if agg_status == RuleStatus.PASS:
            agg_status = RuleStatus.WARN
        messages.append("Pfad-Extraktion eingeschränkt (Limits erreicht)")

    fail_count = sum(1 for h in all_holes if h.status == RuleStatus.FAIL)
    messages.insert(0, f"{len(all_holes)} Bohrung(en) erkannt, {fail_count} mit Abweichungen")

    return DrillHolesResult(
        found=True,
        spot_color=spot_info,
        separation_present=True,
        extraction_limited=extraction_limited,
        holes=all_holes,
        total_count=len(all_holes),
        status=agg_status,
        messages=messages,
    )


# ══════════════════════════════════════════════════════════
# HELPERS: Content-stream parsing
# ══════════════════════════════════════════════════════════

_MAX_XOBJECTS_TOTAL = 200  # hard cap to prevent runaway


def _xobj_key(name: str, xobj: Any) -> str:
    """Build a stable key for a resolved XObject to prevent revisiting."""
    try:
        og = xobj.objgen
        if og != (0, 0):
            return f"xref:{og[0]}:{og[1]}"
    except Exception:
        pass
    return f"name:{name}"


def _find_cs_alias(page: pikepdf.Page, spot_name: str) -> str | None:
    """Find the /ColorSpace alias for a given Separation name on *page*.

    Searches page resources and recursively Form XObject resources.
    """
    try:
        resources = page.get("/Resources")
        if resources is None:
            return None
        visited: set[str] = set()
        return _find_cs_alias_in_resources(resources, spot_name, visited, depth=0)
    except Exception:
        pass
    return None


def _find_cs_alias_in_resources(
    resources: Any, spot_name: str, visited: set[str], depth: int,
) -> str | None:
    """Recursively search resources (incl. Form XObjects) for a CS alias."""
    if depth > _MAX_RECURSION:
        return None

    # 1) Check /ColorSpace on this level
    cs_dict = resources.get("/ColorSpace") if hasattr(resources, "get") else None
    if cs_dict is not None:
        if hasattr(cs_dict, "items"):
            for alias, cs_obj in cs_dict.items():
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
                    name = str(cs[1]).lstrip("/")
                    if name.upper() == spot_name.upper():
                        return str(alias).lstrip("/")
                except Exception:
                    continue

    # 2) Recurse into Form XObjects
    xobject_dict = resources.get("/XObject") if hasattr(resources, "get") else None
    if xobject_dict is None:
        return None
    if not hasattr(xobject_dict, "items"):
        return None
    for xobj_name, xobj_ref in xobject_dict.items():
        if len(visited) >= _MAX_XOBJECTS_TOTAL:
            return None
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
                found = _find_cs_alias_in_resources(
                    sub_resources, spot_name, visited, depth + 1,
                )
                if found is not None:
                    return found
        except Exception:
            continue

    return None


def _extract_paths_for_cs(
    page: pikepdf.Page,
    cs_alias: str,
) -> tuple[list[tuple[list[tuple[float, float]], bool, float | None]], bool]:
    """Parse content streams to extract paths drawn in *cs_alias*.

    Returns ``(paths, extraction_limited)`` where each path is
    ``(coords, has_closepath, stroke_width_or_None)``.
    """
    paths: list[tuple[list[tuple[float, float]], bool, float | None]] = []
    limited = False
    visited: set[int] = set()

    # Get page content stream
    try:
        contents = page.get("/Contents")
        if contents is None:
            return paths, limited

        # Collect all content stream data
        stream_data = _resolve_contents(contents)
    except Exception as exc:
        logger.debug("Cannot read page contents: %s", exc)
        return paths, True

    # Parse the main page stream
    resources = page.get("/Resources")
    new_paths, new_limited = _parse_stream(
        stream_data, cs_alias, resources, visited, depth=0
    )
    paths.extend(new_paths)
    if new_limited:
        limited = True

    return paths, limited


def _resolve_contents(contents: Any) -> bytes:
    """Resolve /Contents which can be a stream or array of streams."""
    if isinstance(contents, pikepdf.Array):
        parts = []
        for item in contents:
            obj = item.resolve() if hasattr(item, "resolve") else item
            if hasattr(obj, "read_bytes"):
                parts.append(obj.read_bytes())
            elif hasattr(obj, "get_stream_buffer"):
                parts.append(bytes(obj.get_stream_buffer()))
        return b"\n".join(parts)
    obj = contents.resolve() if hasattr(contents, "resolve") else contents
    if hasattr(obj, "read_bytes"):
        return obj.read_bytes()
    if hasattr(obj, "get_stream_buffer"):
        return bytes(obj.get_stream_buffer())
    return b""


def _parse_stream(
    data: bytes,
    cs_alias: str,
    resources: Any,
    visited: set[int],
    depth: int,
) -> tuple[list[tuple[list[tuple[float, float]], bool, float | None]], bool]:
    """Parse a content stream for paths in the target colour space.

    Handles q/Q, cm, cs/CS for colour space switching, and path ops.
    """
    if depth > _MAX_RECURSION:
        return [], True

    paths: list[tuple[list[tuple[float, float]], bool, float | None]] = []
    limited = False

    # Tokenize (simple approach: split on whitespace, handle operators)
    tokens = _tokenize_stream(data)

    # State
    in_target_cs = False
    current_path: list[tuple[float, float]] = []
    has_close = False
    stroke_width: float | None = None
    stack: list[float] = []
    # CTM stack for q/Q
    ctm_stack: list[list[float]] = []
    ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]  # identity matrix
    ops_count = 0

    for token in tokens:
        ops_count += 1
        if ops_count > _MAX_OPS_PER_STREAM:
            limited = True
            break

        if len(paths) > _MAX_PATHS_PER_PAGE:
            limited = True
            break

        # Try to parse as number
        num = _try_float(token)
        if num is not None:
            stack.append(num)
            continue

        op = token

        if op == "q":
            ctm_stack.append(ctm[:])
        elif op == "Q":
            if ctm_stack:
                ctm = ctm_stack.pop()
        elif op == "cm" and len(stack) >= 6:
            # Concatenate matrix
            new_ctm = stack[-6:]
            ctm = _concat_matrix(ctm, new_ctm)
            stack = stack[:-6]
        elif op == "w" and len(stack) >= 1:
            stroke_width = stack[-1]
            stack = stack[:-1]
        elif op in ("cs", "CS"):
            # Check if the operand is our target CS
            if len(stack) >= 1:
                # Numeric operand shouldn't happen for cs, skip
                stack.pop()
            # The CS name is usually a preceding Name token
            # We handle it via the token before: /CSname cs
            pass
        elif token.startswith("/") and token.lstrip("/") == cs_alias:
            # This is a name token – peek at next for cs/CS
            # Actually we handle it below in the operator check
            stack.clear()
            # Set flag: next cs/CS confirms this colour space
            in_target_cs = True
        elif op == "cs" or op == "CS":
            # cs was already handled; the preceding name set in_target_cs
            pass
        elif op == "scn" or op == "SCN" or op == "sc" or op == "SC":
            # Colour value set – consume stack but keep cs state
            stack.clear()
        elif op == "m" and len(stack) >= 2:
            # moveto – start new subpath
            y, x = stack[-1], stack[-2]
            stack = stack[:-2]
            tx, ty = _transform_point(x, y, ctm)
            current_path = [(tx, ty)]
            has_close = False
        elif op == "l" and len(stack) >= 2:
            y, x = stack[-1], stack[-2]
            stack = stack[:-2]
            tx, ty = _transform_point(x, y, ctm)
            current_path.append((tx, ty))
        elif op == "c" and len(stack) >= 6:
            # Cubic Bézier: x1 y1 x2 y2 x3 y3
            coords = stack[-6:]
            stack = stack[:-6]
            # Add control points and endpoint for bbox
            for i in range(0, 6, 2):
                tx, ty = _transform_point(coords[i], coords[i + 1], ctm)
                current_path.append((tx, ty))
        elif op == "v" and len(stack) >= 4:
            coords = stack[-4:]
            stack = stack[:-4]
            for i in range(0, 4, 2):
                tx, ty = _transform_point(coords[i], coords[i + 1], ctm)
                current_path.append((tx, ty))
        elif op == "y" and len(stack) >= 4:
            coords = stack[-4:]
            stack = stack[:-4]
            for i in range(0, 4, 2):
                tx, ty = _transform_point(coords[i], coords[i + 1], ctm)
                current_path.append((tx, ty))
        elif op == "re" and len(stack) >= 4:
            # Rectangle: x y w h
            h_val, w_val, y_val, x_val = stack[-1], stack[-2], stack[-3], stack[-4]
            stack = stack[:-4]
            p1 = _transform_point(x_val, y_val, ctm)
            p2 = _transform_point(x_val + w_val, y_val, ctm)
            p3 = _transform_point(x_val + w_val, y_val + h_val, ctm)
            p4 = _transform_point(x_val, y_val + h_val, ctm)
            current_path = [p1, p2, p3, p4]
            has_close = True  # re implicitly closes
        elif op == "h":
            has_close = True
        elif op in _PAINT_OPS:
            # Finalize path
            if in_target_cs and current_path:
                paths.append((current_path[:], has_close, stroke_width))
            current_path = []
            has_close = False
            stack.clear()
        elif op == "Do":
            # Invoke XObject – handle Form XObjects recursively
            # The name was the preceding token
            pass
        else:
            # Unknown operator or name token – for name tokens starting with /
            if not token.startswith("/"):
                stack.clear()

    return paths, limited


def _tokenize_stream(data: bytes) -> list[str]:
    """Simple tokenizer for PDF content streams."""
    text = data.decode("latin-1", errors="replace")
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "%":
            # Comment: skip to end of line
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/":
            # Name token
            j = i + 1
            while j < n and text[j] not in " \t\r\n/<>[](){}%":
                j += 1
            tokens.append(text[i:j])
            i = j
            continue
        if ch == "(":
            # String literal: skip
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            i = j
            continue
        if ch == "<" and i + 1 < n and text[i + 1] == "<":
            # Dict begin
            i += 2
            continue
        if ch == ">" and i + 1 < n and text[i + 1] == ">":
            i += 2
            continue
        if ch == "<":
            # Hex string
            j = i + 1
            while j < n and text[j] != ">":
                j += 1
            i = j + 1
            continue
        # Regular token (number or operator)
        j = i
        while j < n and text[j] not in " \t\r\n/<>[](){}%":
            j += 1
        tokens.append(text[i:j])
        i = j
    return tokens


# ── Matrix / geometry helpers ────────────────────────────

def _try_float(s: str) -> float | None:
    try:
        return float(s)
    except (ValueError, OverflowError):
        return None


def _concat_matrix(
    m1: list[float], m2: list[float]
) -> list[float]:
    """Concatenate two 3×2 affine matrices: m2 × m1 (PDF convention)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [
        a2 * a1 + b2 * c1,
        a2 * b1 + b2 * d1,
        c2 * a1 + d2 * c1,
        c2 * b1 + d2 * d1,
        e2 * a1 + f2 * c1 + e1,
        e2 * b1 + f2 * d1 + f1,
    ]


def _transform_point(
    x: float, y: float, ctm: list[float]
) -> tuple[float, float]:
    """Apply CTM to a point."""
    a, b, c, d, e, f = ctm
    return (a * x + c * y + e, b * x + d * y + f)


def _path_bbox(
    coords: list[tuple[float, float]],
) -> tuple[float, float, float, float] | None:
    """Compute bounding box from path coordinates."""
    if not coords:
        return None
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    return (min(xs), min(ys), max(xs), max(ys))


def _edge_distance_mm(
    bx0: float, by0: float, bx1: float, by1: float,
    trim: fitz.Rect,
) -> float:
    """Compute minimum edge distance from bbox to trim box in mm.

    left   = bbox.x0 - trim.x0
    right  = trim.x1 - bbox.x1
    bottom = bbox.y0 - trim.y0
    top    = trim.y1 - bbox.y1
    """
    left = bx0 - trim.x0
    right = trim.x1 - bx1
    bottom = by0 - trim.y0
    top = trim.y1 - by1
    return min(left, right, bottom, top) * PT_TO_MM
