"""FastAPI application – Korrekturabzug proof generation service."""

from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import load_config
from app.generator.proof_pdf import generate_proof_pdf
from app.generator.tech_svg import generate_tech_svg
from app.models import PreflightResult, ProofRequest, ProofResponse
from app.preflight.engine import run_preflight

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
TEMPLATE_DIR = BASE_DIR / "templates"

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

TEMPLATE_FILENAME = "Korrekturabzug Hochformat_LorenzWerbung.pdf"


def _resolve_template() -> Path:
    """Return the fixed proof template path, raising HTTP 503 if missing."""
    path = TEMPLATE_DIR / TEMPLATE_FILENAME
    if path.is_file():
        return path
    # Try without .pdf extension (in case directory listing differs)
    stem = Path(TEMPLATE_FILENAME).stem
    for candidate in TEMPLATE_DIR.iterdir() if TEMPLATE_DIR.is_dir() else []:
        if candidate.stem == stem and candidate.suffix.lower() == ".pdf":
            return candidate
    raise HTTPException(
        status_code=503,
        detail=f"Proof template not found: {TEMPLATE_FILENAME}. "
        "Please place the template in the templates/ directory.",
    )

_UMLAUT_MAP = str.maketrans({
    "\u00e4": "ae", "\u00f6": "oe", "\u00fc": "ue", "\u00df": "ss",
    "\u00c4": "Ae", "\u00d6": "Oe", "\u00dc": "Ue",
})
_ILLEGAL_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_MAX_FILENAME_LEN = 120


def _sanitize_part(text: str) -> str:
    """Sanitize a single filename component: transliterate, strip illegal chars, normalise whitespace."""
    text = text.translate(_UMLAUT_MAP)
    text = _ILLEGAL_CHARS.sub("", text)
    text = re.sub(r"\s+", "_", text.strip())
    return text


def _build_output_filenames(
    order_number: str, customer_name: str, version_number: str,
) -> tuple[str, str, str]:
    """Return (proof_filename, svg_filename, jobticket_filename) built from request fields."""
    order = _sanitize_part(order_number)
    customer = _sanitize_part(customer_name)
    version = _sanitize_part(version_number)

    # Build base: <order>_<customer>_Korrekturabzug_<version>
    base_proof = f"{order}_{customer}_Korrekturabzug_{version}"
    base_tech = f"{order}_{customer}_Technik_{version}"
    base_ticket = f"{order}_{customer}_JobTicket_{version}"

    # Truncate customer name first if total exceeds limit (excl. extension)
    for base, ext in [(base_proof, ".pdf"), (base_tech, ".svg"), (base_ticket, ".json")]:
        max_stem = _MAX_FILENAME_LEN - len(ext)
        if len(base) > max_stem:
            # Recalculate how much room customer has
            overhead = len(order) + 1 + 1 + len("_Korrekturabzug_") + len(version)
            allowed = max(10, max_stem - overhead)
            customer = _sanitize_part(customer_name)[:allowed]

    proof_name = f"{order}_{customer}_Korrekturabzug_{version}.pdf"
    tech_name = f"{order}_{customer}_Technik_{version}.svg"
    ticket_name = f"{order}_{customer}_JobTicket_{version}.json"
    return proof_name, tech_name, ticket_name


app = FastAPI(
    title="Korrekturabzug",
    version="1.0.0",
    description="PDF Preflight Analysis & Proof Sheet Generator",
)

# Serve generated output files
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")

# Minimal web UI
STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def _save_upload(upload: UploadFile, dest: Path, max_bytes: int) -> Path:
    """Stream an upload to disk, enforcing a size limit."""
    written = 0
    with open(dest, "wb") as f:
        while True:
            chunk = upload.file.read(1024 * 1024)  # 1 MB chunks
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"File exceeds maximum size ({max_bytes / 1024 / 1024:.0f} MB).",
                )
            f.write(chunk)
    return dest


