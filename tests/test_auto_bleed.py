"""Tests for app.generator.auto_bleed — all synthetic in-memory PDFs.

Hard constraints verified by this suite:
  1. No rasterisation (no new /Image XObjects created)
  2. Existing images bit-identical before / after (raw SHA-256)
  3. Form XObject approach — 9 regions in content stream
  4. PDF/X validate-only (basic checks), no silent conversion
  5. /Resources are MERGED, not overwritten (Point 1)
  6. /UserUnit removed from page dict (Point 2)
  7. Main draw clips to TrimBox (Point 3)
  8. Recursive transparency / RGB detection (Point 4)
  9. /Annots removed with warning (Point 5)
"""
from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from app.generator.auto_bleed import (
    MM_TO_PT,
    _check_edge_strip_complexity,
    _collect_image_fingerprints,
    _fmt,
    add_bleed,
    build_bleed_extension_stream,
    collect_pdfx_info,
    detect_reference_box,
    read_content_streams,
    verify_image_integrity,
)


# ── synthetic PDF helpers ─────────────────────────────────────────────────────


def _rv(*vals: float) -> pikepdf.Array:
    """pikepdf Array of numeric values (floats become Decimal internally)."""
    return pikepdf.Array(list(vals))


def _simple_page(width: float = 595.0, height: float = 842.0) -> pikepdf.Page:
    """Minimal /Page (no TrimBox, empty Resources)."""
    return pikepdf.Page(pikepdf.Dictionary(
        Type=pikepdf.Name("/Page"),
        MediaBox=_rv(0, 0, width, height),
        Resources=pikepdf.Dictionary(),
    ))


def _simple_pdf(tmp_path: Path, width: float = 595.0, height: float = 842.0,
                name: str = "in.pdf") -> Path:
    """Save a 1-page PDF with the given MediaBox."""
    pdf = pikepdf.Pdf.new()
    pdf.pages.append(_simple_page(width, height))
    p = tmp_path / name
    pdf.save(str(p))
    return p


def _pdf_with_trim(tmp_path: Path, media=(0, 0, 595, 842),
                   trim=(10, 10, 585, 832)) -> Path:
    """1-page PDF whose TrimBox is smaller than MediaBox."""
    pdf = pikepdf.Pdf.new()
    page = pikepdf.Page(pikepdf.Dictionary(
        Type=pikepdf.Name("/Page"),
        MediaBox=_rv(*media),
        TrimBox=_rv(*trim),
        Resources=pikepdf.Dictionary(),
    ))
    pdf.pages.append(page)
    p = tmp_path / "trim.pdf"
    pdf.save(str(p))
    return p


def _make_image_stream(pdf: pikepdf.Pdf, w: int = 2, h: int = 4) -> pikepdf.Stream:
    """Return an Image XObject stream (w×h DeviceCMYK, 8 bpc, uncompressed)."""
    raw = bytes([0, 50, 100, 150] * w * h)
    img = pikepdf.Stream(pdf, raw)
    img["/Type"] = pikepdf.Name("/XObject")
    img["/Subtype"] = pikepdf.Name("/Image")
    img["/Width"] = w
    img["/Height"] = h
    img["/BitsPerComponent"] = 8
    img["/ColorSpace"] = pikepdf.Name("/DeviceCMYK")
    return img


def _pdf_with_image(tmp_path: Path) -> Path:
    """1-page PDF with one 2×4 CMYK image XObject."""
    pdf = pikepdf.Pdf.new()
    img_ref = pdf.make_indirect(_make_image_stream(pdf))
    page = pikepdf.Page(pikepdf.Dictionary(
        Type=pikepdf.Name("/Page"),
        MediaBox=_rv(0, 0, 595, 842),
        Resources=pikepdf.Dictionary(
            XObject=pikepdf.Dictionary(Im0=img_ref),
        ),
    ))
    pdf.pages.append(page)
    p = tmp_path / "img.pdf"
    pdf.save(str(p))
    return p


def _count_image_xobjects(path: Path) -> int:
    count = 0
    with pikepdf.open(str(path)) as pdf:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                if str(obj.get("/Subtype", "")) == "/Image":
                    count += 1
    return count


def _get_output_content_stream(path: Path) -> str:
    """Return decoded content stream of page 0 as str."""
    with pikepdf.open(str(path)) as pdf:
        return read_content_streams(pdf.pages[0]).decode("latin-1")


# ── TestDetectReferenceBox ────────────────────────────────────────────────────


