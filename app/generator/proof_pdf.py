"""Generate proof_output.pdf (compact mode):
   - Fill AcroForm template
   - 1 Summary page (Ampel overview + action items)
   - N Preview pages (per customer page: preview + dimensions + material)
   - 0-2 Detail pages (FAIL/WARN only, grouped, row-limited)
   - Optional: preflight_details.json for overflow
"""

from __future__ import annotations

import io
import json
import logging
import math
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Optional

import fitz  # PyMuPDF
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Flowable,
    Image as _RLImage,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.config import get_field_mapping, load_config
from app.models import PreflightResult, ProofRequest, RuleStatus

logger = logging.getLogger(__name__)

PT_TO_MM = 25.4 / 72.0
MM_TO_PT = 72.0 / 25.4

# ── Report limits ──────────────────────────────────────
_MAX_DETAIL_ROWS = 50  # max FAIL/WARN rows in PDF detail table
_MATERIAL_MIN_FONT_PT = 7  # minimum font size for material text
_MATERIAL_MAX_HEIGHT_PT = 100  # max height for material text block on preview page

# ── Colour helpers ──────────────────────────────────────

_STATUS_COLORS = {
    RuleStatus.PASS: colors.HexColor("#2e7d32"),
    RuleStatus.WARN: colors.HexColor("#ef6c00"),
    RuleStatus.FAIL: colors.HexColor("#c62828"),
}


def _status_html(status: RuleStatus) -> str:
    """Return coloured HTML span for a status value."""
    col = _STATUS_COLORS[status].hexval()
    return f'<font color="#{col}"><b>{status.value}</b></font>'


# ── Fill AcroForm fields via PyMuPDF ────────────────────

def _fill_form(template_path: str, output_path: str, fields: dict[str, str]) -> None:
    """Open *template_path*, set field values, save to *output_path*."""
    doc = fitz.open(template_path)
    mapping = get_field_mapping()

    for page in doc:
        for widget in page.widgets():
            field_name = widget.field_name
            if field_name is None:
                continue
            for internal_key, form_field in mapping.items():
                if form_field == field_name and internal_key in fields:
                    widget.field_value = fields[internal_key]
                    widget.update()

    doc.save(output_path)
    doc.close()


# ── Determine reference box for a page ──────────────────

def _get_reference_box(page: fitz.Page) -> tuple[fitz.Rect, str]:
    """Return (rect, box_name) for the best reference box: TrimBox > CropBox > MediaBox."""
    mediabox = page.mediabox
    trimbox = page.trimbox
    cropbox = page.cropbox

    if trimbox != mediabox:
        return trimbox, "TrimBox"
    if cropbox != mediabox:
        return cropbox, "CropBox"
    return mediabox, "MediaBox"


def _get_bleed_box(page: fitz.Page) -> Optional[fitz.Rect]:
    """Return BleedBox if it differs from MediaBox, else None."""
    bleedbox = page.bleedbox
    mediabox = page.mediabox
    if bleedbox != mediabox:
        return bleedbox
    return None


# ── Render page preview as JPEG bytes (max 72 DPI) ──────

_MAX_PREVIEW_DPI = 72
_MAX_PREVIEW_PIXELS = 40_000_000  # safety net for very large pages (banners)


def _render_page_preview(
    doc: fitz.Document,
    page_num: int,
    dpi: int = 72,
    clip_rect: Optional[fitz.Rect] = None,
) -> bytes:
    """Render a PDF page to JPEG bytes at max 72 DPI.

    If *clip_rect* is given, only that region of the page is rendered
    (typically the TrimBox, so bleed areas are excluded from the preview).

    If the resulting pixel count exceeds *_MAX_PREVIEW_PIXELS* (e.g. for
    oversized banner pages), DPI is further reduced to stay within budget.
    This is purely cosmetic – preflight DPI checks happen beforehand.
    """
    page = doc[page_num]
    dpi = min(dpi, _MAX_PREVIEW_DPI)

    # Use clip rect for budget calculation; fall back to full page rect
    render_area = clip_rect if clip_rect is not None else page.rect
    w_px = render_area.width * dpi / 72.0
    h_px = render_area.height * dpi / 72.0

    if w_px * h_px > _MAX_PREVIEW_PIXELS:
        factor = math.sqrt(_MAX_PREVIEW_PIXELS / (w_px * h_px))
        dpi = max(10, math.floor(dpi * factor))

    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, clip=clip_rect, alpha=False)
    jpeg_bytes = pix.tobytes("jpeg", jpg_quality=80)
    pix = None  # free
    return jpeg_bytes


# ── Draw dimension lines with ReportLab Canvas ─────────