@app.get("/", include_in_schema=False)
def root() -> FileResponse:
    """Serve the minimal web UI at root."""
    index = BASE_DIR / "static" / "index.html"
    return FileResponse(index, media_type="text/html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _generate_jobticket(
    request: ProofRequest,
    result: PreflightResult,
    output_path: Path,
) -> None:
    """Write the JobTicket JSON file with comprehensive structure."""
    ticket = {
        "job": {
            "customer_name": request.customer_name,
            "order_number": request.order_number,
            "version_number": request.version_number,
            "date": request.date,
            "quantity": request.quantity,
            "has_contour_cut": request.has_contour_cut,
            "bleed_mm_target": request.bleed_mm,
            "material_execution": request.material_execution,
            "comment": request.comment,
            "product_profile": request.product_profile,
            "safe_margin_mm": request.safe_margin_mm,
            "has_drill_holes": request.has_drill_holes,
        },
        "preflight": {
            "filename": result.filename,
            "total_pages": result.total_pages,
            "overall_status": result.overall_status.value,
            "pages": [
                {
                    "page": ps.page,
                    "trim_width_mm": ps.trim_width_mm,
                    "trim_height_mm": ps.trim_height_mm,
                    "bleed_mm": ps.bleed_mm,
                    "bleed_status": ps.bleed_status.value,
                    "bleed_message": ps.bleed_message,
                    "boxes": [
                        {
                            "name": b.name,
                            "width_mm": b.width_mm,
                            "height_mm": b.height_mm,
                        }
                        for b in ps.boxes
                    ],
                }
                for ps in result.page_sizes
            ],
            "images": {
                "total_count": result.images_total_count,
                "fail_count": sum(1 for i in result.images if i.status.value == "FAIL"),
                "warn_count": sum(1 for i in result.images if i.status.value == "WARN"),
                "lowest_dpi": round(min((i.effective_dpi for i in result.images), default=0), 1),
            },
            "fonts": None,
            "spot_colors": None,
            "rgb_check": None,
            "cut_contour": None,
            "die": None,
            "min_size": None,
            "safe_area": None,
            "overprint": None,
            "drill_holes": None,
        },
    }

    # Font check
    if result.font_check:
        fc = result.font_check
        ticket["preflight"]["fonts"] = {
            "status": fc.status.value,
            "total_fonts": fc.total_fonts,
            "not_embedded_count": fc.not_embedded_count,
            "type3_count": fc.type3_count,
            "messages": fc.messages,
            "details": [
                {
                    "name": f.name,
                    "page": f.page,
                    "is_embedded": f.is_embedded,
                    "is_type3": f.is_type3,
                    "status": f.status.value,
                }
                for f in fc.fonts
            ],
        }

    # Spot colours
    if result.spot_color_list:
        scl = result.spot_color_list
        ticket["preflight"]["spot_colors"] = {
            "status": scl.status.value,
            "total_count": scl.total_count,
            "disallowed_spots": scl.disallowed_spots,
            "messages": scl.messages,
            "colors": [
                {
                    "name": sc.name,
                    "cmyk": sc.cmyk,
                    "pages": sc.pages,
                }
                for sc in scl.spot_colors
            ],
        }

    # RGB check
    if result.rgb_check:
        rc = result.rgb_check
        ticket["preflight"]["rgb_check"] = {
            "status": rc.status.value,
            "has_device_rgb": rc.has_device_rgb,
            "has_device_gray": rc.has_device_gray,
            "pages_with_rgb": rc.pages_with_rgb,
            "pages_with_gray": rc.pages_with_gray,
            "message": rc.message,
        }

    # Cut contour
    if result.cut_contour:
        cc = result.cut_contour
        ticket["preflight"]["cut_contour"] = {
            "found": cc.found,
            "status": cc.status.value,
            "messages": cc.messages,
            "spot_color": {
                "name": cc.spot_color.name,
                "cmyk": cc.spot_color.cmyk,
            } if cc.spot_color else None,
            "pages": cc.pages,
            "stroke_width_pt": cc.stroke_width_pt,
            "is_unfilled": cc.is_unfilled,
            "is_closed": cc.is_closed,
        }

    # Die
    if result.die:
        d = result.die
        ticket["preflight"]["die"] = {
            "found": d.found,
            "status": d.status.value,
            "name_matched": d.name_matched,
            "messages": d.messages,
        }

    # Min size
    if result.min_size:
        ms = result.min_size
        ticket["preflight"]["min_size"] = {
            "status": ms.status.value,
            "min_width_mm": ms.min_width_mm,
            "min_height_mm": ms.min_height_mm,
            "pages_failed": ms.pages_failed,
            "pages_detail": ms.pages_detail,
            "message": ms.message,
        }

    # Safe area
    if result.safe_area:
        sa = result.safe_area
        ticket["preflight"]["safe_area"] = {
            "status": sa.status.value,
            "safe_margin_mm": sa.safe_margin_mm,
            "detection_limited": sa.detection_limited,
            "violation_count": len(sa.violations),
            "messages": sa.messages,
            "violations": [
                {
                    "page": v.page,
                    "description": v.description,
                    "overflow_mm": v.overflow_mm,
                }
                for v in sa.violations[:20]
            ],
        }

    # Overprint
    if result.overprint_check:
        oc = result.overprint_check
        ticket["preflight"]["overprint"] = {
            "status": oc.status.value,
            "overprint_used": oc.overprint_used,
            "pages_with_overprint": oc.pages_with_overprint,
            "message": oc.message,
        }

    # Drill holes
    if result.drill_holes:
        dh = result.drill_holes
        ticket["preflight"]["drill_holes"] = {
            "status": dh.status.value,
            "found": dh.found,
            "separation_present": dh.separation_present,
            "extraction_limited": dh.extraction_limited,
            "total_count": dh.total_count,
            "messages": dh.messages,
            "spot_color": {
                "name": dh.spot_color.name,
                "cmyk": dh.spot_color.cmyk,
            } if dh.spot_color else None,
            "holes": [
                {
                    "hole_id": h.hole_id,
                    "page": h.page,
                    "diameter_mm": h.diameter_mm,
                    "edge_distance_mm": h.edge_distance_mm,
                    "is_circular": h.is_circular,
                    "status": h.status.value,
                    "note": h.note,
                }
                for h in dh.holes
            ],
        }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(ticket, f, ensure_ascii=False, indent=2)

    logger.info("JobTicket JSON written to %s", output_path)