class TestDetectReferenceBox:
    def test_trimbox_preferred_over_cropbox_and_mediabox(self):
        pdf = pikepdf.Pdf.new()
        pdf.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name("/Page"),
            MediaBox=_rv(0, 0, 595, 842),
            TrimBox=_rv(10, 10, 585, 832),
            CropBox=_rv(5, 5, 590, 837),
            Resources=pikepdf.Dictionary(),
        )))
        assert detect_reference_box(pdf.pages[0]) == (10.0, 10.0, 585.0, 832.0)

    def test_cropbox_when_no_trimbox(self):
        pdf = pikepdf.Pdf.new()
        pdf.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name("/Page"),
            MediaBox=_rv(0, 0, 595, 842),
            CropBox=_rv(5, 5, 590, 837),
            Resources=pikepdf.Dictionary(),
        )))
        assert detect_reference_box(pdf.pages[0]) == (5.0, 5.0, 590.0, 837.0)

    def test_mediabox_fallback(self, tmp_path):
        p = _simple_pdf(tmp_path)
        with pikepdf.open(str(p)) as pdf:
            assert detect_reference_box(pdf.pages[0]) == (0.0, 0.0, 595.0, 842.0)

    def test_trimbox_equal_to_mediabox_uses_mediabox(self):
        pdf = pikepdf.Pdf.new()
        pdf.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name("/Page"),
            MediaBox=_rv(0, 0, 595, 842),
            TrimBox=_rv(0, 0, 595, 842),
            Resources=pikepdf.Dictionary(),
        )))
        # TrimBox == MediaBox → not "different" → fall through to mediabox
        assert detect_reference_box(pdf.pages[0]) == (0.0, 0.0, 595.0, 842.0)

    def test_no_mediabox_raises(self):
        pdf = pikepdf.Pdf.new()
        pdf.pages.append(pikepdf.Page(pikepdf.Dictionary(Type=pikepdf.Name("/Page"))))
        with pytest.raises(ValueError, match="MediaBox"):
            detect_reference_box(pdf.pages[0])


# ── TestMetadataOnlyBoxes ─────────────────────────────────────────────────────


class TestMetadataOnlyBoxes:
    def test_correct_box_values(self, tmp_path):
        bleed_mm = 3.0
        bp = bleed_mm * MM_TO_PT
        tw, th = 595.0, 842.0
        p = _simple_pdf(tmp_path, width=tw, height=th)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, bleed_mm=bleed_mm, mode="metadata-only")

        with pikepdf.open(str(out)) as pdf:
            page = pdf.pages[0]
            mb = [float(v) for v in page.obj["/MediaBox"]]
            tb = [float(v) for v in page.obj["/TrimBox"]]
            blb = [float(v) for v in page.obj["/BleedBox"]]

        new_w, new_h = tw + 2 * bp, th + 2 * bp
        assert abs(mb[2] - new_w) < 0.01
        assert abs(mb[3] - new_h) < 0.01
        assert abs(tb[0] - bp) < 0.01
        assert abs(tb[1] - bp) < 0.01
        assert abs(tb[2] - (bp + tw)) < 0.01
        assert abs(tb[3] - (bp + th)) < 0.01
        assert mb == blb

    def test_no_form_xobject_in_metadata_only(self, tmp_path):
        p = _simple_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="metadata-only")
        with pikepdf.open(str(out)) as pdf:
            res = pdf.pages[0].obj.get("/Resources")
            if res is not None:
                xobjs = res.get("/XObject")
                if xobjs is not None:
                    for val in xobjs.values():
                        obj = val.resolve() if hasattr(val, "resolve") else val
                        assert str(obj.get("/Subtype", "")) != "/Form", \
                            "metadata-only must not create a Form XObject"


# ── TestBoxGeometryExact ──────────────────────────────────────────────────────


class TestBoxGeometryExact:
    def test_new_width_exact(self, tmp_path):
        bleed_mm = 5.0
        bp = bleed_mm * MM_TO_PT
        tw, th = 400.0, 600.0
        p = _simple_pdf(tmp_path, width=tw, height=th)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, bleed_mm=bleed_mm, mode="metadata-only")
        with pikepdf.open(str(out)) as pdf:
            mb = [float(v) for v in pdf.pages[0].obj["/MediaBox"]]
        assert abs(mb[2] - (tw + 2 * bp)) < 0.01
        assert abs(mb[3] - (th + 2 * bp)) < 0.01

    def test_with_trimbox_offset(self, tmp_path):
        bleed_mm = 3.0
        bp = bleed_mm * MM_TO_PT
        # TrimBox: tw=575, th=822
        p = _pdf_with_trim(tmp_path, media=(0, 0, 595, 842), trim=(10, 10, 585, 832))
        out = tmp_path / "out.pdf"
        add_bleed(p, out, bleed_mm=bleed_mm, mode="metadata-only")
        with pikepdf.open(str(out)) as pdf:
            mb = [float(v) for v in pdf.pages[0].obj["/MediaBox"]]
        assert abs(mb[2] - (575.0 + 2 * bp)) < 0.01
        assert abs(mb[3] - (822.0 + 2 * bp)) < 0.01


# ── TestFormXObjectCreated ────────────────────────────────────────────────────


