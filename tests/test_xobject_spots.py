"""Test that spot colours in Form XObjects are found by recursive extraction."""

import os
import tempfile

import pikepdf
import pytest

from app.preflight.rules.spot_colors import _extract_spot_colors_pikepdf
from app.preflight.rules.spotcolor_list import check_spot_colors_list


def _make_pdf_with_xobject_separation(spot_name: str = "cutkontur") -> str:
    """Create a minimal PDF where the Separation is ONLY in a Form XObject's Resources.

    The page itself has NO /ColorSpace entries – the Separation is nested inside
    a Form XObject referenced via /XObject in the page Resources.
    """
    pdf = pikepdf.new()

    # Add a blank page first (pikepdf requires Page objects)
    pdf.add_blank_page(page_size=(612, 792))
    page = pdf.pages[0]

    # Build a tint transform function (exponential interpolation)
    tint_fn = pikepdf.Dictionary(
        FunctionType=2,
        Domain=[0, 1],
        C0=[0, 0, 0, 0],
        C1=[0, 1, 0, 0],  # Magenta
        N=1,
    )
    tint_fn_ref = pdf.make_indirect(tint_fn)

    # Build the Separation color space array
    sep_cs = pikepdf.Array([
        pikepdf.Name.Separation,
        pikepdf.Name("/" + spot_name),
        pikepdf.Name.DeviceCMYK,
        tint_fn_ref,
    ])

    # Form XObject with this separation in its Resources
    form_stream = pdf.make_stream(b"/CS1 cs 1 scn 100 100 200 200 re f")
    form_stream[pikepdf.Name.Type] = pikepdf.Name.XObject
    form_stream[pikepdf.Name.Subtype] = pikepdf.Name.Form
    form_stream[pikepdf.Name.BBox] = [0, 0, 612, 792]
    form_stream[pikepdf.Name.Resources] = pikepdf.Dictionary(
        ColorSpace=pikepdf.Dictionary(CS1=sep_cs),
    )

    # Page has NO /ColorSpace – only references the Form XObject
    page[pikepdf.Name.Resources] = pikepdf.Dictionary(
        XObject=pikepdf.Dictionary(Fm1=pdf.make_indirect(form_stream)),
    )
    page[pikepdf.Name.Contents] = pdf.make_stream(b"/Fm1 Do")

    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    pdf.save(path)
    pdf.close()
    return path


class TestXObjectSpotExtraction:
    """Test that _extract_spot_colors_pikepdf finds spots in Form XObjects."""

    def test_spot_in_xobject_found(self):
        """Separation defined only in Form XObject → must be found."""
        path = _make_pdf_with_xobject_separation("cutkontur")
        try:
            spots = _extract_spot_colors_pikepdf(path)
            assert "CUTKONTUR" in spots
            assert spots["CUTKONTUR"]["original_name"] == "cutkontur"
        finally:
            os.unlink(path)

    def test_spot_in_xobject_found_bohrungen(self):
        """Separation 'Bohrungen' in Form XObject → must be found."""
        path = _make_pdf_with_xobject_separation("Bohrungen")
        try:
            spots = _extract_spot_colors_pikepdf(path)
            assert "BOHRUNGEN" in spots
        finally:
            os.unlink(path)

    def test_spotcolor_list_finds_xobject_spots(self):
        """check_spot_colors_list should find spots nested in Form XObjects."""
        path = _make_pdf_with_xobject_separation("cutkontur")
        try:
            result = check_spot_colors_list(path, {})
            names = [sc.name for sc in result.spot_colors]
            assert "cutkontur" in names
            assert result.total_count >= 1
        finally:
            os.unlink(path)

    def test_spot_not_on_page_directly(self):
        """The page-level Resources should NOT have the spot – only the XObject."""
        path = _make_pdf_with_xobject_separation("cutkontur")
        try:
            # Verify spot is NOT on page directly (it's in the XObject)
            with pikepdf.open(path) as pdf:
                page = pdf.pages[0]
                page_cs = page.get("/Resources", {}).get("/ColorSpace")
                # Page should have no ColorSpace dict
                assert page_cs is None

            # But our extractor should still find it
            spots = _extract_spot_colors_pikepdf(path)
            assert "CUTKONTUR" in spots
        finally:
            os.unlink(path)
