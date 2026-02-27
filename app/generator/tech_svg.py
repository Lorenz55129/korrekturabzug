"""Export CutContour and Stanze paths to SVG."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
import pikepdf

from app.config import load_config

logger = logging.getLogger(__name__)

PT_TO_MM = 25.4 / 72.0

# ── Helpers ──────────────────────────────────────────────


def _spot_names_in_config(cfg: dict[str, Any]) -> dict[str, str]:
    """Return {UPPER_NAME: group_label} for all configured spot colours."""
    names: dict[str, str] = {}
    for n in cfg.get("cut_contour", {}).get("allowed_names", []):
        names[n.upper()] = "CutContour"
    for n in cfg.get("die", {}).get("allowed_names", []):
        names[n.upper()] = "Stanze"
    return names


def _find_separation_cs_aliases(pdf_path: str, target_names: set[str]) -> dict[str, str]:
    """Walk page resources and return {cs_alias: spot_name_upper} for matching Separations."""
    aliases: dict[str, str] = {}
    with pikepdf.open(pdf_path) as pdf:
        for page in pdf.pages:
            res = page.get("/Resources")
            if res is None:
                continue
            cs_dict = res.get("/ColorSpace")
            if cs_dict is None:
                continue
            for alias, cs_obj in cs_dict.items():
                try:
                    cs = cs_obj.resolve() if hasattr(cs_obj, "resolve") else cs_obj
                    if not isinstance(cs, pikepdf.Array) or len(cs) < 2:
                        continue
                    if str(cs[0]) != "/Separation":
                        continue
                    spot = str(cs[1]).lstrip("/")
                    if spot.upper() in target_names:
                        aliases[alias.lstrip("/")] = spot.upper()
                except Exception:
                    pass
    return aliases


def _extract_drawings_for_spot(
    page: fitz.Page,
    cs_alias_map: dict[str, str],
    target_upper: str,
) -> list[dict[str, Any]]:
    """Extract vector drawing items that use the target spot colour.

    PyMuPDF's page.get_drawings() returns path items with colour info.
    We match by checking if the stroking colorspace alias matches our spot.

    Fallback: if we cannot match by colorspace (PyMuPDF doesn't always expose
    the CS alias), we collect ALL vector paths — the caller should filter further.
    """
    drawings = page.get_drawings()
    matched: list[dict[str, Any]] = []

    for d in drawings:
        # get_drawings returns dicts with keys: items, color, fill, width, ...
        # 'color' is the stroke colour (tuple or None)
        # We cannot directly get the CS name from get_drawings, so we use a
        # heuristic: if the stroke colour is close to Magenta (1,0,1,0 in RGB
        # or (0,1,0,0) in CMYK normalised), treat it as our spot.
        stroke = d.get("color")
        fill = d.get("fill")
        if stroke is not None:
            # PyMuPDF returns RGB tuples from get_drawings
            r, g, b = (stroke + (0, 0, 0))[:3]
            # Magenta in RGB ≈ (1, 0, 1) – allow tolerance
            is_magenta_rgb = (r > 0.8 and g < 0.2 and b > 0.8)
            # Pure spot colours sometimes render as a single-channel
            if is_magenta_rgb or (r > 0.8 and g < 0.2 and b < 0.2):
                matched.append(d)
                continue

        # Also collect if fill matches (shouldn't for contour, but be safe)
        if fill is not None:
            r, g, b = (fill + (0, 0, 0))[:3]
            if r > 0.8 and g < 0.2 and b > 0.8:
                matched.append(d)

    return matched


def _drawing_to_svg_path(drawing: dict[str, Any]) -> str:
    """Convert a PyMuPDF drawing dict to an SVG <path> d-attribute string."""
    parts: list[str] = []
    for item in drawing.get("items", []):
        kind = item[0]
        if kind == "l":  # line
            p1, p2 = item[1], item[2]
            if not parts:
                parts.append(f"M {p1.x:.3f} {p1.y:.3f}")
            parts.append(f"L {p2.x:.3f} {p2.y:.3f}")
        elif kind == "re":  # rectangle
            rect = item[1]
            parts.append(f"M {rect.x0:.3f} {rect.y0:.3f}")
            parts.append(f"L {rect.x1:.3f} {rect.y0:.3f}")
            parts.append(f"L {rect.x1:.3f} {rect.y1:.3f}")
            parts.append(f"L {rect.x0:.3f} {rect.y1:.3f}")
            parts.append("Z")
        elif kind == "c":  # cubic bezier
            p1, p2, p3, p4 = item[1], item[2], item[3], item[4]
            if not parts:
                parts.append(f"M {p1.x:.3f} {p1.y:.3f}")
            parts.append(f"C {p2.x:.3f} {p2.y:.3f} {p3.x:.3f} {p3.y:.3f} {p4.x:.3f} {p4.y:.3f}")
        elif kind == "qu":  # quad bezier
            p1, p2, p3 = item[1], item[2], item[3]
            if not parts:
                parts.append(f"M {p1.x:.3f} {p1.y:.3f}")
            parts.append(f"Q {p2.x:.3f} {p2.y:.3f} {p3.x:.3f} {p3.y:.3f}")

    if drawing.get("closePath"):
        parts.append("Z")

    return " ".join(parts)


# ── Public entry point ──────────────────────────────────


def generate_tech_svg(
    pdf_path: str | Path,
    output_path: str | Path,
    cfg: dict[str, Any] | None = None,
) -> Path:
    """Extract CutContour and Stanze vector paths and write an SVG file.

    Coordinate system: points (pt), matching the PDF page coordinate system.
    The SVG documents the unit in a comment.
    """
    pdf_path = str(pdf_path)
    output_path = Path(output_path)
    cfg = cfg or load_config()

    spot_group_map = _spot_names_in_config(cfg)  # {UPPER: group_label}
    target_names = set(spot_group_map.keys())
    cs_aliases = _find_separation_cs_aliases(pdf_path, target_names)

    doc = fitz.open(pdf_path)
    try:
        # Use page 1 dimensions for the SVG viewBox
        page0 = doc[0]
        mb = page0.mediabox
        vb_w = mb.width
        vb_h = mb.height

        svg = ET.Element("svg", {
            "xmlns": "http://www.w3.org/2000/svg",
            "viewBox": f"0 0 {vb_w:.3f} {vb_h:.3f}",
            "width": f"{vb_w * PT_TO_MM:.2f}mm",
            "height": f"{vb_h * PT_TO_MM:.2f}mm",
        })
        svg.append(ET.Comment(
            " Coordinate unit: PDF points (1 pt = 1/72 inch = 0.3528 mm). "
            "viewBox matches MediaBox of page 1. "
        ))

        # Create groups
        groups: dict[str, ET.Element] = {}
        for label in sorted(set(spot_group_map.values())):
            g = ET.SubElement(svg, "g", {"id": label, "fill": "none"})
            groups[label] = g

        # Set default stroke colours per group
        stroke_colors = {"CutContour": "#FF00FF", "Stanze": "#0000FF"}

        for page_num in range(len(doc)):
            page = doc[page_num]

            # For each target spot colour, extract matching drawings
            for upper_name, group_label in spot_group_map.items():
                drawings = _extract_drawings_for_spot(page, cs_aliases, upper_name)
                g = groups.get(group_label)
                if g is None:
                    continue

                for d_idx, drawing in enumerate(drawings):
                    d_str = _drawing_to_svg_path(drawing)
                    if not d_str.strip():
                        continue
                    stroke_w = drawing.get("width", 0.25)
                    ET.SubElement(g, "path", {
                        "d": d_str,
                        "stroke": stroke_colors.get(group_label, "#000000"),
                        "stroke-width": f"{stroke_w:.3f}",
                        "data-page": str(page_num + 1),
                        "data-index": str(d_idx),
                    })

        # Write SVG
        tree = ET.ElementTree(svg)
        ET.indent(tree, space="  ")
        tree.write(str(output_path), encoding="unicode", xml_declaration=True)

        logger.info("Tech SVG written to %s", output_path)
        return output_path

    finally:
        doc.close()