class _DimensionDrawing:
    """Flowable-like helper that draws dimension lines on the canvas
    around a placed preview image.

    All real dimensions are in mm (from the PDF boxes).
    The drawing coordinates are in ReportLab points.
    """

    def __init__(
        self,
        preview_x_pt: float,
        preview_y_pt: float,
        preview_w_pt: float,
        preview_h_pt: float,
        real_w_mm: float,
        real_h_mm: float,
        box_name: str,
        bleed_w_mm: Optional[float] = None,
        bleed_h_mm: Optional[float] = None,
        cfg: Optional[dict[str, Any]] = None,
    ):
        self.px = preview_x_pt
        self.py = preview_y_pt
        self.pw = preview_w_pt
        self.ph = preview_h_pt
        self.real_w = real_w_mm
        self.real_h = real_h_mm
        self.box_name = box_name
        self.bleed_w = bleed_w_mm
        self.bleed_h = bleed_h_mm

        pcfg = (cfg or {}).get("preview", {})
        self.line_color = colors.HexColor(pcfg.get("dimension_line_color", "#333333"))
        self.line_width = pcfg.get("dimension_line_width_pt", 0.5)
        self.arrow_size = pcfg.get("arrow_size_pt", 4)
        self.dash = pcfg.get("bleed_line_dash", [3, 2])

    # ── arrow helpers ───────────────────────────────────

    def _draw_arrow_h(self, c, x: float, y: float, direction: int):
        """Draw a small arrowhead pointing left (-1) or right (+1)."""
        a = self.arrow_size
        c.saveState()
        c.setFillColor(self.line_color)
        p = c.beginPath()
        p.moveTo(x, y)
        p.lineTo(x - direction * a, y + a * 0.4)
        p.lineTo(x - direction * a, y - a * 0.4)
        p.close()
        c.drawPath(p, fill=1, stroke=0)
        c.restoreState()

    def _draw_arrow_v(self, c, x: float, y: float, direction: int):
        """Draw a small arrowhead pointing down (-1) or up (+1)."""
        a = self.arrow_size
        c.saveState()
        c.setFillColor(self.line_color)
        p = c.beginPath()
        p.moveTo(x, y)
        p.lineTo(x - a * 0.4, y - direction * a)
        p.lineTo(x + a * 0.4, y - direction * a)
        p.close()
        c.drawPath(p, fill=1, stroke=0)
        c.restoreState()

    # ── main draw routine ───────────────────────────────

    def draw_on_canvas(self, c):
        """Draw dimension lines on a ReportLab canvas *c*."""
        c.saveState()
        c.setStrokeColor(self.line_color)
        c.setLineWidth(self.line_width)
        c.setFont("Helvetica", 7)

        gap = 6  # pt gap between preview edge and dimension line
        ext = 3  # pt extension lines beyond arrow tips

        # ── Horizontal (width) dimension below the preview ──
        y_line = self.py - gap
        x_left = self.px
        x_right = self.px + self.pw

        # extension lines (vertical ticks)
        c.line(x_left, self.py - ext, x_left, y_line - ext)
        c.line(x_right, self.py - ext, x_right, y_line - ext)
        # main horizontal line
        c.line(x_left, y_line, x_right, y_line)
        # arrows
        self._draw_arrow_h(c, x_left, y_line, -1)
        self._draw_arrow_h(c, x_right, y_line, 1)
        # label
        label_w = f"{self.real_w:.1f} mm"
        c.setFillColor(self.line_color)
        c.drawCentredString((x_left + x_right) / 2, y_line - 9, label_w)
        # box name hint
        c.setFont("Helvetica", 5.5)
        c.setFillColor(colors.HexColor("#888888"))
        c.drawCentredString((x_left + x_right) / 2, y_line - 16, f"({self.box_name})")

        # ── Vertical (height) dimension to the right ────────
        c.setFont("Helvetica", 7)
        c.setFillColor(self.line_color)
        x_line = self.px + self.pw + gap
        y_bottom = self.py
        y_top = self.py + self.ph

        # extension lines (horizontal ticks)
        c.line(self.px + self.pw + ext, y_bottom, x_line + ext, y_bottom)
        c.line(self.px + self.pw + ext, y_top, x_line + ext, y_top)
        # main vertical line
        c.line(x_line, y_bottom, x_line, y_top)
        # arrows
        self._draw_arrow_v(c, x_line, y_bottom, -1)
        self._draw_arrow_v(c, x_line, y_top, 1)
        # label (rotated)
        label_h = f"{self.real_h:.1f} mm"
        c.saveState()
        c.translate(x_line + 10, (y_bottom + y_top) / 2)
        c.rotate(90)
        c.drawCentredString(0, 0, label_h)
        c.restoreState()

        # ── BleedBox dashed lines (if present) ──────────────
        if self.bleed_w is not None and self.bleed_h is not None:
            c.setDash(self.dash[0], self.dash[1])

            # Scale factors: how bleed relates to reference in preview coords
            scale_x = self.pw / self.real_w if self.real_w > 0 else 0
            scale_y = self.ph / self.real_h if self.real_h > 0 else 0
            bleed_pw = self.bleed_w * scale_x
            bleed_ph = self.bleed_h * scale_y
            bx_offset = (bleed_pw - self.pw) / 2
            by_offset = (bleed_ph - self.ph) / 2

            bx_left = self.px - bx_offset
            bx_right = self.px + self.pw + bx_offset
            by_bottom = self.py - by_offset
            by_top = self.py + self.ph + by_offset

            # Dashed horizontal below (further down)
            y_bline = by_bottom - gap - 20
            c.line(bx_left, y_bline, bx_right, y_bline)
            c.line(bx_left, by_bottom, bx_left, y_bline - ext)
            c.line(bx_right, by_bottom, bx_right, y_bline - ext)
            self._draw_arrow_h(c, bx_left, y_bline, -1)
            self._draw_arrow_h(c, bx_right, y_bline, 1)
            c.setDash()  # reset
            c.setFont("Helvetica", 6)
            c.setFillColor(colors.HexColor("#666666"))
            c.drawCentredString(
                (bx_left + bx_right) / 2, y_bline - 8,
                f"BleedBox: {self.bleed_w:.1f} mm"
            )

        c.restoreState()


# ── PreviewFlowable: image + dimension lines ────────────

class PreviewFlowable(Flowable):
    """ReportLab Flowable: page preview image + dimension lines."""

    def __init__(
        self,
        img_bytes: bytes,
        preview_w_pt: float,
        preview_h_pt: float,
        real_w_mm: float,
        real_h_mm: float,
        box_name: str,
        bleed_w_mm: Optional[float],
        bleed_h_mm: Optional[float],
        cfg: dict[str, Any],
    ):
        super().__init__()
        self.img_bytes = img_bytes
        self.preview_w_pt = preview_w_pt
        self.preview_h_pt = preview_h_pt
        self.real_w_mm = real_w_mm
        self.real_h_mm = real_h_mm
        self.box_name = box_name
        self.bleed_w_mm = bleed_w_mm
        self.bleed_h_mm = bleed_h_mm
        self.cfg = cfg

        self.margin_right = 30
        self.margin_bottom = 30
        if bleed_w_mm:
            self.margin_bottom = 55

        self.width = self.preview_w_pt + self.margin_right
        self.height = self.preview_h_pt + self.margin_bottom

    def wrap(self, availWidth, availHeight):
        return (self.width, self.height)

    def draw(self):
        """Draw onto self.canv at origin (0,0) = bottom-left of allocated space."""
        c = self.canv
        img_x = 0
        img_y = self.margin_bottom

        from reportlab.lib.utils import ImageReader
        img_reader = ImageReader(io.BytesIO(self.img_bytes))
        c.drawImage(
            img_reader,
            img_x, img_y,
            width=self.preview_w_pt,
            height=self.preview_h_pt,
            preserveAspectRatio=True,
            anchor="sw",
        )

        # Thin border
        c.saveState()
        c.setStrokeColor(colors.HexColor("#cccccc"))
        c.setLineWidth(0.3)
        c.rect(img_x, img_y, self.preview_w_pt, self.preview_h_pt, fill=0)
        c.restoreState()

        # Dimension lines
        dim = _DimensionDrawing(
            preview_x_pt=img_x,
            preview_y_pt=img_y,
            preview_w_pt=self.preview_w_pt,
            preview_h_pt=self.preview_h_pt,
            real_w_mm=self.real_w_mm,
            real_h_mm=self.real_h_mm,
            box_name=self.box_name,
            bleed_w_mm=self.bleed_w_mm,
            bleed_h_mm=self.bleed_h_mm,
            cfg=self.cfg,
        )
        dim.draw_on_canvas(c)


# ── MaterialFlowable: fixed-height text, font shrink + truncate ──

