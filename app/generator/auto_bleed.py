"""Add bleed to a PDF without rasterisation.

Hard constraints (enforced at runtime and in tests):
  1. No page rasterisation — fitz.get_pixmap / insert_image / Ghostscript are
     NEVER used here.
  2. No new /Image XObjects — existing image streams travel through unchanged.
  3. DPI definition: effective_PPI = pixel_size / placed_size_in (derived from
     CTM, not a PDF property).  The Form XObject approach places the TrimBox
     content at 1:1 scale, so effective PPI is unchanged.
  4. PDF/X is validate-only (basic checks) — no silent colour-conversion or
     transparency-flattening.
  5. Save parameters guarantee no re-encoding of existing streams.

Algorithm (per page, content-extend mode):
  a. Wrap the page's content in a Form XObject via
     page.as_form_xobject(handle_transformations=True).
     The form's /Matrix absorbs /Rotate and /UserUnit.
  b. Replace page /Contents with a new content stream that draws the form
     XObject nine times with different clip rectangles and CTM shifts,
     covering the TrimBox area and all eight bleed regions.
  c. Expand page boxes: MediaBox = TrimBox + bleed on all sides.

CTM table (9 regions).  Abbreviations:
  mtx = bp - tx0   mty = bp - ty0   tw = tx1-tx0   th = ty1-ty0
  new_w = tw+2*bp  new_h = th+2*bp

  Region     Clip (x y w h)             CTM tx          CTM ty
  Main       bp bp tw th                mtx             mty
  Left        0 bp bp th               mtx-bp           mty
  Right     bp+tw bp bp th             mtx+bp           mty
  Top        bp bp+th tw bp             mtx            mty+bp
  Bottom      bp 0 tw bp               mtx            mty-bp
  TL corner   0 bp+th bp bp            mtx-bp          mty+bp
  TR corner bp+tw bp+th bp bp          mtx+bp          mty+bp
  BL corner   0 0 bp bp               mtx-bp           mty-bp
  BR corner bp+tw 0 bp bp             mtx+bp           mty-bp

All CTMs use a=1 b=0 c=0 d=1 (identity rotation, pure translation).
Mirror mode replaces the left/right CTMs with a=-1 reflections.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import pikepdf

logger = logging.getLogger(__name__)

MM_TO_PT: float = 72.0 / 25.4
_XFORM_NAME: str = "XpageForm"

# ── helpers ───────────────────────────────────────────────────────────────────


def _fmt(n: float) -> str:
    """Format a float for a PDF content stream (max 4 decimals, no trailing zeros)."""
    return f"{n:.4f}".rstrip("0").rstrip(".")


def _resolve(obj: Any) -> Any:
    """Resolve an indirect pikepdf reference; no-op for direct objects.

    Note: hasattr() is NOT used because in pikepdf 9 accessing .resolve
    on a non-indirect object (e.g. a plain Array) raises ValueError rather
    than AttributeError, so hasattr() itself would propagate the exception.
    We use a blanket try/except instead.
    """
    try:
        return obj.resolve()
    except Exception:
        return obj


def _objgen_key(obj: Any) -> str:
    """Stable identity key for a pikepdf object (uses objgen when available)."""
    try:
        og = obj.objgen
        if og != (0, 0):
            return f"xref:{og[0]}:{og[1]}"
    except Exception:
        pass
    return f"id:{id(obj)}"


# ── reference box ─────────────────────────────────────────────────────────────


def detect_reference_box(page: pikepdf.Page) -> tuple[float, float, float, float]:
    """Return (x0, y0, x1, y1) of TrimBox > CropBox > MediaBox.

    All coordinates are raw PDF user-space values from the page dictionary.
    Raises ValueError if no MediaBox can be found.
    """
    def _to_floats(arr: Any) -> tuple[float, float, float, float] | None:
        if arr is None:
            return None
        try:
            a = _resolve(arr)
            vals = [float(v) for v in a]
            if len(vals) == 4:
                return (vals[0], vals[1], vals[2], vals[3])
        except Exception:
            pass
        return None

    mb = _to_floats(page.obj.get("/MediaBox"))
    if mb is None:
        raise ValueError("Page has no /MediaBox")

    tb = _to_floats(page.obj.get("/TrimBox"))
    if tb is not None and tb != mb:
        return tb

    cb = _to_floats(page.obj.get("/CropBox"))
    if cb is not None and cb != mb:
        return cb

    return mb


# ── content stream reading ─────────────────────────────────────────────────────


def read_content_streams(page: pikepdf.Page) -> bytes:
    """Concatenate all /Contents streams for a page (decoded bytes).

    Handles both single-stream and multi-stream (Array) /Contents.
    Uses .read_bytes() (decoded) — suitable for operator analysis.
    Mirrors the _resolve_contents() pattern from drill_holes.py.
    """
    contents = page.obj.get("/Contents")
    if contents is None:
        return b""

    if isinstance(contents, pikepdf.Array):
        parts: list[bytes] = []
        for item in contents:
            obj = _resolve(item)
            if hasattr(obj, "read_bytes"):
                parts.append(obj.read_bytes())
            elif hasattr(obj, "get_stream_buffer"):
                parts.append(bytes(obj.get_stream_buffer()))
        return b"\n".join(parts)

    obj = _resolve(contents)
    if hasattr(obj, "read_bytes"):
        return obj.read_bytes()
    if hasattr(obj, "get_stream_buffer"):
        return bytes(obj.get_stream_buffer())
    return b""


# ── PDF/X info — recursive transparency & RGB detection ───────────────────────


def _scan_resources_for_transparency(
    resources: Any,
    visited: set[str],
    depth: int = 0,
) -> bool:
    """Recursively scan /Resources for transparency indicators.

    Checks:
      - /ExtGState: CA/ca < 1.0, BM != /Normal|/Compatible, /SMask != /None
      - Recurses into /Form XObjects (max depth 3)

    This is a BASIC check — it may miss transparency deep inside inline
    image masks or uncommon encoding schemes.  Label accordingly in docs.
    """
    if depth > 3 or resources is None:
        return False

    res = _resolve(resources)
    if not isinstance(res, pikepdf.Dictionary):
        return False

    # /ExtGState entries
    ext_gstate = res.get("/ExtGState")
    if ext_gstate is not None:
        eg = _resolve(ext_gstate)
        if isinstance(eg, pikepdf.Dictionary):
            for _key, gs_ref in eg.items():
                try:
                    gs = _resolve(gs_ref)
                    if not isinstance(gs, pikepdf.Dictionary):
                        continue
                    # Alpha (CA = stroking, ca = non-stroking)
                    for alpha_key in ("/CA", "/ca"):
                        alpha = gs.get(alpha_key)
                        if alpha is not None:
                            try:
                                if float(alpha) < 1.0:
                                    return True
                            except Exception:
                                pass
                    # Blend mode (anything except Normal/Compatible = transparency)
                    bm = gs.get("/BM")
                    if bm is not None:
                        bm_str = str(bm)
                        if bm_str not in ("/Normal", "/Compatible"):
                            return True
                    # Soft mask
                    smask = gs.get("/SMask")
                    if smask is not None and str(smask) != "/None":
                        return True
                except Exception:
                    continue

    # Recurse into Form XObjects
    xobjs = res.get("/XObject")
    if xobjs is not None:
        xo = _resolve(xobjs)
        if isinstance(xo, pikepdf.Dictionary):
            for _key, xobj_ref in xo.items():
                try:
                    xobj = _resolve(xobj_ref)
                    if not isinstance(xobj, pikepdf.Stream):
                        continue
                    if str(xobj.get("/Subtype", "")) != "/Form":
                        continue
                    obj_key = _objgen_key(xobj)
                    if obj_key in visited:
                        continue
                    visited.add(obj_key)
                    # Check /Group /S /Transparency on the form
                    grp = xobj.get("/Group")
                    if grp is not None:
                        g = _resolve(grp)
                        if isinstance(g, pikepdf.Dictionary):
                            s = g.get("/S")
                            if s is not None and str(s) == "/Transparency":
                                return True
                    # Recurse into form's own resources
                    form_res = xobj.get("/Resources")
                    if form_res is not None and _scan_resources_for_transparency(
                        form_res, visited, depth + 1
                    ):
                        return True
                except Exception:
                    continue

    return False


def _scan_resources_for_rgb(
    resources: Any,
    visited: set[str],
    depth: int = 0,
) -> bool:
    """Recursively scan /Resources for RGB colour spaces.

    Detects:
      - /DeviceRGB (direct or in /ColorSpace dict)
      - /ICCBased with N=3 (3-component = RGB)
      - Recurses into /Form XObjects (max depth 3)

    BASIC check — may miss RGB inside inline image dictionaries or
    uncommon encoding.
    """
    if depth > 3 or resources is None:
        return False

    res = _resolve(resources)
    if not isinstance(res, pikepdf.Dictionary):
        return False

    # /ColorSpace dict
    cs_dict = res.get("/ColorSpace")
    if cs_dict is not None:
        csd = _resolve(cs_dict)
        if isinstance(csd, pikepdf.Dictionary):
            for _key, cs_val in csd.items():
                try:
                    cs = _resolve(cs_val)
                    if isinstance(cs, pikepdf.Array) and len(cs) >= 1:
                        cs_type = str(cs[0])
                        if cs_type == "/DeviceRGB":
                            return True
                        if cs_type == "/ICCBased" and len(cs) >= 2:
                            icc = _resolve(cs[1])
                            if isinstance(icc, pikepdf.Stream):
                                n = icc.get("/N")
                                if n is not None and int(n) == 3:
                                    return True
                    elif isinstance(cs, pikepdf.Name) and str(cs) == "/DeviceRGB":
                        return True
                    elif hasattr(cs, "__str__") and str(cs) == "/DeviceRGB":
                        return True
                except Exception:
                    continue

    # Also check direct /DeviceRGB usage in image XObjects
    xobjs = res.get("/XObject")
    if xobjs is not None:
        xo = _resolve(xobjs)
        if isinstance(xo, pikepdf.Dictionary):
            for _key, xobj_ref in xo.items():
                try:
                    xobj = _resolve(xobj_ref)
                    if not isinstance(xobj, pikepdf.Stream):
                        continue
                    subtype = str(xobj.get("/Subtype", ""))
                    if subtype == "/Image":
                        cs = xobj.get("/ColorSpace")
                        if cs is not None:
                            cs_str = str(_resolve(cs))
                            if "DeviceRGB" in cs_str:
                                return True
                            if "ICCBased" in cs_str:
                                # Check N in the ICCBased stream
                                cs_val = _resolve(cs)
                                if isinstance(cs_val, pikepdf.Array) and len(cs_val) >= 2:
                                    icc = _resolve(cs_val[1])
                                    if isinstance(icc, pikepdf.Stream):
                                        n = icc.get("/N")
                                        if n is not None and int(n) == 3:
                                            return True
                    elif subtype == "/Form":
                        obj_key = _objgen_key(xobj)
                        if obj_key in visited:
                            continue
                        visited.add(obj_key)
                        form_res = xobj.get("/Resources")
                        if form_res is not None and _scan_resources_for_rgb(
                            form_res, visited, depth + 1
                        ):
                            return True
                except Exception:
                    continue

    return False


def collect_pdfx_info(pdf: pikepdf.Pdf) -> dict:
    """Collect PDF/X compliance metadata and content flags.

    NOTE: These are BASIC checks — not a substitute for a full
    Adobe-certified preflight.  x1a mode uses fail-safe logic:
    false positives (unnecessary aborts) are preferred over false
    negatives (silent pass-through of non-conforming content).

    Returns dict with keys:
      gts_pdfx_version   str or None
      output_intents     list[dict]
      has_transparency   bool  (ExtGState CA/ca/BM, /SMask, /Group Transparency)
      has_rgb            bool  (DeviceRGB or ICCBased N=3)
      has_smask          bool  (derived from has_transparency check)
      has_encryption     bool
    """
    info: dict = {
        "gts_pdfx_version": None,
        "output_intents": [],
        "has_transparency": False,
        "has_rgb": False,
        "has_smask": False,
        "has_encryption": pdf.is_encrypted,
    }

    # PDF/X version marker
    try:
        ver = pdf.Root.get("/GTS_PDFXVersion")
        if ver is not None:
            info["gts_pdfx_version"] = str(ver)
    except Exception:
        pass

    # OutputIntents
    try:
        oi_arr = pdf.Root.get("/OutputIntents")
        if oi_arr is not None:
            for entry in oi_arr:
                oi = _resolve(entry)
                info["output_intents"].append({
                    "S": str(oi.get("/S", "")),
                    "identifier": str(oi.get("/OutputConditionIdentifier", "")),
                })
    except Exception:
        pass

    # Per-page checks
    for page in pdf.pages:
        try:
            page_obj = page.obj

            # /Group /S /Transparency on the page itself
            grp = page_obj.get("/Group")
            if grp is not None:
                g = _resolve(grp)
                if isinstance(g, pikepdf.Dictionary):
                    s = g.get("/S")
                    if s is not None and str(s) == "/Transparency":
                        info["has_transparency"] = True

            # Recurse into Resources
            resources = page_obj.get("/Resources")
            if resources is not None:
                visited: set[str] = set()
                if not info["has_transparency"]:
                    if _scan_resources_for_transparency(resources, visited):
                        info["has_transparency"] = True
                        info["has_smask"] = True  # conservative: flag SMask too
                if not info["has_rgb"]:
                    if _scan_resources_for_rgb(resources, set()):
                        info["has_rgb"] = True

        except Exception:
            continue

    return info


# ── Form XObject wrapping ─────────────────────────────────────────────────────


def create_page_form_xobject(pdf: pikepdf.Pdf, page: pikepdf.Page) -> str:
    """Wrap the page content in a Form XObject; register it in page Resources.

    Uses page.as_form_xobject(handle_transformations=True) which:
      - Creates a Form XObject with /BBox = original MediaBox
      - Incorporates /Rotate and /UserUnit into /Matrix (absorbed)

    Point 1: MERGES into /Resources — does NOT overwrite existing entries.
    Point 2: Removes /Rotate and /UserUnit from the page dict (absorbed).

    Returns the XObject name string (without leading "/"), e.g. "XpageForm".
    """
    form_obj = page.as_form_xobject(handle_transformations=True)
    form_ref = pdf.make_indirect(form_obj)

    # ── Point 1: merge into /Resources, preserve all existing entries ──
    res = page.obj.get("/Resources")
    if res is None:
        page.obj["/Resources"] = pikepdf.Dictionary()
        res = page.obj["/Resources"]
    res = _resolve(res)

    if "/XObject" not in res:
        res["/XObject"] = pikepdf.Dictionary()
    xobjs = res["/XObject"]
    xobjs = _resolve(xobjs)

    # Collision-safe name
    name = _XFORM_NAME
    counter = 0
    while f"/{name}" in xobjs:
        counter += 1
        name = f"{_XFORM_NAME}{counter}"

    xobjs[f"/{name}"] = form_ref

    # ── Point 2: remove /Rotate and /UserUnit (absorbed into form /Matrix) ─
    for key in ("/Rotate", "/UserUnit"):
        if key in page.obj:
            del page.obj[key]

    return name


# ── bleed content stream builder ──────────────────────────────────────────────


def build_bleed_extension_stream(
    xobj_name: str,
    tx0: float,
    ty0: float,
    tx1: float,
    ty1: float,
    bp: float,
    mirror: bool = False,
) -> bytes:
    """Build the PDF content stream for bleed extension (9 regions).

    All coordinates are in the OUTPUT page coordinate system:
      - New MediaBox: [0, 0, new_w, new_h]
      - TrimBox:      [bp, bp, bp+tw, bp+th]
      - Form XObject was created from the original page (local coords = original)

    Each region is drawn as:
      q  <clip_x> <clip_y> <clip_w> <clip_h>  re W n
         <a> <b> <c> <d> <tx> <ty> cm  /<name> Do  Q

    Point 3: The MAIN draw clips to TrimBox [bp bp tw th] to prevent
    content that lived outside TrimBox (e.g. crop marks) from appearing
    in the output.
    """
    n = f"/{xobj_name}"
    tw = tx1 - tx0
    th = ty1 - ty0
    mtx = bp - tx0
    mty = bp - ty0
    tw_bp = tw + bp   # bp + tw
    th_bp = th + bp   # bp + th

    def region(clip: str, a: float, tx: float, ty: float) -> str:
        """Render one q/clip/cm/Do/Q block. clip is 'x y w h' string."""
        return (
            f"q {clip} re W n "
            f"{_fmt(a)} 0 0 1 {_fmt(tx)} {_fmt(ty)} cm {n} Do Q"
        )

    def region_full_identity(clip: str, tx: float, ty: float) -> str:
        """Shorthand for a=1 identity CTM (most regions)."""
        return region(clip, 1.0, tx, ty)

    parts: list[str] = []

    # ── Main: clip to TrimBox [bp bp tw th] (Point 3) ─────────────────────
    parts.append(
        f"q {_fmt(bp)} {_fmt(bp)} {_fmt(tw)} {_fmt(th)} re W n "
        f"1 0 0 1 {_fmt(mtx)} {_fmt(mty)} cm {n} Do Q"
    )

    # ── 4 edge bands ──────────────────────────────────────────────────────
    if mirror:
        # Left mirror: a=-1, reflect around TrimBox left edge (output x=bp)
        # At output x=bp: form_x = tx0 (seamless join)
        tx_left = tx0 + bp   # = bp + tx0  → xf = tx_left - output_x
        parts.append(
            f"q 0 {_fmt(bp)} {_fmt(bp)} {_fmt(th)} re W n "
            f"-1 0 0 1 {_fmt(tx_left)} {_fmt(mty)} cm {n} Do Q"
        )
    else:
        # Left shift: form_x = output_x + tx0 → CTM tx = -tx0 = mtx-bp
        parts.append(region_full_identity(
            f"0 {_fmt(bp)} {_fmt(bp)} {_fmt(th)}", mtx - bp, mty
        ))

    if mirror:
        # Right mirror: a=-1, reflect around TrimBox right edge (output x=bp+tw)
        # tx_right = tx1 + bp + tw = tx0 + 2*tw + bp
        tx_right = tx1 + bp + tw
        parts.append(
            f"q {_fmt(tw_bp)} {_fmt(bp)} {_fmt(bp)} {_fmt(th)} re W n "
            f"-1 0 0 1 {_fmt(tx_right)} {_fmt(mty)} cm {n} Do Q"
        )
    else:
        # Right shift: CTM tx = mtx+bp = 2*bp - tx0
        parts.append(region_full_identity(
            f"{_fmt(tw_bp)} {_fmt(bp)} {_fmt(bp)} {_fmt(th)}", mtx + bp, mty
        ))

    # Top band (no mirror — Y-mirror would need d=-1, out-of-scope)
    parts.append(region_full_identity(
        f"{_fmt(bp)} {_fmt(th_bp)} {_fmt(tw)} {_fmt(bp)}", mtx, mty + bp
    ))
    # Bottom band
    parts.append(region_full_identity(
        f"{_fmt(bp)} 0 {_fmt(tw)} {_fmt(bp)}", mtx, mty - bp
    ))

    # ── 4 corner bleed areas ───────────────────────────────────────────────
    parts.append(region_full_identity(
        f"0 {_fmt(th_bp)} {_fmt(bp)} {_fmt(bp)}", mtx - bp, mty + bp  # TL
    ))
    parts.append(region_full_identity(
        f"{_fmt(tw_bp)} {_fmt(th_bp)} {_fmt(bp)} {_fmt(bp)}", mtx + bp, mty + bp  # TR
    ))
    parts.append(region_full_identity(
        f"0 0 {_fmt(bp)} {_fmt(bp)}", mtx - bp, mty - bp  # BL
    ))
    parts.append(region_full_identity(
        f"{_fmt(tw_bp)} 0 {_fmt(bp)} {_fmt(bp)}", mtx + bp, mty - bp  # BR
    ))

    return "\n".join(parts).encode("latin-1")


# ── page box setting ───────────────────────────────────────────────────────────


def set_page_boxes(
    page: pikepdf.Page,
    new_media: tuple[float, float, float, float],
    new_trim: tuple[float, float, float, float],
    new_bleed: tuple[float, float, float, float],
) -> None:
    """Set MediaBox, TrimBox, BleedBox, CropBox on the page dict.

    CropBox is set to new_media so viewers default to showing the full
    bleed area.  /Rotate is removed (absorbed by Form XObject /Matrix).
    """
    def _arr(t: tuple[float, float, float, float]) -> pikepdf.Array:
        return pikepdf.Array([float(v) for v in t])

    page.obj["/MediaBox"] = _arr(new_media)
    page.obj["/TrimBox"] = _arr(new_trim)
    page.obj["/BleedBox"] = _arr(new_bleed)
    page.obj["/CropBox"] = _arr(new_media)
    if "/Rotate" in page.obj:
        del page.obj["/Rotate"]


# ── image integrity verification ──────────────────────────────────────────────


def _collect_image_fingerprints(pdf: pikepdf.Pdf) -> set[tuple]:
    """Collect content fingerprints for all /Image XObjects.

    Each fingerprint tuple: (width, height, bpc, colorspace_str,
                              filter_str, decodeparms_str, raw_sha256)

    Uses read_raw_bytes() — unmodified compressed stream bytes as stored
    in the PDF — NOT decoded pixel data.  This guarantees that any
    re-encoding (even lossless) would produce a different hash.
    """
    fingerprints: set[tuple] = set()
    for obj in pdf.objects:
        try:
            if not isinstance(obj, pikepdf.Stream):
                continue
            if str(obj.get("/Subtype", "")) != "/Image":
                continue
            w = int(obj.get("/Width", 0))
            h = int(obj.get("/Height", 0))
            bpc = int(obj.get("/BitsPerComponent", 0))
            cs = str(obj.get("/ColorSpace", ""))
            filt = str(obj.get("/Filter", ""))
            dp = str(obj.get("/DecodeParms", ""))
            raw = obj.read_raw_bytes()          # ← RAW, unmodified
            h256 = hashlib.sha256(raw).hexdigest()
            fingerprints.add((w, h, bpc, cs, filt, dp, h256))
        except Exception:
            continue
    return fingerprints


def verify_image_integrity(
    pdf_in_path: str | Path,
    pdf_out_path: str | Path,
) -> list[str]:
    """Verify all Image XObjects are bit-identical in input vs. output.

    Returns a list of violation strings (empty = OK).
    Any difference in /Width, /Height, /BitsPerComponent, /ColorSpace,
    /Filter, /DecodeParms, or raw stream bytes → violation.
    """
    with pikepdf.open(str(pdf_in_path)) as pdf_in:
        before = _collect_image_fingerprints(pdf_in)
    with pikepdf.open(str(pdf_out_path)) as pdf_out:
        after = _collect_image_fingerprints(pdf_out)

    violations: list[str] = []
    for w, h, bpc, cs, filt, dp, sha in (before - after):
        violations.append(
            f"Image {w}x{h} bpc={bpc} cs={cs!r} filter={filt!r} "
            f"hash={sha[:16]}… MISSING from output (modified or removed)"
        )
    for w, h, bpc, cs, filt, dp, sha in (after - before):
        violations.append(
            f"Image {w}x{h} bpc={bpc} cs={cs!r} filter={filt!r} "
            f"hash={sha[:16]}… UNEXPECTEDLY ADDED in output"
        )
    return violations


# ── edge-strip complexity check ───────────────────────────────────────────────


def _check_edge_strip_complexity(
    page: pikepdf.Page,
    tx0: float,
    ty0: float,
    tx1: float,
    ty1: float,
    bp: float,
) -> list[str]:
    """Warn if the page content may contain text or paths near the trim edge.

    Conservative check: looks for PDF text-begin operator (BT) anywhere in
    the content stream.  Geometric precision (CTM-aware strip analysis) is
    out-of-scope; this is a conservative signal to prompt visual review.
    """
    warnings: list[str] = []
    try:
        stream_bytes = read_content_streams(page)
        if b"BT" in stream_bytes:
            warnings.append(
                "Seite enthält Text (BT-Operator gefunden). "
                "Der Randstreifen im Bleed-Bereich kann sichtbaren Text zeigen. "
                "Bitte Ergebnis visuell prüfen oder 'metadata-only' verwenden."
            )
    except Exception:
        pass
    return warnings


# ── main function ─────────────────────────────────────────────────────────────


def add_bleed(
    input_path: str | Path,
    output_path: str | Path,
    bleed_mm: float = 3.0,
    mode: str = "content-extend",
    pdfx: str = "keep",
    mirror: bool = False,
) -> tuple[Path, int, list[str]]:
    """Add bleed to every page of a PDF without rasterisation.

    Parameters
    ----------
    input_path:   Path to the source PDF.
    output_path:  Path where the output PDF will be written.
    bleed_mm:     Bleed amount in millimetres (all four sides).
    mode:         'content-extend' — Form XObject + 9-region bleed fill.
                  'metadata-only' — only update page boxes, no content change.
    pdfx:         'keep'  — preserve any existing PDF/X metadata unchanged.
                  'x1a'   — validate for PDF/X-1a; abort if transparency or
                            RGB/ICCBased-RGB is found (basic checks).
                  'x4'    — validate for PDF/X-4; abort on encryption,
                            warn if OutputIntent is missing.
    mirror:       If True, left/right bleed strips are mirrored rather than
                  shifted (only for 'content-extend' mode).

    Returns
    -------
    (output_path, page_count, warnings)
    warnings is a list of non-fatal advisory strings.

    Raises
    ------
    ValueError    Hard constraint violation (PDF/X validation failure,
                  invalid mode, zero-dimension TrimBox).
    RuntimeError  Post-save image integrity check failed (output deleted).
    pikepdf.PdfError  Input PDF cannot be opened or is malformed.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    bp = bleed_mm * MM_TO_PT
    all_warnings: list[str] = []

    if mode not in ("content-extend", "metadata-only"):
        raise ValueError(f"Unbekannter Modus: {mode!r}. Gültig: 'content-extend', 'metadata-only'")
    if pdfx not in ("keep", "x1a", "x4"):
        raise ValueError(f"Unbekannter pdfx-Modus: {pdfx!r}. Gültig: 'keep', 'x1a', 'x4'")

    with pikepdf.open(str(input_path)) as pdf:
        # ── PDF/X pre-flight (validate-only, no silent conversion) ────────
        pdfx_info = collect_pdfx_info(pdf)

        if pdfx == "x1a":
            errors: list[str] = []
            if pdfx_info["has_transparency"]:
                errors.append("Transparenz/Blendmode/SMask gefunden")
            if pdfx_info["has_rgb"]:
                errors.append("DeviceRGB oder ICCBased-RGB (N=3) gefunden")
            if pdfx_info["has_smask"]:
                if "Transparenz" not in "; ".join(errors):
                    errors.append("SMask/Softmask gefunden")
            if pdfx_info["has_encryption"]:
                errors.append("Verschlüsselung gefunden")
            if errors:
                raise ValueError(
                    f"PDF/X-1a Validierung fehlgeschlagen: {'; '.join(errors)}. "
                    f"Dieses Tool führt KEINE Konvertierung durch — "
                    f"bitte die Quelldatei mit einem DTP-Programm konvertieren."
                )

        elif pdfx == "x4":
            if pdfx_info["has_encryption"]:
                raise ValueError(
                    "PDF/X-4 Validierung fehlgeschlagen: Verschlüsselung gefunden."
                )
            if not pdfx_info["output_intents"]:
                msg = "PDF/X-4: Kein /OutputIntent in der Quelldatei. Bitte OutputIntent setzen."
                logger.warning(msg)
                all_warnings.append(msg)

        # ── Per-page processing ────────────────────────────────────────────
        for pno, pk_page in enumerate(pdf.pages):
            try:
                tx0, ty0, tx1, ty1 = detect_reference_box(pk_page)
            except ValueError as exc:
                raise ValueError(f"Seite {pno+1}: {exc}") from exc

            tw = tx1 - tx0
            th = ty1 - ty0
            if tw <= 0 or th <= 0:
                raise ValueError(
                    f"Seite {pno+1}: TrimBox hat ungültige Maße "
                    f"({tx0},{ty0},{tx1},{ty1})"
                )

            new_w = tw + 2.0 * bp
            new_h = th + 2.0 * bp
            new_media = (0.0, 0.0, new_w, new_h)
            new_trim = (bp, bp, bp + tw, bp + th)
            new_bleed = (0.0, 0.0, new_w, new_h)

            if mode == "metadata-only":
                set_page_boxes(pk_page, new_media, new_trim, new_bleed)

            else:  # content-extend
                # Edge-strip complexity warning
                page_warnings = _check_edge_strip_complexity(
                    pk_page, tx0, ty0, tx1, ty1, bp
                )
                if page_warnings:
                    all_warnings.extend(
                        [f"Seite {pno+1}: {w}" for w in page_warnings]
                    )

                # Point 5: /Annots — warn and remove for print (Option A)
                if "/Annots" in pk_page.obj:
                    try:
                        n_annots = len(list(pk_page.obj["/Annots"]))
                    except Exception:
                        n_annots = "?"
                    msg = (
                        f"Seite {pno+1}: {n_annots} Annotation(en) (Links/Kommentare) "
                        f"werden entfernt — Koordinaten wären nach Bleed-Verschiebung "
                        f"nicht mehr korrekt."
                    )
                    all_warnings.append(msg)
                    del pk_page.obj["/Annots"]

                # Wrap content + replace page
                xobj_name = create_page_form_xobject(pdf, pk_page)
                stream = build_bleed_extension_stream(
                    xobj_name, tx0, ty0, tx1, ty1, bp, mirror=mirror
                )
                pk_page.obj["/Contents"] = pdf.make_stream(stream)
                set_page_boxes(pk_page, new_media, new_trim, new_bleed)

        page_count = len(pdf.pages)

        # ── Save — parameters chosen to prevent re-encoding ───────────────
        pdf.save(
            str(output_path),
            stream_decode_level=pikepdf.StreamDecodeLevel.none,  # no stream decompression
            compress_streams=True,       # compress the new content streams we added
            recompress_flate=False,      # do NOT touch existing Flate-compressed streams
            linearize=False,
            object_stream_mode=pikepdf.ObjectStreamMode.preserve,
        )

    # ── Post-save integrity check ──────────────────────────────────────────
    violations = verify_image_integrity(input_path, output_path)
    if violations:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Image-Integritätsprüfung fehlgeschlagen — Output gelöscht.\n"
            + "\n".join(violations)
        )

    logger.info(
        "add_bleed: %.1f mm mode=%s pdfx=%s mirror=%s → %d Seite(n) → %s",
        bleed_mm, mode, pdfx, mirror, page_count, output_path,
    )
    return output_path, page_count, all_warnings