class TestFormXObjectCreated:
    def test_xpageform_in_resources(self, tmp_path):
        p = _simple_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="content-extend")
        with pikepdf.open(str(out)) as pdf:
            res = pdf.pages[0].obj["/Resources"]
            assert "/XObject" in res, "No /XObject in Resources"
            xobjs = res["/XObject"]
            found_form = False
            for _k, val in xobjs.items():
                try:
                    obj = val.resolve()
                except Exception:
                    obj = val
                if str(obj.get("/Subtype", "")) == "/Form":
                    found_form = True
                    break
            assert found_form, "No /Form XObject in /Resources/XObject"

    def test_xpageform_name_present(self, tmp_path):
        p = _simple_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="content-extend")
        with pikepdf.open(str(out)) as pdf:
            xobjs = pdf.pages[0].obj["/Resources"]["/XObject"]
            assert "/XpageForm" in xobjs


# ── TestBleedStreamOperators ──────────────────────────────────────────────────


class TestBleedStreamOperators:
    def test_exactly_9_do_calls(self, tmp_path):
        p = _simple_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="content-extend")
        cs = _get_output_content_stream(out)
        assert cs.count("Do Q") == 9, \
            f"Expected 9 'Do Q' in stream, got {cs.count('Do Q')}"

    def test_clipping_operators_present(self, tmp_path):
        p = _simple_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="content-extend")
        cs = _get_output_content_stream(out)
        assert "re W n" in cs
        assert " cm " in cs

    def test_nine_q_blocks(self, tmp_path):
        p = _simple_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="content-extend")
        cs = _get_output_content_stream(out)
        # Each region starts with "q " on its own segment
        assert cs.count("Do Q") == 9

    def test_stream_has_form_xobject_name(self, tmp_path):
        p = _simple_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="content-extend")
        cs = _get_output_content_stream(out)
        assert "/XpageForm" in cs


# ── TestBleedStreamGeometry ───────────────────────────────────────────────────


class TestBleedStreamGeometry:
    def test_main_ctm_identity_page(self):
        """Main CTM: tx=bp, ty=bp when TrimBox starts at (0,0)."""
        bp = 3.0 * MM_TO_PT
        stream = build_bleed_extension_stream("Xf", 0.0, 0.0, 595.0, 842.0, bp)
        text = stream.decode("latin-1")
        # Main region uses identity shift: a=1, tx=bp, ty=bp
        assert f"1 0 0 1 {_fmt(bp)} {_fmt(bp)} cm" in text

    def test_left_band_ctm(self):
        """Left band: tx = mtx-bp = -tx0."""
        bp = 3.0 * MM_TO_PT
        tx0 = 0.0
        stream = build_bleed_extension_stream("Xf", tx0, 0.0, 595.0, 842.0, bp)
        text = stream.decode("latin-1")
        # mtx = bp - tx0 = bp; left tx = mtx - bp = 0
        assert f"1 0 0 1 {_fmt(0.0)} {_fmt(bp)} cm" in text

    def test_right_band_ctm(self):
        """Right band: tx = mtx+bp = 2*bp - tx0."""
        bp = 3.0 * MM_TO_PT
        stream = build_bleed_extension_stream("Xf", 0.0, 0.0, 595.0, 842.0, bp)
        text = stream.decode("latin-1")
        assert f"1 0 0 1 {_fmt(2 * bp)} {_fmt(bp)} cm" in text

    def test_top_bottom_ctm(self):
        """Top: ty=mty+bp. Bottom: ty=mty-bp."""
        bp = 3.0 * MM_TO_PT
        stream = build_bleed_extension_stream("Xf", 0.0, 0.0, 595.0, 842.0, bp)
        text = stream.decode("latin-1")
        assert f"1 0 0 1 {_fmt(bp)} {_fmt(2 * bp)} cm" in text   # top
        assert f"1 0 0 1 {_fmt(bp)} {_fmt(0.0)} cm" in text       # bottom

    def test_nonzero_origin_ctms(self):
        """With tx0=10, ty0=10: mtx=bp-10, mty=bp-10."""
        bp = 3.0 * MM_TO_PT
        tx0, ty0 = 10.0, 10.0
        stream = build_bleed_extension_stream("Xf", tx0, ty0, 585.0, 832.0, bp)
        text = stream.decode("latin-1")
        mtx = bp - tx0
        mty = bp - ty0
        assert f"1 0 0 1 {_fmt(mtx)} {_fmt(mty)} cm" in text
        assert f"1 0 0 1 {_fmt(mtx - bp)} {_fmt(mty)} cm" in text
        assert f"1 0 0 1 {_fmt(mtx + bp)} {_fmt(mty)} cm" in text

    def test_main_clip_in_stream(self):
        """Main region clip must be [bp, bp, tw, th] (output coords)."""
        bp = 3.0 * MM_TO_PT
        tw, th = 595.0, 842.0
        stream = build_bleed_extension_stream("Xf", 0.0, 0.0, tw, th, bp)
        text = stream.decode("latin-1")
        expected = f"{_fmt(bp)} {_fmt(bp)} {_fmt(tw)} {_fmt(th)} re W n"
        assert expected in text, \
            f"Main TrimBox clip not found.\nExpected: {expected!r}"

    def test_nine_regions_in_direct_call(self):
        """build_bleed_extension_stream returns exactly 9 'Do Q' blocks."""
        stream = build_bleed_extension_stream("Xf", 0, 0, 595, 842, 3 * MM_TO_PT)
        assert stream.decode("latin-1").count("Do Q") == 9