class _MaterialFlowable(Flowable):
    """Material & Ausführung text block that fits within a fixed max height.

    Strategy:
    1. Try rendering at 9pt.
    2. If too tall, shrink font down to _MATERIAL_MIN_FONT_PT.
    3. If still too tall, truncate text and append "...".
    Never overflows to a new page.
    """

    def __init__(self, text: str, avail_width_pt: float, max_height_pt: float):
        super().__init__()
        self.raw_text = text
        self.avail_width = avail_width_pt
        self.max_height = max_height_pt
        self._built_story: list[Any] = []
        self._actual_h = 0.0

    def _make_paragraph(self, text: str, font_size: float) -> list[Any]:
        styles = getSampleStyleSheet()
        head_style = ParagraphStyle(
            "MatH", parent=styles["Normal"],
            fontSize=font_size, leading=font_size + 2,
            fontName="Helvetica-Bold",
        )
        body_style = ParagraphStyle(
            "MatB", parent=styles["Normal"],
            fontSize=font_size, leading=font_size + 2,
        )
        text_html = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br/>")
        )
        return [
            Paragraph("Material &amp; Ausf\u00fchrung:", head_style),
            Spacer(1, 1.5 * mm),
            Paragraph(text_html, body_style),
        ]

    def _measure(self, parts: list[Any]) -> float:
        total = 0.0
        for p in parts:
            _, h = p.wrap(self.avail_width, 100000)
            total += h
        return total

    def wrap(self, availWidth, availHeight):
        self.avail_width = availWidth
        text = self.raw_text

        # Try font sizes from 9 down to minimum
        for fs in [9, 8, _MATERIAL_MIN_FONT_PT]:
            parts = self._make_paragraph(text, fs)
            h = self._measure(parts)
            if h <= self.max_height:
                self._built_story = parts
                self._actual_h = h
                return (availWidth, h)

        # Still too tall – truncate text progressively at minimum font
        for cut in range(len(text) - 10, 20, -20):
            truncated = text[:cut].rstrip() + " \u2026"
            parts = self._make_paragraph(truncated, _MATERIAL_MIN_FONT_PT)
            h = self._measure(parts)
            if h <= self.max_height:
                self._built_story = parts
                self._actual_h = h
                return (availWidth, h)

        # Extreme fallback – very short
        parts = self._make_paragraph(text[:50] + " \u2026", _MATERIAL_MIN_FONT_PT)
        h = self._measure(parts)
        self._built_story = parts
        self._actual_h = h
        return (availWidth, h)

    def draw(self):
        c = self.canv
        y = self._actual_h
        for p in self._built_story:
            w, h = p.wrap(self.avail_width, 100000)
            y -= h
            p.drawOn(c, 0, y)


# ══════════════════════════════════════════════════════════
# BUILD SUMMARY PAGE (always exactly 1 page)
# ══════════════════════════════════════════════════════════

