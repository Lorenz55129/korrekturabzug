"""Expand a PDF by adding bleed on all four sides via edge-stretching.

Strategy – "Rand strecken":
  A thin strip (_STRIP_PT wide) is taken from each of the four TrimBox edges
  and rendered as a raster image into the corresponding bleed band.  The DPI
  used for rendering is automatically detected from the source page (minimum
  effective DPI of all raster images); _FALLBACK_DPI is used for vector-only
  pages.  The four corner areas are filled by rendering the respective corner
  strip.  Finally, the original TrimBox content is placed on top via
  show_pdf_page at 1:1 scale so that:
    • the TrimBox area is covered by the original vector content, and
    • the bleed bands are plain Image XObjects whose DPI matches the source,
      so the preflight DPI checker reports a consistent value.

Why raster instead of show_pdf_page for the strips?
  show_pdf_page(dst, ..., clip=narrow_strip, keep_proportion=False) creates a
  Form XObject with an extreme scale factor (bleed_pt / STRIP_PT ≈ 7×).  The
  preflight DPI checker finds images inside that Form XObject and divides their
  pixel size by the 7× scaled placed size — reporting ~30 DPI instead of the
  actual source DPI.  Using get_pixmap + insert_image creates plain Image
  XObjects at the correct source DPI, which the checker reads correctly.
"""

from __future__ import annotations

import logging
from pathlib import Path

import fitz  # PyMuPDF ≥ 1.23

logger = logging.getLogger(__name__)

MM_TO_PT: float = 72.0 / 25.4

# Width of the source strip sampled from each TrimBox edge.
# 2 pt ≈ 0.7 mm — narrow enough to represent "the very edge colour" while
# still being a valid, non-degenerate rectangle for get_pixmap.
_STRIP_PT: float = 2.0

# Fallback resolution used when rasterising bleed strips on pages that contain
# no raster images (pure vector artwork).  For pages with raster images the
# actual strip DPI is determined automatically from the source page.
_FALLBACK_DPI: float = 300.0


def _detect_effective_dpi(page: fitz.Page, doc: fitz.Document) -> float:
    """Return the minimum effective DPI of all raster images on *page*.

    Effective DPI = pixel_width / (placed_width_pt / 72).  The minimum over
    all axes and all images is returned so that bleed strips are never
    rendered at a higher resolution than the source content.

    Falls back to *_FALLBACK_DPI* when the page contains no raster images
    (pure vector artwork) or when no placement rectangle can be determined.
    """
    min_dpi = _FALLBACK_DPI
    for img_info in page.get_images(full=True):
        xref = img_info[0]
        try:
            pix = fitz.Pixmap(doc, xref)
            pw, ph = pix.width, pix.height
            pix = None  # free memory immediately
        except Exception:
            continue
        for rect in page.get_image_rects(xref):
            placed_w = abs(rect.width)
            placed_h = abs(rect.height)
            if placed_w > 0 and placed_h > 0:
                dpi = min(pw / (placed_w / 72.0), ph / (placed_h / 72.0))
                if dpi > 0:
                    min_dpi = min(min_dpi, dpi)
    return min_dpi


def _reference_box(page: fitz.Page) -> fitz.Rect:
    """Return TrimBox > CropBox > MediaBox (first one that differs from MediaBox)."""
    mb = page.mediabox
    tb = page.trimbox
    cb = page.cropbox
    if tb != mb:
        return tb
    if cb != mb:
        return cb
    return mb


def _render_strip(
    page: fitz.Page,
    src_clip: fitz.Rect,
    dst_w_pt: float,
    dst_h_pt: float,
    dpi: float,
) -> fitz.Pixmap:
    """Render *src_clip* of *page* into a Pixmap sized for *dst_w_pt × dst_h_pt* at *dpi*.

    The pixmap will have:
      width  = round(dst_w_pt * dpi / 72)  pixels
      height = round(dst_h_pt * dpi / 72)  pixels

    A non-uniform fitz.Matrix is used so the strip is stretched to fill the
    destination rectangle — equivalent to keep_proportion=False in show_pdf_page
    but producing a raster Image XObject instead of a scaled Form XObject.
    """
    scale_x = (dst_w_pt / src_clip.width) * (dpi / 72.0)
    scale_y = (dst_h_pt / src_clip.height) * (dpi / 72.0)
    mat = fitz.Matrix(scale_x, scale_y)
    return page.get_pixmap(matrix=mat, clip=src_clip, alpha=False)