@app.post("/api/proof", response_model=ProofResponse)
def create_proof(
    customer_pdf: UploadFile = File(..., description="Kundendruckdatei (PDF)"),
    customer_name: str = Form(...),
    order_number: str = Form(...),
    version_number: str = Form(...),
    quantity: int = Form(...),
    has_contour_cut: bool = Form(default=False),
    comment: str = Form(default=""),
    material_execution: str = Form(default=""),
    bleed_mm: float = Form(default=10.0),
    product_profile: str = Form(default=""),
    safe_margin_mm: float = Form(default=0.0),
    has_drill_holes: bool = Form(default=False),
    scale: int = Form(default=1),
) -> ProofResponse:
    cfg = load_config()
    max_bytes = cfg.get("max_upload_bytes", 524_288_000)
    job_id = uuid.uuid4().hex[:12]

    # ── Validate bleed_mm ────────────────────────────────
    if bleed_mm < 0 or bleed_mm > 30:
        raise HTTPException(status_code=422, detail="bleed_mm must be between 0 and 30.")

    # ── Validate safe_margin_mm ──────────────────────────
    if safe_margin_mm < 0 or safe_margin_mm > 30:
        raise HTTPException(status_code=422, detail="safe_margin_mm must be between 0 and 30.")

    # ── Validate scale ───────────────────────────────────
    if scale not in (1, 10):
        raise HTTPException(status_code=422, detail="scale must be 1 (1:1) or 10 (1:10).")

    # ── Validate product_profile ─────────────────────────
    product_profile_clean: str | None = product_profile.strip() if product_profile else None
    if product_profile_clean and product_profile_clean not in ("wahlplakate",):
        raise HTTPException(status_code=422, detail=f"Unknown product_profile: {product_profile_clean}")

    # ── Fixed template (server-side) ─────────────────────
    template_path = _resolve_template()

    # ── Save uploads ────────────────────────────────────
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    customer_path = _save_upload(customer_pdf, job_dir / "customer_input.pdf", max_bytes)

    request = ProofRequest(
        customer_name=customer_name,
        order_number=order_number,
        version_number=version_number,
        date=date.today().strftime("%d.%m.%Y"),
        comment=comment or None,
        quantity=quantity if quantity else None,
        has_contour_cut=has_contour_cut,
        material_execution=material_execution or None,
        bleed_mm=bleed_mm,
        product_profile=product_profile_clean,
        safe_margin_mm=safe_margin_mm,
        has_drill_holes=has_drill_holes,
        scale=scale,
    )

    # ── Preflight ───────────────────────────────────────
    try:
        preflight_result: PreflightResult = run_preflight(
            customer_path, cfg,
            has_contour_cut=has_contour_cut,
            bleed_mm=bleed_mm,
            product_profile=product_profile_clean,
            safe_margin_mm=safe_margin_mm,
            has_drill_holes=has_drill_holes,
            scale=scale,
        )
    except Exception as exc:
        logger.exception("Preflight failed for job %s", job_id)
        raise HTTPException(status_code=422, detail=f"Preflight analysis failed: {exc}") from exc

    # Originalen Dateinamen beibehalten
    if customer_pdf.filename:
        preflight_result.filename = customer_pdf.filename

    # ── Build output filenames ───────────────────────────
    proof_filename, tech_filename, ticket_filename = _build_output_filenames(
        order_number, customer_name, version_number,
    )

    out_dir = OUTPUT_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    proof_pdf_path = out_dir / proof_filename
    tech_svg_path = out_dir / tech_filename
    ticket_json_path = out_dir / ticket_filename

    try:
        generate_proof_pdf(template_path, customer_path, proof_pdf_path, preflight_result, request)
    except Exception as exc:
        logger.exception("Proof PDF generation failed for job %s", job_id)
        raise HTTPException(status_code=500, detail=f"Proof PDF generation failed: {exc}") from exc

    # ── Tech SVG only when has_contour_cut=true ──────────
    tech_svg_url = ""
    tech_svg_filename_out = ""
    if has_contour_cut:
        try:
            generate_tech_svg(customer_path, tech_svg_path, cfg)
            tech_svg_url = f"/output/{job_id}/{tech_filename}"
            tech_svg_filename_out = tech_filename
        except Exception as exc:
            logger.exception("SVG generation failed for job %s", job_id)
            raise HTTPException(status_code=500, detail=f"SVG generation failed: {exc}") from exc

    # ── JobTicket JSON (always) ──────────────────────────
    try:
        _generate_jobticket(request, preflight_result, ticket_json_path)
    except Exception as exc:
        logger.exception("JobTicket JSON generation failed for job %s", job_id)
        # Non-fatal: log and continue
        ticket_json_path = None

    # ── Cleanup uploads (keep outputs) ──────────────────
    shutil.rmtree(job_dir, ignore_errors=True)

    # ── Check for detail JSON (generated by proof_pdf if overflow) ──
    detail_json_path = out_dir / "preflight_details.json"
    detail_json_url = None
    detail_json_filename = None
    if detail_json_path.is_file():
        detail_json_filename = "preflight_details.json"
        detail_json_url = f"/output/{job_id}/{detail_json_filename}"

    # ── JobTicket response ────────────────────────────────
    jobticket_json_url = None
    jobticket_json_filename = None
    if ticket_json_path and ticket_json_path.is_file():
        jobticket_json_filename = ticket_filename
        jobticket_json_url = f"/output/{job_id}/{ticket_filename}"

    return ProofResponse(
        preflight=preflight_result,
        proof_pdf_url=f"/output/{job_id}/{proof_filename}",
        proof_pdf_filename=proof_filename,
        tech_svg_url=tech_svg_url,
        tech_svg_filename=tech_svg_filename_out,
        detail_json_url=detail_json_url,
        detail_json_filename=detail_json_filename,
        jobticket_json_url=jobticket_json_url,
        jobticket_json_filename=jobticket_json_filename,
    )