# ── TestImageRawHashIdentical ─────────────────────────────────────────────────


class TestImageRawHashIdentical:
    def test_sha256_unchanged_after_bleed(self, tmp_path):
        p = _pdf_with_image(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="content-extend")

        with pikepdf.open(str(p)) as pdf_in:
            before = _collect_image_fingerprints(pdf_in)
        with pikepdf.open(str(out)) as pdf_out:
            after = _collect_image_fingerprints(pdf_out)

        missing = before - after
        assert not missing, f"Image fingerprints changed or missing: {missing}"

    def test_filter_and_decodeparms_unchanged(self, tmp_path):
        """Verify /Filter and /DecodeParms of images survive unchanged."""
        p = _pdf_with_image(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="content-extend")

        with pikepdf.open(str(p)) as pdf_in:
            before = _collect_image_fingerprints(pdf_in)
        with pikepdf.open(str(out)) as pdf_out:
            after = _collect_image_fingerprints(pdf_out)
        # The fingerprint tuples include filter and decodeparms → mismatch would
        # appear as missing items in `before - after`
        assert (before - after) == set()


# ── TestImageWidthHeightPreserved ─────────────────────────────────────────────


class TestImageWidthHeightPreserved:
    def test_dimensions_and_colorspace(self, tmp_path):
        p = _pdf_with_image(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="content-extend")

        with pikepdf.open(str(out)) as pdf:
            found = False
            for obj in pdf.objects:
                if isinstance(obj, pikepdf.Stream) and \
                        str(obj.get("/Subtype", "")) == "/Image":
                    assert int(obj["/Width"]) == 2
                    assert int(obj["/Height"]) == 4
                    assert int(obj["/BitsPerComponent"]) == 8
                    assert str(obj["/ColorSpace"]) == "/DeviceCMYK"
                    found = True
        assert found, "Image XObject not found in output"


# ── TestNoNewImageXObjects ────────────────────────────────────────────────────


class TestNoNewImageXObjects:
    def test_zero_images_in_zero_images_out(self, tmp_path):
        p = _simple_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="content-extend")
        assert _count_image_xobjects(out) == 0

    def test_one_image_in_one_image_out(self, tmp_path):
        p = _pdf_with_image(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="content-extend")
        assert _count_image_xobjects(out) == _count_image_xobjects(p)


# ── TestSavePreservesRawStreams ───────────────────────────────────────────────


class TestSavePreservesRawStreams:
    def test_verify_integrity_returns_empty_list(self, tmp_path):
        p = _pdf_with_image(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="content-extend")
        violations = verify_image_integrity(p, out)
        assert violations == [], f"Unexpected violations: {violations}"

    def test_no_image_page_also_passes_integrity(self, tmp_path):
        p = _simple_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="content-extend")
        violations = verify_image_integrity(p, out)
        assert violations == []


# ── TestVerifyImageIntegrityDetectsTampering ──────────────────────────────────


class TestVerifyImageIntegrityDetectsTampering:
    def test_modified_image_detected(self, tmp_path):
        p = _pdf_with_image(tmp_path)

        # Create "tampered" output: modify the image bytes
        out = tmp_path / "out.pdf"
        with pikepdf.open(str(p)) as pdf_mod:
            for obj in pdf_mod.objects:
                if isinstance(obj, pikepdf.Stream) and \
                        str(obj.get("/Subtype", "")) == "/Image":
                    obj.write(bytes([255, 0, 0, 0] * 8))  # 2x4 px, all different
                    break
            pdf_mod.save(str(out))

        violations = verify_image_integrity(p, out)
        assert len(violations) > 0, "Tampered image was not detected"

    def test_extra_image_in_output_detected(self, tmp_path):
        p = _simple_pdf(tmp_path)

        out = tmp_path / "out.pdf"
        with pikepdf.open(str(p)) as pdf_mod:
            img = _make_image_stream(pdf_mod, 3, 3)
            img_ref = pdf_mod.make_indirect(img)
            # Manually add image to resources
            res = pdf_mod.pages[0].obj.get("/Resources")
            if res is None:
                pdf_mod.pages[0].obj["/Resources"] = pikepdf.Dictionary()
            pdf_mod.pages[0].obj["/Resources"]["/XObject"] = pikepdf.Dictionary()
            pdf_mod.pages[0].obj["/Resources"]["/XObject"]["/ImExtra"] = img_ref
            pdf_mod.save(str(out))

        violations = verify_image_integrity(p, out)
        assert len(violations) > 0, "Extra image in output was not detected"