def _build_summary_pdf(result: PreflightResult, request: ProofRequest) -> bytes:
    """Build a 1-page compact summary with traffic-light overview + action items."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=20 * mm, bottomMargin=15 * mm,
    )
    styles = getSampleStyleSheet()
    story: list[Any] = []

    h1 = ParagraphStyle("SH1", parent=styles["Heading1"], fontSize=16, spaceAfter=4 * mm)
    h2 = ParagraphStyle("SH2", parent=styles["Heading2"], fontSize=11, spaceAfter=2 * mm)
    body = ParagraphStyle("SBody", parent=styles["Normal"], fontSize=9, leading=12)
    small = ParagraphStyle("SSmall", parent=body, fontSize=8, leading=10,
                           textColor=colors.HexColor("#555555"))

    ts = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e0e0e0")),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ])

    # ── Meta ───────────────────────────────────────────
    story.append(Paragraph("Preflight Summary", h1))
    date_str = request.date or date.today().strftime("%d.%m.%Y")
    story.append(Paragraph(
        f"Kunde: <b>{request.customer_name}</b> &nbsp;|&nbsp; "
        f"Auftrag: <b>{request.order_number}</b> &nbsp;|&nbsp; "
        f"Version: <b>{request.version_number}</b> &nbsp;|&nbsp; "
        f"Datum: <b>{date_str}</b>",
        body,
    ))
    if request.quantity:
        story.append(Paragraph(f"Menge: <b>{request.quantity} Stk.</b>", body))
    # Maßstab
    scale = getattr(result, "scale", 1)
    if scale > 1:
        story.append(Paragraph(f"Maßstab: <b>1:{scale}</b>", body))
    if request.bleed_mm > 0:
        effective_bleed = request.bleed_mm / scale
        if scale > 1:
            story.append(Paragraph(
                f"Soll-Bleed: <b>{request.bleed_mm:.1f} mm</b> (im PDF: <b>{effective_bleed:.2f} mm</b>)", body
            ))
        else:
            story.append(Paragraph(f"Soll-Bleed: <b>{request.bleed_mm:.1f} mm</b>", body))
    else:
        story.append(Paragraph("Bleed-Check: <b>deaktiviert</b>", body))
    # Product profile info
    if request.product_profile:
        profile_parts = [f"Produktprofil: <b>{request.product_profile}</b>"]
        if request.safe_margin_mm > 0:
            profile_parts.append(f"Sicherheitsabstand: <b>{request.safe_margin_mm:.1f} mm</b>")
        if request.has_drill_holes:
            profile_parts.append("Bohrungen: <b>aktiv</b>")
        story.append(Paragraph(" &nbsp;|&nbsp; ".join(profile_parts), body))
    story.append(Paragraph(
        f"Datei: <b>{result.filename}</b> &nbsp;|&nbsp; Seiten: <b>{result.total_pages}</b>",
        body,
    ))
    story.append(Spacer(1, 2 * mm))

    # Overall status
    story.append(Paragraph(f"Gesamtstatus: {_status_html(result.overall_status)}", body))
    story.append(Spacer(1, 4 * mm))

    # ── Document info per page ─────────────────────────
    story.append(Paragraph("Dokument", h2))
    for ps in result.page_sizes:
        box_name = "MediaBox"
        for b in ps.boxes:
            if b.name == "TrimBox":
                box_name = "TrimBox"
                break
            if b.name == "CropBox":
                box_name = "CropBox"
        story.append(Paragraph(
            f"Seite {ps.page}: {ps.trim_width_mm} x {ps.trim_height_mm} mm "
            f"({box_name}) \u2013 Bleed: {_status_html(ps.bleed_status)} {ps.bleed_message}",
            small,
        ))
    story.append(Spacer(1, 4 * mm))

    # ── Traffic-light table ────────────────────────────
    story.append(Paragraph("Pr\u00fcf\u00fcbersicht", h2))

    fail_imgs = [img for img in result.images if img.status == RuleStatus.FAIL]
    warn_imgs = [img for img in result.images if img.status == RuleStatus.WARN]
    lowest_dpi = min((img.effective_dpi for img in result.images), default=0)
    img_status = RuleStatus.PASS
    if fail_imgs:
        img_status = RuleStatus.FAIL
    elif warn_imgs:
        img_status = RuleStatus.WARN

    # Aggregate bleed status
    bleed_statuses = [ps.bleed_status for ps in result.page_sizes]
    if RuleStatus.FAIL in bleed_statuses:
        bleed_agg = RuleStatus.FAIL
    elif RuleStatus.WARN in bleed_statuses:
        bleed_agg = RuleStatus.WARN
    else:
        bleed_agg = RuleStatus.PASS

    bleed_target = f"{request.bleed_mm:.0f}" if request.bleed_mm > 0 else "deaktiviert"

    rows = [["Pr\u00fcfung", "Status", "Details"]]
    rows.append(["Boxen / Format", bleed_agg.value,
                 f"{result.total_pages} Seite(n)"])
    rows.append([f"Bleed (Soll {bleed_target} mm)", bleed_agg.value,
                 "; ".join(ps.bleed_message for ps in result.page_sizes if ps.bleed_message)])
    # images_total_count = instances (placements), not unique xrefs
    total_instances = result.images_total_count
    fail_instances = len(fail_imgs)
    rows.append(["Bildaufl\u00f6sung (min 72 DPI)", img_status.value,
                 f"Instanzen: {total_instances}, "
                 f"FAIL: {fail_instances}, "
                 f"niedrigste: {lowest_dpi:.0f} DPI"])

    # Fonts row
    if result.font_check:
        fc = result.font_check
        font_detail = f"{fc.total_fonts} Schriften"
        if fc.not_embedded_count > 0:
            font_detail += f", {fc.not_embedded_count} nicht eingebettet"
        if fc.type3_count > 0:
            font_detail += f", {fc.type3_count} Type3"
        rows.append(["Schriften", fc.status.value, font_detail])

    # Spotcolors row
    if result.spot_color_list:
        scl = result.spot_color_list
        sc_detail = f"{scl.total_count} Spotfarbe(n)"
        if scl.spot_colors:
            names = ", ".join(s.name for s in scl.spot_colors[:5])
            if scl.total_count > 5:
                names += " ..."
            sc_detail += f": {names}"
        rows.append(["Spotfarben", scl.status.value, sc_detail])

    # RGB row
    if result.rgb_check:
        rc = result.rgb_check
        rows.append(["RGB / CMYK", rc.status.value, rc.message])

    # Min size row (profile-specific)
    if result.min_size:
        ms = result.min_size
        ms_detail = f"Minimum: {ms.min_width_mm:.0f} × {ms.min_height_mm:.0f} mm"
        if ms.pages_failed:
            ms_detail += f" – Seite(n) {', '.join(str(p) for p in ms.pages_failed)} zu klein"
        rows.append(["Mindestgröße", ms.status.value, ms_detail])

    # Safe area row
    if result.safe_area:
        sa = result.safe_area
        sa_detail = f"Rand: {sa.safe_margin_mm:.1f} mm"
        if sa.violations:
            sa_detail += f" – {len(sa.violations)} Überschreitung(en)"
        if sa.detection_limited:
            sa_detail += " (Erkennung eingeschränkt)"
        rows.append(["Sicherheitsabstand", sa.status.value, sa_detail])

    # Overprint row
    if result.overprint_check:
        oc = result.overprint_check
        if oc.overprint_used:
            oc_detail = f"Überdrucken auf Seite(n): {', '.join(str(p) for p in oc.pages_with_overprint)}"
        else:
            oc_detail = "Kein Überdrucken erkannt"
        rows.append(["Überdrucken", oc.status.value, oc_detail])

    # Contour / Die rows
    if result.contour_check_enabled:
        cc_status = result.cut_contour.status if result.cut_contour else RuleStatus.FAIL
        if result.cut_contour and result.cut_contour.found:
            cc_name = (
                result.cut_contour.spot_color.name
                if result.cut_contour.spot_color else "Gefunden"
            )
            cc_detail = f"{cc_name} \u2013 OK" if cc_status == RuleStatus.PASS else cc_name
        else:
            cc_detail = "Nicht gefunden"
        rows.append(["Konturschnitt", cc_status.value, cc_detail])
        die_status = result.die.status if result.die else RuleStatus.FAIL
        rows.append(["Stanze / Die", die_status.value,
                     "Gefunden" if (result.die and result.die.found) else "Nicht gefunden"])
    else:
        rows.append(["Konturschnitt / Stanze", "\u2013", "deaktiviert"])

    # Drill holes row
    if result.drill_holes_check_enabled:
        if result.drill_holes:
            dh = result.drill_holes
            if dh.found:
                dh_detail = f"{dh.total_count} Bohrung(en) erkannt"
                fail_holes = [h for h in dh.holes if h.status == RuleStatus.FAIL]
                warn_holes = [h for h in dh.holes if h.status == RuleStatus.WARN]
                if fail_holes:
                    dh_detail += f", {len(fail_holes)} FAIL"
                if warn_holes:
                    dh_detail += f", {len(warn_holes)} WARN"
            elif dh.separation_present:
                dh_detail = "Separation vorhanden, Pfade nicht auswertbar"
            else:
                dh_detail = "Sonderfarbe 'Bohrungen' nicht gefunden"
            rows.append(["Bohrungen", dh.status.value, dh_detail])
        else:
            rows.append(["Bohrungen", RuleStatus.FAIL.value, "Prüfung fehlgeschlagen"])
    else:
        rows.append(["Bohrungen", "\u2013", "deaktiviert"])

    t = Table(rows, colWidths=[42 * mm, 18 * mm, None])
    # Colour status cells
    status_style = list(ts.getCommands())
    for row_idx in range(1, len(rows)):
        val = rows[row_idx][1]
        if val == "FAIL":
            status_style.append(("TEXTCOLOR", (1, row_idx), (1, row_idx), _STATUS_COLORS[RuleStatus.FAIL]))
        elif val == "WARN":
            status_style.append(("TEXTCOLOR", (1, row_idx), (1, row_idx), _STATUS_COLORS[RuleStatus.WARN]))
        elif val == "PASS":
            status_style.append(("TEXTCOLOR", (1, row_idx), (1, row_idx), _STATUS_COLORS[RuleStatus.PASS]))
    t.setStyle(TableStyle(status_style))
    story.append(t)
    story.append(Spacer(1, 4 * mm))

    # ── Action Items (max 5, only WARN/FAIL) ───────────
    action_items: list[str] = []
    for ps in result.page_sizes:
        if ps.bleed_status != RuleStatus.PASS and ps.bleed_message:
            action_items.append(f"Seite {ps.page}: {ps.bleed_message}")
    if fail_imgs:
        action_items.append(
            f"{len(fail_imgs)} Bild-Instanz(en) unter 72 DPI "
            f"(niedrigste: {lowest_dpi:.0f} DPI)"
        )
    if result.font_check and result.font_check.not_embedded_count > 0:
        action_items.append(
            f"{result.font_check.not_embedded_count} Schrift(en) nicht eingebettet"
        )
    if result.font_check and result.font_check.type3_count > 0:
        action_items.append(
            f"{result.font_check.type3_count} Type3-Schrift(en) gefunden"
        )
    if result.rgb_check and result.rgb_check.has_device_rgb:
        action_items.append(
            f"DeviceRGB auf {len(result.rgb_check.pages_with_rgb)} Seite(n)"
        )
    if result.rgb_check and result.rgb_check.has_device_gray:
        action_items.append(
            f"DeviceGray auf {len(result.rgb_check.pages_with_gray)} Seite(n)"
        )
    if result.contour_check_enabled and result.cut_contour and not result.cut_contour.found:
        action_items.append("Konturschnitt: Spotfarbe nicht gefunden")
    if result.contour_check_enabled and result.die and not result.die.found:
        action_items.append("Stanze/Die: nicht gefunden")
    # New check action items
    if result.min_size and result.min_size.status == RuleStatus.FAIL:
        action_items.append(
            f"Mindestgröße: Seite(n) {', '.join(str(p) for p in result.min_size.pages_failed)} "
            f"unter {result.min_size.min_width_mm:.0f}×{result.min_size.min_height_mm:.0f} mm"
        )
    if result.safe_area and result.safe_area.violations:
        action_items.append(
            f"Sicherheitsabstand: {len(result.safe_area.violations)} Überschreitung(en)"
        )
    if result.overprint_check and result.overprint_check.overprint_used:
        action_items.append(
            f"Überdrucken auf {len(result.overprint_check.pages_with_overprint)} Seite(n) – manuelle Prüfung empfohlen"
        )
    if result.drill_holes and result.drill_holes.status == RuleStatus.FAIL:
        action_items.append(
            "; ".join(result.drill_holes.messages[:2])
        )
    if result.spot_color_list and result.spot_color_list.disallowed_spots:
        action_items.append(
            f"Nicht erlaubte Spotfarben: {', '.join(result.spot_color_list.disallowed_spots)}"
        )

    if action_items:
        story.append(Paragraph("Handlungsbedarf", h2))
        for item in action_items[:5]:
            story.append(Paragraph(f"\u2022 {item}", small))
        if len(action_items) > 5:
            story.append(Paragraph(f"... und {len(action_items) - 5} weitere", small))

    doc.build(story)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════
# BUILD PREVIEW PAGES (1 per customer page)
# ══════════════════════════════════════════════════════════

def _build_preview_pdf(
    customer_pdf_path: str,
    result: PreflightResult,
    request: ProofRequest,
    cfg: dict[str, Any],
) -> bytes:
    """Render each page of the customer PDF as a preview with dimension lines.

    material_execution text is placed on the SAME page as the first preview,
    using font shrink + truncation to guarantee no page overflow.
    """
    pcfg = cfg.get("preview", {})
    max_w_mm = pcfg.get("max_width_mm", 160)
    max_h_mm = pcfg.get("max_height_mm", 220)
    render_dpi = min(pcfg.get("render_dpi", 72), _MAX_PREVIEW_DPI)

    max_w_pt = max_w_mm * MM_TO_PT
    max_h_pt = max_h_mm * MM_TO_PT

    buf = io.BytesIO()
    doc_rl = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=20 * mm,
        bottomMargin=15 * mm,
    )

    styles = getSampleStyleSheet()
    h2 = ParagraphStyle("PH2", parent=styles["Heading2"], fontSize=12, spaceAfter=2 * mm)
    body = ParagraphStyle("PBody", parent=styles["Normal"], fontSize=9, leading=12)

    # Available content width for material text
    page_w = A4[0] - 15 * mm - 15 * mm

    customer_doc = fitz.open(customer_pdf_path)
    story: list[Any] = []

    for page_num in range(len(customer_doc)):
        page = customer_doc[page_num]
        ref_box, box_name = _get_reference_box(page)
        bleed_box = _get_bleed_box(page)

        real_w_mm = round(ref_box.width * PT_TO_MM, 1)
        real_h_mm = round(ref_box.height * PT_TO_MM, 1)
        bleed_w_mm = round(bleed_box.width * PT_TO_MM, 1) if bleed_box else None
        bleed_h_mm = round(bleed_box.height * PT_TO_MM, 1) if bleed_box else None

        scale = min(max_w_pt / ref_box.width, max_h_pt / ref_box.height, 1.0)
        preview_w_pt = ref_box.width * scale
        preview_h_pt = ref_box.height * scale

        jpeg_bytes = _render_page_preview(customer_doc, page_num, render_dpi, clip_rect=ref_box)

        if page_num > 0:
            story.append(PageBreak())

        # ── Page header ─────────────────────────────────
        story.append(Paragraph(f"Seitenvorschau \u2013 Seite {page_num + 1}", h2))

        info_parts = [
            f"Referenz: <b>{box_name}</b>",
            f"Format: {real_w_mm} x {real_h_mm} mm",
        ]
        if bleed_w_mm:
            info_parts.append(f"BleedBox: {bleed_w_mm} x {bleed_h_mm} mm")
        if request.quantity:
            info_parts.append(f"<b>Menge: {request.quantity} Stk.</b>")

        story.append(Paragraph(" &nbsp;|&nbsp; ".join(info_parts), body))
        story.append(Spacer(1, 4 * mm))

        # ── Preview + dimension lines flowable ──────────
        preview = PreviewFlowable(
            img_bytes=jpeg_bytes,
            preview_w_pt=preview_w_pt,
            preview_h_pt=preview_h_pt,
            real_w_mm=real_w_mm,
            real_h_mm=real_h_mm,
            box_name=box_name,
            bleed_w_mm=bleed_w_mm,
            bleed_h_mm=bleed_h_mm,
            cfg=cfg,
        )
        story.append(preview)

        # ── Material & Ausführung (first page only, fixed-height) ──
        if page_num == 0 and request.material_execution:
            story.append(Spacer(1, 4 * mm))
            mat = _MaterialFlowable(
                request.material_execution,
                avail_width_pt=page_w,
                max_height_pt=_MATERIAL_MAX_HEIGHT_PT,
            )
            story.append(mat)

    customer_doc.close()
    doc_rl.build(story)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════
# BUILD DETAIL PAGES (FAIL/WARN only, compact)
# ══════════════════════════════════════════════════════════

def _group_fail_images(result: PreflightResult) -> list[dict[str, Any]]:
    """Group FAIL/WARN images by (page, pixel_size, placed_size_mm, rounded_dpi).

    Returns list of dicts with a 'count' field for identical instances.
    Sorted by effective_dpi ascending, then page ascending.
    """
    fail_warn = [img for img in result.images if img.status in (RuleStatus.FAIL, RuleStatus.WARN)]

    groups: dict[tuple, dict[str, Any]] = {}
    for img in fail_warn:
        key = (
            img.page,
            img.pixel_width, img.pixel_height,
            round(img.width_mm, 1), round(img.height_mm, 1),
            round(img.effective_dpi),
        )
        if key in groups:
            groups[key]["count"] += 1
        else:
            groups[key] = {
                "page": img.page,
                "pixel_width": img.pixel_width,
                "pixel_height": img.pixel_height,
                "width_mm": round(img.width_mm, 1),
                "height_mm": round(img.height_mm, 1),
                "effective_dpi": round(img.effective_dpi),
                "min_dpi": img.min_dpi,
                "status": img.status,
                "count": 1,
            }

    # Sort: worst DPI first, then page
    sorted_groups = sorted(groups.values(), key=lambda g: (g["effective_dpi"], g["page"]))
    return sorted_groups


def _build_detail_pdf(
    result: PreflightResult,
    request: ProofRequest,
    has_overflow: bool,
    customer_pdf_path: str = "",
) -> Optional[bytes]:
    """Build 0-2 pages showing only FAIL/WARN entries.

    Returns None if there are no FAIL/WARN entries at all.
    """
    grouped = _group_fail_images(result)

    # Also check contour/die for FAIL/WARN
    has_contour_issues = (
        result.contour_check_enabled
        and result.cut_contour is not None
        and result.cut_contour.status != RuleStatus.PASS
    )
    has_die_issues = (
        result.contour_check_enabled
        and result.die is not None
        and result.die.status != RuleStatus.PASS
    )

    # Check bleed issues
    bleed_issues = [ps for ps in result.page_sizes if ps.bleed_status != RuleStatus.PASS]

    # Check font issues
    has_font_issues = (
        result.font_check is not None
        and result.font_check.status != RuleStatus.PASS
    )

    # Check RGB issues
    has_rgb_issues = (
        result.rgb_check is not None
        and result.rgb_check.status != RuleStatus.PASS
    )

    # Check spotcolor issues
    has_spot_issues = (
        result.spot_color_list is not None
        and result.spot_color_list.status != RuleStatus.PASS
    )

    # Check new rule issues
    has_min_size_issues = (
        result.min_size is not None
        and result.min_size.status != RuleStatus.PASS
    )
    has_safe_area_issues = (
        result.safe_area is not None
        and result.safe_area.status != RuleStatus.PASS
    )
    has_overprint_issues = (
        result.overprint_check is not None
        and result.overprint_check.status != RuleStatus.PASS
    )
    has_drill_issues = (
        result.drill_holes is not None
        and result.drill_holes.status != RuleStatus.PASS
    )

    if (not grouped and not has_contour_issues and not has_die_issues
            and not bleed_issues and not has_font_issues and not has_rgb_issues
            and not has_spot_issues and not has_min_size_issues
            and not has_safe_area_issues and not has_overprint_issues
            and not has_drill_issues):
        return None

    # Open customer PDF for cut contour page previews (closed at the end)
    _cc_fitz_doc: Optional[fitz.Document] = None
    if customer_pdf_path and has_contour_issues:
        try:
            _cc_fitz_doc = fitz.open(customer_pdf_path)
        except Exception as exc:
            logger.debug("Could not open customer PDF for CC preview: %s", exc)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=20 * mm, bottomMargin=15 * mm,
    )
    styles = getSampleStyleSheet()
    story: list[Any] = []

    h2 = ParagraphStyle("DH2", parent=styles["Heading2"], fontSize=11, spaceAfter=2 * mm)
    body = ParagraphStyle("DBody", parent=styles["Normal"], fontSize=8, leading=10)
    note = ParagraphStyle("DNote", parent=body, textColor=colors.HexColor("#888888"), fontSize=7)

    ts = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e0e0e0")),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ])

    story.append(Paragraph("Preflight Details (nur Abweichungen)", h2))
    story.append(Spacer(1, 2 * mm))

    # ── Bleed issues ──────────────────────────────────
    if bleed_issues:
        story.append(Paragraph("Bleed-Abweichungen", h2))
        for ps in bleed_issues:
            story.append(Paragraph(
                f"Seite {ps.page}: {_status_html(ps.bleed_status)} \u2013 {ps.bleed_message}",
                body,
            ))
        story.append(Spacer(1, 3 * mm))

    # ── Image DPI issues (grouped) ────────────────────
    if grouped:
        story.append(Paragraph("Bildaufl\u00f6sung \u2013 Abweichungen", h2))

        display_rows = grouped[:_MAX_DETAIL_ROWS]
        rows = [["Seite", "Pixel (px)", "Platzierung (mm)", "Eff. DPI", "Min. DPI", "Status", "Anz."]]
        for g in display_rows:
            rows.append([
                str(g["page"]),
                f"{g['pixel_width']} x {g['pixel_height']}",
                f"{g['width_mm']} x {g['height_mm']}",
                str(g["effective_dpi"]),
                str(g["min_dpi"]),
                g["status"].value,
                str(g["count"]),
            ])

        t = Table(rows, colWidths=[12 * mm, 24 * mm, 26 * mm, 16 * mm, 16 * mm, 14 * mm, 10 * mm])
        t.setStyle(ts)
        story.append(t)

        if len(grouped) > _MAX_DETAIL_ROWS:
            overflow_count = len(grouped) - _MAX_DETAIL_ROWS
            story.append(Spacer(1, 2 * mm))
            story.append(Paragraph(
                f"Weitere {overflow_count} Gruppe(n) "
                "in Detail-Datei (preflight_details.json).",
                note,
            ))
        elif grouped:
            # Even when not truncated, mention the detail file exists
            story.append(Spacer(1, 2 * mm))
            story.append(Paragraph(
                "Vollst\u00e4ndige Liste: preflight_details.json",
                note,
            ))
        story.append(Spacer(1, 3 * mm))

    # ── Font issues ──────────────────────────────────
    if has_font_issues and result.font_check:
        fc = result.font_check
        story.append(Paragraph("Schriften \u2013 Abweichungen", h2))
        for msg in fc.messages:
            story.append(Paragraph(f"\u2022 {msg}", body))
        # List problematic fonts (max 20)
        problem_fonts = [f for f in fc.fonts if f.status != RuleStatus.PASS]
        if problem_fonts:
            font_rows = [["Schrift", "Seite", "Status", "Hinweis"]]
            for f in problem_fonts[:20]:
                font_rows.append([
                    f.name[:40],
                    str(f.page),
                    f.status.value,
                    "nicht eingebettet" if not f.is_embedded else ("Type3" if f.is_type3 else ""),
                ])
            ft = Table(font_rows, colWidths=[50 * mm, 12 * mm, 14 * mm, None])
            ft.setStyle(ts)
            story.append(ft)
            if len(problem_fonts) > 20:
                story.append(Paragraph(
                    f"... und {len(problem_fonts) - 20} weitere",
                    note,
                ))
        story.append(Spacer(1, 3 * mm))

    # ── RGB issues ───────────────────────────────────
    if has_rgb_issues and result.rgb_check:
        rc = result.rgb_check
        story.append(Paragraph("RGB / CMYK \u2013 Hinweis", h2))
        story.append(Paragraph(f"\u2022 {rc.message}", body))
        story.append(Spacer(1, 3 * mm))

    # ── Spotcolor issues ─────────────────────────────
    if has_spot_issues and result.spot_color_list:
        scl = result.spot_color_list
        story.append(Paragraph("Spotfarben \u2013 Hinweis", h2))
        for msg in scl.messages:
            story.append(Paragraph(f"\u2022 {msg}", body))
        story.append(Spacer(1, 3 * mm))

    # ── Contour / Die issues ──────────────────────────
    if has_contour_issues and result.cut_contour:
        cc = result.cut_contour
        story.append(Paragraph(f"Konturschnitt: {_status_html(cc.status)}", h2))

        # Properties table
        _MAX_STROKE_PT = 0.25
        cc_rows: list[list[str]] = [["Eigenschaft", "Wert", "Status"]]
        if cc.spot_color:
            cc_rows.append(["Spot Color", cc.spot_color.name, "\u2713"])
            if cc.spot_color.cmyk is not None:
                cmyk_str = "/".join(f"{int(v)}" for v in cc.spot_color.cmyk)
                mag = cc.spot_color.is_expected_magenta
                cc_rows.append([
                    "CMYK (Magenta)", cmyk_str,
                    "\u2713" if mag else ("\u003f" if mag is None else "\u2717"),
                ])
        if cc.stroke_width_pt is not None:
            ok = cc.stroke_width_pt <= _MAX_STROKE_PT
            cc_rows.append([
                "Strichst\u00e4rke",
                f"{cc.stroke_width_pt:.3f} pt",
                "\u2713" if ok else "\u2717",
            ])
        if cc.is_overprint is not None:
            cc_rows.append([
                "\u00dcberdrucken",
                "Ja" if cc.is_overprint else "Nein",
                "\u2713" if cc.is_overprint else "\u2717",
            ])
        if cc.is_unfilled is not None:
            cc_rows.append([
                "Pfad ungef\u00fcllt",
                "Ja" if cc.is_unfilled else "Nein",
                "\u2713" if cc.is_unfilled else "\u2717",
            ])
        if cc.is_closed is not None:
            cc_rows.append([
                "Pfad geschlossen",
                "Ja" if cc.is_closed else "Nein",
                "\u2713" if cc.is_closed else "\u2717",
            ])

        if len(cc_rows) > 1:
            cc_tbl = Table(cc_rows, colWidths=[45 * mm, 50 * mm, 12 * mm])
            cc_tbl.setStyle(ts)
            story.append(cc_tbl)
            story.append(Spacer(1, 2 * mm))

        for m in cc.messages:
            story.append(Paragraph(f"\u2022 {m}", body))

        # Page previews
        if cc.pages and _cc_fitz_doc is not None:
            story.append(Spacer(1, 3 * mm))
            for page_num in cc.pages[:3]:
                if page_num < 1 or page_num > len(_cc_fitz_doc):
                    continue
                page_fitz = _cc_fitz_doc[page_num - 1]
                rect = page_fitz.rect
                max_w_pt = 120.0 * MM_TO_PT
                scale = min(1.0, max_w_pt / max(rect.width, 1.0))
                preview_w_pt = rect.width * scale
                preview_h_pt = rect.height * scale
                jpeg_bytes = _render_page_preview(_cc_fitz_doc, page_num - 1)
                rl_img = _RLImage(
                    io.BytesIO(jpeg_bytes),
                    width=preview_w_pt,
                    height=preview_h_pt,
                )
                story.append(Paragraph(f"Seite {page_num}", note))
                story.append(rl_img)
                story.append(Spacer(1, 3 * mm))
            if len(cc.pages) > 3:
                story.append(Paragraph(
                    f"... und {len(cc.pages) - 3} weitere Seiten", note,
                ))

        story.append(Spacer(1, 3 * mm))

    if has_die_issues and result.die:
        d = result.die
        story.append(Paragraph(f"Stanze / Die: {_status_html(d.status)}", h2))
        for m in d.messages:
            story.append(Paragraph(f"\u2022 {m}", body))
        story.append(Spacer(1, 3 * mm))

    # ── Min size issues ──────────────────────────────
    if has_min_size_issues and result.min_size:
        ms = result.min_size
        story.append(Paragraph(f"Mindestgröße: {_status_html(ms.status)}", h2))
        story.append(Paragraph(f"\u2022 {ms.message}", body))
        if ms.pages_detail:
            size_rows = [["Seite", "Breite (mm)", "Höhe (mm)", "Status"]]
            for pd in ms.pages_detail:
                pg = pd.get("page", "?")
                w = pd.get("w_mm", 0)
                h = pd.get("h_mm", 0)
                st = "FAIL" if pg in ms.pages_failed else "PASS"
                size_rows.append([str(pg), f"{w:.1f}", f"{h:.1f}", st])
            st_tbl = Table(size_rows, colWidths=[14 * mm, 26 * mm, 26 * mm, 14 * mm])
            st_tbl.setStyle(ts)
            story.append(st_tbl)
        story.append(Spacer(1, 3 * mm))

    # ── Safe area issues ─────────────────────────────
    if has_safe_area_issues and result.safe_area:
        sa = result.safe_area
        story.append(Paragraph(f"Sicherheitsabstand ({sa.safe_margin_mm:.1f} mm): {_status_html(sa.status)}", h2))
        for msg in sa.messages:
            story.append(Paragraph(f"\u2022 {msg}", body))
        if sa.violations:
            sa_rows = [["Seite", "Objekt", "Überschreitung (mm)"]]
            for v in sa.violations[:20]:
                sa_rows.append([str(v.page), v.description[:40], f"{v.overflow_mm:.1f}"])
            sa_tbl = Table(sa_rows, colWidths=[14 * mm, 50 * mm, 30 * mm])
            sa_tbl.setStyle(ts)
            story.append(sa_tbl)
            if len(sa.violations) > 20:
                story.append(Paragraph(
                    f"... und {len(sa.violations) - 20} weitere Überschreitungen",
                    note,
                ))
        story.append(Spacer(1, 3 * mm))

    # ── Overprint issues ─────────────────────────────
    if has_overprint_issues and result.overprint_check:
        oc = result.overprint_check
        story.append(Paragraph(f"Überdrucken: {_status_html(oc.status)}", h2))
        story.append(Paragraph(f"\u2022 {oc.message}", body))
        story.append(Spacer(1, 3 * mm))

    # ── Drill holes issues ───────────────────────────
    if has_drill_issues and result.drill_holes:
        dh = result.drill_holes
        story.append(Paragraph(f"Bohrungen: {_status_html(dh.status)}", h2))
        for msg in dh.messages:
            story.append(Paragraph(f"\u2022 {msg}", body))
        if dh.holes:
            dh_rows = [["ID", "Seite", "⌀ (mm)", "Rand (mm)", "Rund", "Status", "Hinweis"]]
            for h in dh.holes[:30]:
                dh_rows.append([
                    h.hole_id,
                    str(h.page),
                    f"{h.diameter_mm:.1f}",
                    f"{h.edge_distance_mm:.1f}",
                    "Ja" if h.is_circular else "Nein",
                    h.status.value,
                    (h.note[:25] + "…") if len(h.note) > 25 else h.note,
                ])
            dh_tbl = Table(dh_rows, colWidths=[10 * mm, 12 * mm, 14 * mm, 16 * mm, 12 * mm, 14 * mm, None])
            dh_tbl.setStyle(ts)
            story.append(dh_tbl)
            if len(dh.holes) > 30:
                story.append(Paragraph(
                    f"... und {len(dh.holes) - 30} weitere Bohrungen",
                    note,
                ))
        story.append(Spacer(1, 3 * mm))

    doc.build(story)
    if _cc_fitz_doc is not None:
        _cc_fitz_doc.close()
    return buf.getvalue()


# ══════════════════════════════════════════════════════════
# GENERATE DETAIL JSON (overflow file)
# ══════════════════════════════════════════════════════════

def _generate_detail_json(result: PreflightResult, output_path: Path) -> Optional[Path]:
    """Write preflight_details.json with full FAIL/WARN image list.

    The file is written whenever the grouped list was truncated in the PDF
    detail table (> _MAX_DETAIL_ROWS) **or** whenever there are any
    FAIL/WARN image entries at all (so the user always has a complete
    machine-readable file and there is never a "silent truncation").

    Returns the path if written, None if nothing to report.
    """
    fail_warn = [img for img in result.images if img.status in (RuleStatus.FAIL, RuleStatus.WARN)]
    grouped = _group_fail_images(result)

    if not fail_warn:
        return None

    detail_path = output_path.parent / "preflight_details.json"
    detail_data = {
        "filename": result.filename,
        "total_pages": result.total_pages,
        "images_total_instances": result.images_total_count,
        "images_fail_warn_instances": len(fail_warn),
        "grouped_count": len(grouped),
        "pdf_table_truncated": len(grouped) > _MAX_DETAIL_ROWS,
        "images_fail_warn": [
            {
                "page": img.page,
                "image_index": img.image_index,
                "pixel": f"{img.pixel_width}x{img.pixel_height}",
                "placed_mm": f"{img.width_mm}x{img.height_mm}",
                "effective_dpi": round(img.effective_dpi, 1),
                "min_dpi": img.min_dpi,
                "status": img.status.value,
            }
            for img in fail_warn
        ],
        "grouped_fail_warn": [
            {
                "page": g["page"],
                "pixel": f"{g['pixel_width']}x{g['pixel_height']}",
                "placed_mm": f"{g['width_mm']}x{g['height_mm']}",
                "effective_dpi": g["effective_dpi"],
                "min_dpi": g["min_dpi"],
                "status": g["status"].value,
                "count": g["count"],
            }
            for g in grouped
        ],
    }

    with open(detail_path, "w", encoding="utf-8") as f:
        json.dump(detail_data, f, ensure_ascii=False, indent=2)

    logger.info("Detail JSON written to %s", detail_path)
    return detail_path


# ══════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════

def generate_proof_pdf(
    template_path: str | Path,
    customer_pdf_path: str | Path,
    output_path: str | Path,
    result: PreflightResult,
    request: ProofRequest,
) -> Path:
    """Create proof_output.pdf (compact mode):
    1. Fill the AcroForm template with request fields.
    2. Build 1 Summary page.
    3. Build N Preview pages (1 per customer page).
    4. Build 0-2 Detail pages (FAIL/WARN only).
    5. Merge all into one file.
    6. Optionally write preflight_details.json for overflow.
    """
    cfg = load_config()
    output_path = Path(output_path)
    template_path_str = str(template_path)
    customer_pdf_str = str(customer_pdf_path)
    tmp_filled = str(output_path.with_suffix(".filled.pdf"))

    fields: dict[str, str] = {
        "customer_name": request.customer_name,
        "order_number": request.order_number,
        "version_number": request.version_number,
        "date": request.date or date.today().strftime("%d.%m.%Y"),
    }
    if request.comment:
        fields["comment"] = request.comment
    if request.quantity:
        fields["quantity"] = str(request.quantity)

    # Step 1: fill form
    _fill_form(template_path_str, tmp_filled, fields)

    # Step 2: build summary page
    summary_bytes = _build_summary_pdf(result, request)

    # Step 3: build preview pages
    preview_bytes = _build_preview_pdf(customer_pdf_str, result, request, cfg)

    # Step 4: check for overflow and build detail pages
    grouped = _group_fail_images(result)
    has_overflow = len(grouped) > _MAX_DETAIL_ROWS
    detail_bytes = _build_detail_pdf(result, request, has_overflow, customer_pdf_str)

    # Step 5: merge all – template + summary + previews + details
    doc_filled = fitz.open(tmp_filled)
    doc_summary = fitz.open(stream=summary_bytes, filetype="pdf")
    doc_preview = fitz.open(stream=preview_bytes, filetype="pdf")

    doc_filled.insert_pdf(doc_summary)
    doc_filled.insert_pdf(doc_preview)

    if detail_bytes:
        doc_detail = fitz.open(stream=detail_bytes, filetype="pdf")
        doc_filled.insert_pdf(doc_detail)
        doc_detail.close()

    doc_filled.save(str(output_path))

    doc_filled.close()
    doc_summary.close()
    doc_preview.close()

    Path(tmp_filled).unlink(missing_ok=True)

    # Step 6: write detail JSON if overflow
    _generate_detail_json(result, output_path)

    logger.info("Proof PDF written to %s", output_path)
    return output_path
