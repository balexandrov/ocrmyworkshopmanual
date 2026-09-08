"""Tests for the raster-size projection that keeps OCR off absurd page geometry.

ocrmypdf rasterises at `max(image dpi, 400 when a page has text or vector content)` and
that 400 is a floor no CLI option lowers. A page whose declared size does not match its
raster therefore gets blown up by whatever ratio separates the two -- past PIL's 500 MP
bomb guard that is not a slow OCR, it is a hard failure and no text layer at all.

The file this was written for laid every image out at one pixel per POINT, so its pages
declare ~71 x 97 in while holding ~29 MP of scan.
"""
import pikepdf
import pytest
from pypdf import PdfReader

import _util as U

owm = U.owm


def _one_pixel_per_point_pdf(path, npages=2, img_px=1200):
    """A page shaped like the real offender: the image drawn at 1 px = 1 pt, so the page
    box is as many POINTS as the image has PIXELS -- a ~17 x 22 in page holding a 72 dpi
    scan. Same unit mistake as the archive file, at a size a test can afford."""
    import zlib
    pdf = pikepdf.Pdf.new()
    h = round(img_px * 4 / 3)
    row = bytes([0xFF] * ((img_px + 7) // 8))
    dark = bytes([0x00] * len(row))
    raw = b''.join(dark if 100 < y < 160 else row for y in range(h))
    img = pikepdf.Stream(pdf, zlib.compress(raw))
    img.Type, img.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
    img.Width, img.Height = img_px, h
    img.ColorSpace, img.BitsPerComponent = pikepdf.Name.DeviceGray, 1
    img.Filter = pikepdf.Name.FlateDecode
    img = pdf.make_indirect(img)
    for _i in range(npages):
        page = pdf.add_blank_page(page_size=(img_px, h))      # POINTS == PIXELS
        page.Contents = pikepdf.Stream(
            pdf, f'q {img_px} 0 0 {h} 0 0 cm /Im0 Do Q'.encode())
        page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=img))
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(str(path))
    return path


def test_geometry_reads_page_inches_and_image_pixels(tmp_path):
    p = _one_pixel_per_point_pdf(tmp_path / 'pxpt.pdf', npages=1, img_px=1200)
    w_in, h_in, bw, bh = owm._page_geometry(PdfReader(str(p)).pages[0])
    assert round(w_in, 2) == round(1200 / 72, 2)        # points -> inches
    assert (bw, bh) == (1200, 1600)
    # the whole point of the file: its raster is only 72 dpi at the size it declares
    assert round(bw / w_in) == 72


def test_projection_flags_a_page_that_would_blow_the_budget(tmp_path):
    """400 dpi against a 16.7 x 22.2 in page is ~59 MP -- under the budget. Scale the page
    up and the projection has to rise with the AREA, which is what makes a 71 x 97 in page
    reach 1,201 MP while holding the same ~29 MP of scan."""
    small = _one_pixel_per_point_pdf(tmp_path / 'small.pdf', npages=1, img_px=1200)
    mp, pg, native = owm._ocrmypdf_raster_mp(small)
    assert pg == 1 and round(native) == 72
    # projected at ocrmypdf's floor, not at the native resolution
    assert mp == pytest.approx((1200 / 72 * 400) * (1600 / 72 * 400) / 1e6, rel=0.01)

    big = _one_pixel_per_point_pdf(tmp_path / 'big.pdf', npages=1, img_px=5100)
    mp_big, _pg, _n = owm._ocrmypdf_raster_mp(big)
    assert mp_big > owm._OCR_RASTER_BUDGET_MP          # this is the case that must reroute
    assert mp_big > mp * 15                            # grows with area


def test_a_normal_scan_is_not_flagged(tmp_path):
    """The guard must not divert ordinary files: a 300 dpi letter page is ~15 MP at the
    400 floor, nowhere near the budget, and diverting it would change how the whole
    archive is OCR'd."""
    p = U.make_scan_pdf(tmp_path / 'normal.pdf', npages=2, dpi=300)
    mp, _pg, _native = owm._ocrmypdf_raster_mp(p)
    assert 0 < mp < owm._OCR_RASTER_BUDGET_MP


def test_budget_sits_below_pils_bomb_limit(tmp_path):
    """PIL refuses over 500 MP. The budget has to leave headroom for the RGB copy
    ocrmypdf makes alongside, or we would hand it something it still refuses."""
    assert owm._OCR_RASTER_BUDGET_MP < 500.0


def test_projection_survives_an_unreadable_file(tmp_path):
    """Must never raise: it runs before OCR on every preserve-images file, and a corrupt
    source has its own reporting path."""
    junk = tmp_path / 'junk.pdf'
    junk.write_bytes(b'not a pdf at all')
    assert owm._ocrmypdf_raster_mp(junk) == (0.0, 0, 0.0)


def test_projection_ignores_a_page_with_no_mediabox(tmp_path):
    p = _one_pixel_per_point_pdf(tmp_path / 'nobox.pdf', npages=1, img_px=1200)
    with pikepdf.open(str(p), allow_overwriting_input=True) as pdf:
        del pdf.pages[0].obj['/MediaBox']
        pdf.Root.Pages.MediaBox = pikepdf.Array([0, 0, 612, 792])
        pdf.save()
    mp, _pg, _n = owm._ocrmypdf_raster_mp(p)      # inherited box, no crash
    assert mp >= 0.0