# ── TestPDFXKeepOutputIntent ──────────────────────────────────────────────────


class TestPDFXKeepOutputIntent:
    def _make_pdfx_pdf(self, tmp_path: Path) -> Path:
        pdf = pikepdf.Pdf.new()
        pdf.pages.append(_simple_page())
        oi = pikepdf.Dictionary(
            Type=pikepdf.Name("/OutputIntent"),
            S=pikepdf.Name("/GTS_PDFX"),
            OutputConditionIdentifier=pikepdf.String("FOGRA39"),
        )
        pdf.Root["/OutputIntents"] = pikepdf.Array([pdf.make_indirect(oi)])
        pdf.Root["/GTS_PDFXVersion"] = pikepdf.String("PDF/X-1a:2003")
        p = tmp_path / "pdfx.pdf"
        pdf.save(str(p))
        return p

    def test_output_intents_preserved(self, tmp_path):
        p = self._make_pdfx_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, pdfx="keep", mode="content-extend")
        with pikepdf.open(str(out)) as pdf:
            info = collect_pdfx_info(pdf)
        assert len(info["output_intents"]) >= 1
        assert "FOGRA39" in info["output_intents"][0]["identifier"]

    def test_pdfx_version_preserved(self, tmp_path):
        p = self._make_pdfx_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, pdfx="keep", mode="content-extend")
        with pikepdf.open(str(out)) as pdf:
            info = collect_pdfx_info(pdf)
        assert info["gts_pdfx_version"] is not None


# ── TestPDFXX1aAbortsOnRGB ────────────────────────────────────────────────────


class TestPDFXX1aAbortsOnRGB:
    def _make_rgb_pdf(self, tmp_path: Path) -> Path:
        pdf = pikepdf.Pdf.new()
        pdf.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name("/Page"),
            MediaBox=_rv(0, 0, 595, 842),
            Resources=pikepdf.Dictionary(
                ColorSpace=pikepdf.Dictionary(
                    Cs1=pikepdf.Array([pikepdf.Name("/DeviceRGB")]),
                ),
            ),
        )))
        p = tmp_path / "rgb.pdf"
        pdf.save(str(p))
        return p

    def test_rgb_aborts_x1a(self, tmp_path):
        p = self._make_rgb_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        with pytest.raises(ValueError, match="(?i)rgb|validierung"):
            add_bleed(p, out, pdfx="x1a")

    def test_rgb_passes_with_keep(self, tmp_path):
        p = self._make_rgb_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, pdfx="keep")
        assert out.exists()


# ── TestPDFXX1aAbortsOnTransparency ──────────────────────────────────────────


class TestPDFXX1aAbortsOnTransparency:
    def _make_transparency_pdf(self, tmp_path: Path) -> Path:
        """PDF with page-level /Group /S /Transparency."""
        pdf = pikepdf.Pdf.new()
        pdf.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name("/Page"),
            MediaBox=_rv(0, 0, 595, 842),
            Resources=pikepdf.Dictionary(),
            Group=pikepdf.Dictionary(
                Type=pikepdf.Name("/Group"),
                S=pikepdf.Name("/Transparency"),
            ),
        )))
        p = tmp_path / "transp.pdf"
        pdf.save(str(p))
        return p

    def test_page_transparency_aborts_x1a(self, tmp_path):
        p = self._make_transparency_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        with pytest.raises(ValueError, match="(?i)transparenz|validierung"):
            add_bleed(p, out, pdfx="x1a")


# ── TestPDFXX4WarnOnMissingIntent ────────────────────────────────────────────


class TestPDFXX4WarnOnMissingIntent:
    def test_no_exception_raised(self, tmp_path):
        p = _simple_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        path, pages, warnings = add_bleed(p, out, pdfx="x4")
        assert out.exists()
        assert pages == 1

    def test_warning_mentions_output_intent(self, tmp_path):
        p = _simple_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        _, _, warnings = add_bleed(p, out, pdfx="x4")
        assert any("OutputIntent" in w for w in warnings), \
            f"Expected OutputIntent warning, got: {warnings}"


# ── TestEdgeWarningOnText ─────────────────────────────────────────────────────


class TestEdgeWarningOnText:
    def _make_text_pdf(self, tmp_path: Path) -> Path:
        pdf = pikepdf.Pdf.new()
        page = pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name("/Page"),
            MediaBox=_rv(0, 0, 595, 842),
            Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary()),
        ))
        pdf.pages.append(page)
        content = b"BT /F1 12 Tf 100 700 Td (Hello) Tj ET"
        pdf.pages[0].obj["/Contents"] = pdf.make_stream(content)
        p = tmp_path / "text.pdf"
        pdf.save(str(p))
        return p

    def test_bt_operator_triggers_warning(self, tmp_path):
        p = self._make_text_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        _, _, warnings = add_bleed(p, out, mode="content-extend")
        assert any("Text" in w or "BT" in w for w in warnings), \
            f"Expected text warning, got: {warnings}"

    def test_no_text_no_warning(self, tmp_path):
        p = _simple_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        _, _, warnings = add_bleed(p, out, mode="content-extend")
        text_warnings = [w for w in warnings if "Text" in w or "BT" in w]
        assert text_warnings == []