@app.post("/api/add-bleed")
def add_bleed_endpoint(
    pdf: UploadFile = File(..., description="Kundendruckdatei (PDF)"),
    bleed_mm: float = Form(default=3.0),
    mode: str = Form(default="content-extend",
                     description="'content-extend' oder 'metadata-only'"),
    pdfx: str = Form(default="keep",
                     description="'keep', 'x1a' oder 'x4'"),
    mirror: bool = Form(default=False,
                        description="Randstreifen spiegeln statt verschieben"),
) -> dict:
    """PDF um Beschnitt erweitern — vektorbasiert, ohne Rasterisierung.

    Keine Pixel werden neu berechnet; vorhandene Image-Streams bleiben
    bit-identisch (verifiziert durch SHA-256-Vergleich der RAW-Bytes).
    """
    from app.generator.auto_bleed import add_bleed

    if mode not in ("content-extend", "metadata-only"):
        raise HTTPException(status_code=422, detail=f"Unbekannter Modus: {mode!r}")
    if pdfx not in ("keep", "x1a", "x4"):
        raise HTTPException(status_code=422, detail=f"Unbekannter pdfx-Modus: {pdfx!r}")

    cfg = load_config()
    max_bytes = cfg.get("max_upload_bytes", 524_288_000)
    job_id = uuid.uuid4().hex[:12]

    if bleed_mm <= 0 or bleed_mm > 30:
        raise HTTPException(
            status_code=422,
            detail="bleed_mm muss zwischen 0.1 und 30 liegen.",
        )

    # Save upload
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = _save_upload(pdf, job_dir / "input.pdf", max_bytes)

    # Build output filename from original upload name
    original_stem = Path(pdf.filename or "dokument").stem
    safe_stem = _sanitize_part(original_stem)[:80]
    bleed_str = f"{bleed_mm:.1f}".replace(".", "_")
    output_filename = f"{safe_stem}_mit_bleed_{bleed_str}mm.pdf"

    out_dir = OUTPUT_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / output_filename

    try:
        _, page_count, warnings = add_bleed(
            input_path, output_path,
            bleed_mm=bleed_mm,
            mode=mode,
            pdfx=pdfx,
            mirror=mirror,
        )
    except ValueError as exc:
        # Hard constraint violations (PDF/X validation etc.) → 422
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        # Post-save integrity check failed → 500
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Bleed expansion failed for job %s", job_id)
        raise HTTPException(
            status_code=500,
            detail=f"Bleed-Erweiterung fehlgeschlagen: {exc}",
        ) from exc
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)

    return {
        "url": f"/output/{job_id}/{output_filename}",
        "filename": output_filename,
        "pages": page_count,
        "bleed_mm": bleed_mm,
        "mode": mode,
        "warnings": warnings,
    }


@app.get("/output/{job_id}/{filename}")
def download_output(job_id: str, filename: str) -> FileResponse:
    # Sanitise path components
    if "/" in job_id or ".." in job_id or "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid path.")
    path = OUTPUT_DIR / job_id / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    if filename.endswith(".pdf"):
        media = "application/pdf"
    elif filename.endswith(".json"):
        media = "application/json"
    else:
        media = "image/svg+xml"
    return FileResponse(path, media_type=media, filename=filename)