def add_bleed(
    input_path: str | Path,
    output_path: str | Path,
    bleed_mm: float,
) -> tuple[Path, int]:
    """Create *output_path* — a copy of *input_path* with *bleed_mm* added on every side.

    Returns ``(output_path, page_count)``.

    For each page the algorithm is:

    1.  Determine the TrimBox (= the "real" page size used as anchor).
    2.  Create a new output page whose MediaBox is TrimBox + bleed_mm on all sides.
    3.  Fill the four bleed bands by rasterising a thin strip from each TrimBox edge
        at the auto-detected source DPI and inserting it as an Image XObject.
    4.  Fill the four corner bleed areas the same way.
    5.  Place the original TrimBox content on top via show_pdf_page (vector quality,
        1:1 scale) so the TrimBox area is pixel-perfect.
    6.  Set the new page's TrimBox so that CIP4/JDF-aware tools can read it.
        The BleedBox equals the full new MediaBox (default for a new page).
    """
    bleed_pt = bleed_mm * MM_TO_PT
    output_path = Path(output_path)

    doc_in = fitz.open(str(input_path))
    doc_out = fitz.open()

    for pno in range(len(doc_in)):
        page = doc_in[pno]
        trim = _reference_box(page)

        tw = trim.width   # TrimBox width  in pt
        th = trim.height  # TrimBox height in pt
        bp = bleed_pt

        # ── New page size (= new MediaBox = new BleedBox) ─────────────────
        new_w = tw + 2.0 * bp
        new_h = th + 2.0 * bp
        new_page = doc_out.new_page(width=new_w, height=new_h)

        # Convenience aliases for TrimBox corners in source space
        tx0, ty0 = trim.x0, trim.y0
        tx1, ty1 = trim.x1, trim.y1

        s = _STRIP_PT  # strip width/height in source space

        # ── Detect source DPI for bleed strips ────────────────────────────
        # Use the minimum effective DPI of raster images on this page so that
        # bleed strips are never rendered at a higher resolution than the
        # source content.  Falls back to _FALLBACK_DPI for vector-only pages.
        strip_dpi = _detect_effective_dpi(page, doc_in)

        # ── Step 1: rasterise edge strips into the four bleed bands ───────
        # Each call: render source strip → pixmap sized for destination rect
        # → insert as Image XObject (DPI correctly reported by preflight).

        # Left band  (bp × th):  source = left edge strip of TrimBox
        pix = _render_strip(page, fitz.Rect(tx0, ty0, tx0 + s, ty1), bp, th, strip_dpi)
        new_page.insert_image(fitz.Rect(0, bp, bp, bp + th), pixmap=pix)

        # Right band (bp × th):  source = right edge strip of TrimBox
        pix = _render_strip(page, fitz.Rect(tx1 - s, ty0, tx1, ty1), bp, th, strip_dpi)
        new_page.insert_image(fitz.Rect(bp + tw, bp, new_w, bp + th), pixmap=pix)

        # Top band   (tw × bp):  source = top edge strip of TrimBox
        pix = _render_strip(page, fitz.Rect(tx0, ty0, tx1, ty0 + s), tw, bp, strip_dpi)
        new_page.insert_image(fitz.Rect(bp, 0, bp + tw, bp), pixmap=pix)

        # Bottom band (tw × bp): source = bottom edge strip of TrimBox
        pix = _render_strip(page, fitz.Rect(tx0, ty1 - s, tx1, ty1), tw, bp, strip_dpi)
        new_page.insert_image(fitz.Rect(bp, bp + th, bp + tw, new_h), pixmap=pix)

        # ── Step 2: rasterise the four corner bleed areas ─────────────────
        # Top-left corner (bp × bp)
        pix = _render_strip(page, fitz.Rect(tx0, ty0, tx0 + s, ty0 + s), bp, bp, strip_dpi)
        new_page.insert_image(fitz.Rect(0, 0, bp, bp), pixmap=pix)

        # Top-right corner (bp × bp)
        pix = _render_strip(page, fitz.Rect(tx1 - s, ty0, tx1, ty0 + s), bp, bp, strip_dpi)
        new_page.insert_image(fitz.Rect(bp + tw, 0, new_w, bp), pixmap=pix)

        # Bottom-left corner (bp × bp)
        pix = _render_strip(page, fitz.Rect(tx0, ty1 - s, tx0 + s, ty1), bp, bp, strip_dpi)
        new_page.insert_image(fitz.Rect(0, bp + th, bp, new_h), pixmap=pix)

        # Bottom-right corner (bp × bp)
        pix = _render_strip(page, fitz.Rect(tx1 - s, ty1 - s, tx1, ty1), bp, bp, strip_dpi)
        new_page.insert_image(fitz.Rect(bp + tw, bp + th, new_w, new_h), pixmap=pix)

        # ── Step 3: place original TrimBox content on top at 1:1 (vector) ─
        # show_pdf_page with clip=trim maps TrimBox → (bp, bp, bp+tw, bp+th)
        # at exactly 1:1 scale.  This preserves full vector / image quality
        # for the actual page content and covers the raster strips inside the
        # TrimBox area (there are none, but the overlap is clean regardless).
        new_page.show_pdf_page(
            fitz.Rect(bp, bp, bp + tw, bp + th),
            doc_in, pno,
            clip=trim,
        )

        # ── Step 4: set TrimBox on the new page ───────────────────────────
        new_page.set_trimbox(fitz.Rect(bp, bp, bp + tw, bp + th))
        # BleedBox = full MediaBox (the default for a new page — no set needed)

    page_count = len(doc_in)
    doc_out.save(str(output_path), garbage=4, deflate=True)
    doc_out.close()
    doc_in.close()

    logger.info("Bleed %.1f mm added to %d page(s) → %s", bleed_mm, page_count, output_path)
    return output_path, page_count