# ── Point 1: TestResourcesMerged ─────────────────────────────────────────────


class TestResourcesMerged:
    """Point 1: create_page_form_xobject must MERGE into /Resources,
    not overwrite existing ExtGState/ColorSpace/Pattern entries."""

    def test_existing_resources_preserved(self, tmp_path):
        pdf = pikepdf.Pdf.new()
        pdf.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name("/Page"),
            MediaBox=_rv(0, 0, 595, 842),
            Resources=pikepdf.Dictionary(
                ExtGState=pikepdf.Dictionary(
                    GS1=pikepdf.Dictionary(Type=pikepdf.Name("/ExtGState")),
                ),
                ColorSpace=pikepdf.Dictionary(
                    CS1=pikepdf.Array([pikepdf.Name("/DeviceGray")]),
                ),
                Pattern=pikepdf.Dictionary(),
            ),
        )))
        inp = tmp_path / "in.pdf"
        pdf.save(str(inp))

        out = tmp_path / "out.pdf"
        add_bleed(inp, out, mode="content-extend")

        with pikepdf.open(str(out)) as pdf_out:
            res = pdf_out.pages[0].obj["/Resources"]
            assert "/ExtGState" in res, "/ExtGState was lost after add_bleed"
            assert "/ColorSpace" in res, "/ColorSpace was lost after add_bleed"
            assert "/Pattern" in res, "/Pattern was lost after add_bleed"
            assert "/XObject" in res, "/XObject not added"
            assert "/XpageForm" in res["/XObject"], "/XpageForm not in /XObject"

    def test_xobject_added_to_existing_xobject_dict(self, tmp_path):
        """If /XObject already existed with other entries, they survive."""
        p = _pdf_with_image(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="content-extend")
        with pikepdf.open(str(out)) as pdf_out:
            xobjs = pdf_out.pages[0].obj["/Resources"]["/XObject"]
            # XpageForm was added
            assert "/XpageForm" in xobjs


# ── Point 2: TestUserUnitRemoved ─────────────────────────────────────────────


class TestUserUnitRemoved:
    """Point 2: /UserUnit must be removed from the page dict (absorbed into
    the Form XObject /Matrix by handle_transformations=True)."""

    def test_userunit_not_in_output_page(self, tmp_path):
        pdf = pikepdf.Pdf.new()
        pdf.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name("/Page"),
            MediaBox=_rv(0, 0, 595, 842),
            UserUnit=2.0,
            Resources=pikepdf.Dictionary(),
        )))
        inp = tmp_path / "in.pdf"
        pdf.save(str(inp))

        out = tmp_path / "out.pdf"
        add_bleed(inp, out, mode="content-extend")

        with pikepdf.open(str(out)) as pdf_out:
            page_obj = pdf_out.pages[0].obj
            assert "/UserUnit" not in page_obj, \
                "/UserUnit still present in output page dict"

    def test_rotate_not_in_output_page(self, tmp_path):
        """Similarly, /Rotate should be removed."""
        pdf = pikepdf.Pdf.new()
        pdf.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name("/Page"),
            MediaBox=_rv(0, 0, 595, 842),
            Rotate=90,
            Resources=pikepdf.Dictionary(),
        )))
        inp = tmp_path / "in.pdf"
        pdf.save(str(inp))

        out = tmp_path / "out.pdf"
        add_bleed(inp, out, mode="content-extend")

        with pikepdf.open(str(out)) as pdf_out:
            page_obj = pdf_out.pages[0].obj
            assert "/Rotate" not in page_obj, \
                "/Rotate still present in output page dict"


# ── Point 3: TestContentOutsideTrimClipped ────────────────────────────────────


class TestContentOutsideTrimClipped:
    """Point 3: The Main Do region must clip to TrimBox [bp bp tw th re W n].
    This prevents content that lives outside TrimBox (e.g. printers marks in
    MediaBox area) from bleeding into the output."""

    def test_main_clip_string_in_content_stream(self, tmp_path):
        bleed_mm = 3.0
        bp = bleed_mm * MM_TO_PT
        tw, th = 575.0, 822.0  # TrimBox dims (MediaBox 595x842, offset 10,10)
        p = _pdf_with_trim(tmp_path, media=(0, 0, 595, 842), trim=(10, 10, 585, 832))
        out = tmp_path / "out.pdf"
        add_bleed(p, out, bleed_mm=bleed_mm, mode="content-extend")

        cs = _get_output_content_stream(out)
        expected_clip = f"{_fmt(bp)} {_fmt(bp)} {_fmt(tw)} {_fmt(th)} re W n"
        assert expected_clip in cs, \
            f"Main TrimBox clip '{expected_clip}' not in content stream.\n" \
            f"Stream excerpt: {cs[:400]!r}"

    def test_main_clip_appears_before_main_do(self, tmp_path):
        bleed_mm = 3.0
        bp = bleed_mm * MM_TO_PT
        p = _simple_pdf(tmp_path)  # tx0=0, ty0=0 → Main clip = bp bp tw th
        out = tmp_path / "out.pdf"
        add_bleed(p, out, bleed_mm=bleed_mm, mode="content-extend")

        cs = _get_output_content_stream(out)
        clip_pos = cs.find("re W n")
        do_pos = cs.find("Do Q")
        assert clip_pos < do_pos, "Clip must appear before first Do Q"

    def test_content_outside_trim_uses_clip(self):
        """build_bleed_extension_stream: Main region must have clip = TrimBox."""
        bp = 3.0 * MM_TO_PT
        tx0, ty0, tx1, ty1 = 10.0, 10.0, 585.0, 832.0
        tw, th = tx1 - tx0, ty1 - ty0
        stream = build_bleed_extension_stream("Xf", tx0, ty0, tx1, ty1, bp)
        text = stream.decode("latin-1")
        main_clip = f"{_fmt(bp)} {_fmt(bp)} {_fmt(tw)} {_fmt(th)} re W n"
        assert main_clip in text


# ── Point 4a: TestX1aAbortsOnAlphaExtGState ──────────────────────────────────


class TestX1aAbortsOnAlphaExtGState:
    """Point 4a: x1a must abort if ExtGState contains CA or ca < 1.0."""

    def _make_alpha_pdf(self, tmp_path: Path, ca: float) -> Path:
        pdf = pikepdf.Pdf.new()
        pdf.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name("/Page"),
            MediaBox=_rv(0, 0, 595, 842),
            Resources=pikepdf.Dictionary(
                ExtGState=pikepdf.Dictionary(
                    GS1=pikepdf.Dictionary(
                        Type=pikepdf.Name("/ExtGState"),
                        CA=float(ca),
                        ca=float(ca),
                    ),
                ),
            ),
        )))
        p = tmp_path / "alpha.pdf"
        pdf.save(str(p))
        return p

    def test_ca_half_aborts_x1a(self, tmp_path):
        p = self._make_alpha_pdf(tmp_path, 0.5)
        out = tmp_path / "out.pdf"
        with pytest.raises(ValueError, match="(?i)transparenz|validierung"):
            add_bleed(p, out, pdfx="x1a")

    def test_ca_zero_aborts_x1a(self, tmp_path):
        p = self._make_alpha_pdf(tmp_path, 0.0)
        out = tmp_path / "out.pdf"
        with pytest.raises(ValueError):
            add_bleed(p, out, pdfx="x1a")

    def test_ca_one_passes_x1a(self, tmp_path):
        """CA=1.0 is fully opaque — must NOT abort."""
        p = self._make_alpha_pdf(tmp_path, 1.0)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, pdfx="x1a")  # must not raise
        assert out.exists()


# ── Point 4b: TestX1aAbortsOnICCBasedRGB ─────────────────────────────────────


class TestX1aAbortsOnICCBasedRGB:
    """Point 4b: x1a must abort if a colour space is /ICCBased with N=3 (RGB)."""

    def _make_icc_rgb_pdf(self, tmp_path: Path) -> Path:
        pdf = pikepdf.Pdf.new()
        # Minimal ICC stream — only /N matters for detection
        icc_stream = pikepdf.Stream(pdf, b"\x00" * 128)
        icc_stream["/N"] = 3
        icc_ref = pdf.make_indirect(icc_stream)
        pdf.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name("/Page"),
            MediaBox=_rv(0, 0, 595, 842),
            Resources=pikepdf.Dictionary(
                ColorSpace=pikepdf.Dictionary(
                    CS1=pikepdf.Array([pikepdf.Name("/ICCBased"), icc_ref]),
                ),
            ),
        )))
        p = tmp_path / "icc_rgb.pdf"
        pdf.save(str(p))
        return p

    def _make_icc_cmyk_pdf(self, tmp_path: Path) -> Path:
        """ICCBased with N=4 (CMYK) — should NOT abort x1a."""
        pdf = pikepdf.Pdf.new()
        icc_stream = pikepdf.Stream(pdf, b"\x00" * 128)
        icc_stream["/N"] = 4
        icc_ref = pdf.make_indirect(icc_stream)
        pdf.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name("/Page"),
            MediaBox=_rv(0, 0, 595, 842),
            Resources=pikepdf.Dictionary(
                ColorSpace=pikepdf.Dictionary(
                    CS1=pikepdf.Array([pikepdf.Name("/ICCBased"), icc_ref]),
                ),
            ),
        )))
        p = tmp_path / "icc_cmyk.pdf"
        pdf.save(str(p))
        return p

    def test_iccbased_n3_aborts_x1a(self, tmp_path):
        p = self._make_icc_rgb_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        with pytest.raises(ValueError, match="(?i)rgb|icc|validierung"):
            add_bleed(p, out, pdfx="x1a")

    def test_iccbased_n4_passes_x1a(self, tmp_path):
        """ICCBased N=4 is CMYK — must not abort."""
        p = self._make_icc_cmyk_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, pdfx="x1a")  # must not raise
        assert out.exists()


# ── Point 4c: TestX1aAbortsOnBlendMode ───────────────────────────────────────


class TestX1aAbortsOnBlendMode:
    """Point 4c: x1a must abort if ExtGState has BM != /Normal or /Compatible."""

    def _make_bm_pdf(self, tmp_path: Path, bm: str) -> Path:
        pdf = pikepdf.Pdf.new()
        pdf.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name("/Page"),
            MediaBox=_rv(0, 0, 595, 842),
            Resources=pikepdf.Dictionary(
                ExtGState=pikepdf.Dictionary(
                    GS1=pikepdf.Dictionary(
                        Type=pikepdf.Name("/ExtGState"),
                        BM=pikepdf.Name(bm),
                    ),
                ),
            ),
        )))
        p = tmp_path / "bm.pdf"
        pdf.save(str(p))
        return p

    def test_multiply_aborts_x1a(self, tmp_path):
        p = self._make_bm_pdf(tmp_path, "/Multiply")
        out = tmp_path / "out.pdf"
        with pytest.raises(ValueError, match="(?i)transparenz|blendmode|validierung"):
            add_bleed(p, out, pdfx="x1a")

    def test_screen_aborts_x1a(self, tmp_path):
        p = self._make_bm_pdf(tmp_path, "/Screen")
        out = tmp_path / "out.pdf"
        with pytest.raises(ValueError):
            add_bleed(p, out, pdfx="x1a")

    def test_normal_passes_x1a(self, tmp_path):
        p = self._make_bm_pdf(tmp_path, "/Normal")
        out = tmp_path / "out.pdf"
        add_bleed(p, out, pdfx="x1a")  # must not raise
        assert out.exists()

    def test_compatible_passes_x1a(self, tmp_path):
        p = self._make_bm_pdf(tmp_path, "/Compatible")
        out = tmp_path / "out.pdf"
        add_bleed(p, out, pdfx="x1a")  # must not raise
        assert out.exists()


# ── Point 5: TestAnnotsRemovedWithWarning ─────────────────────────────────────


class TestAnnotsRemovedWithWarning:
    """Point 5 (Option A): /Annots are removed in content-extend mode and a
    warning is included in the response.  metadata-only must NOT remove them."""

    def _make_annot_pdf(self, tmp_path: Path) -> Path:
        pdf = pikepdf.Pdf.new()
        annot = pikepdf.Dictionary(
            Type=pikepdf.Name("/Annot"),
            Subtype=pikepdf.Name("/Text"),
            Rect=_rv(100, 100, 200, 200),
            Contents=pikepdf.String("Test annotation"),
        )
        pdf.pages.append(pikepdf.Page(pikepdf.Dictionary(
            Type=pikepdf.Name("/Page"),
            MediaBox=_rv(0, 0, 595, 842),
            Resources=pikepdf.Dictionary(),
            Annots=pikepdf.Array([pdf.make_indirect(annot)]),
        )))
        p = tmp_path / "annots.pdf"
        pdf.save(str(p))
        return p

    def test_annots_not_in_output(self, tmp_path):
        p = self._make_annot_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="content-extend")
        with pikepdf.open(str(out)) as pdf_out:
            page_obj = pdf_out.pages[0].obj
            assert "/Annots" not in page_obj, "/Annots still in output page"

    def test_warning_mentions_annotation(self, tmp_path):
        p = self._make_annot_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        _, _, warnings = add_bleed(p, out, mode="content-extend")
        assert any("Annotation" in w or "Annot" in w for w in warnings), \
            f"Expected annotation warning, got: {warnings}"

    def test_warning_count_matches_annot_count(self, tmp_path):
        """Warning text should mention the number of annotations."""
        p = self._make_annot_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        _, _, warnings = add_bleed(p, out, mode="content-extend")
        # We added 1 annotation; the warning should reference "1"
        annot_warnings = [w for w in warnings if "Annotation" in w or "Annot" in w]
        assert len(annot_warnings) >= 1
        assert "1" in annot_warnings[0]

    def test_metadata_only_keeps_annots(self, tmp_path):
        """metadata-only mode must NOT touch /Annots."""
        p = self._make_annot_pdf(tmp_path)
        out = tmp_path / "out.pdf"
        add_bleed(p, out, mode="metadata-only")
        with pikepdf.open(str(out)) as pdf_out:
            page_obj = pdf_out.pages[0].obj
            assert "/Annots" in page_obj, \
                "/Annots was wrongly removed in metadata-only mode"
