"""--replace-text, scans stored as stencil masks, and the exit status of a failed run.

Three faults one dealer-CD Japanese manual (432 pages) hit together:

* its old OCR layer read kanji as Latin noise on a sixth of its pages and was missing from
  54; kept or carried, that layer ships. --replace-text drops it so every page is OCR'd.
* its pages without a layer had been re-saved by Illustrator, which stores the 1-bit scan as
  an /ImageMask stencil. ocrmypdf reads such a page as 0 dpi, and with the paddle engine it
  divides by that dpi: ZeroDivisionError, the whole file FAILED.
* the run still exited 0, so the caller announced "Done" over the untouched file.
"""
import subprocess
import sys

import pytest
from pypdf import PdfReader

import _util as U

owm = U.owm
_missing = U.tools_missing()
_no_ocr = U.ocr_missing()

LAYER = 'removal and installation'          # what `make_ocr_layer_pdf` writes, every page


def _texts(pdf):
    return [p.extract_text() or '' for p in PdfReader(str(pdf)).pages]


def _set_ops(pdf_path, ops: bytes):
    """Replace page 1's content with the scan plus `ops`, keeping its image and font."""
    import pikepdf
    with pikepdf.open(str(pdf_path), allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        U._add_font(pdf, page.obj.Resources, '/F1')
        (iname, _), = list(page.obj.Resources.XObject.items())
        mb = [float(v) for v in page.mediabox]
        page.Contents = pikepdf.Stream(
            pdf, f'q {mb[2]:g} 0 0 {mb[3]:g} 0 0 cm {iname} Do Q '.encode() + ops)
        pdf.save()


def make_stencil_scan_pdf(path, npages=2, dpi=150):
    """A scan whose image is a 1-bit /ImageMask stencil, wrapped in a `/Layer` marked-content
    block -- the shape an Illustrator re-save leaves (measured on the dealer CD's pages)."""
    import zlib
    import pikepdf
    from PIL import Image
    U.make_scan_pdf(path, npages=npages, dpi=dpi)
    with pikepdf.open(str(path), allow_overwriting_input=True) as pdf:
        for page in pdf.pages:
            res = page.obj.Resources
            (iname, img), = list(res.XObject.items())
            im = pikepdf.PdfImage(img).as_pil_image().convert('1')
            bits = Image.eval(im, lambda v: 255 - v).tobytes()   # 1 = paint, as a stencil
            mask = pikepdf.Stream(pdf, zlib.compress(bits))
            mask.Type, mask.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
            mask.Width, mask.Height = im.width, im.height
            mask.ImageMask, mask.BitsPerComponent = True, 1
            mask.Decode = pikepdf.Array([1, 0])
            mask.Filter = pikepdf.Name.FlateDecode
            res.XObject = pikepdf.Dictionary({iname: pdf.make_indirect(mask)})
            mb = [float(v) for v in page.mediabox]
            page.Contents = pikepdf.Stream(pdf, (
                f'/Layer <</Title (Layer 1)/Visible true>> BDC q {mb[2]:g} 0 0 {mb[3]:g} 0 0 cm '
                f'{iname} Do Q EMC ').encode())
        pdf.save()
    return path


# ── stripping the layer ───────────────────────────────────────────────────────

@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_an_invisible_layer_is_stripped_and_the_pages_render_the_same(tmp_path):
    src = U.make_ocr_layer_pdf(tmp_path / 'src.pdf', npages=3)
    out = tmp_path / 'out.pdf'
    res = owm._strip_text_layer(src, out)
    assert res == {'pages': 3, 'left': 0, 'err': None}
    assert not any(t.strip() for t in _texts(out))
    assert len(PdfReader(str(out)).pages) == 3


def test_visible_text_is_never_stripped(tmp_path):
    src = U.make_born_digital_pdf(tmp_path / 'bd.pdf', npages=2)
    out = tmp_path / 'out.pdf'
    res = owm._strip_text_layer(src, out)
    assert res['pages'] == 0 and res['err'] is None
    assert not out.exists(), 'nothing to strip means nothing written'


def test_a_block_kept_after_a_dropped_one_leaves_the_page_whole(tmp_path):
    """Text state outlives ET: the second block shows its glyph in the mode the first set."""
    import pikepdf
    src = U.make_scan_pdf(tmp_path / 'src.pdf', npages=1)
    _set_ops(src, b'BT 3 Tr /F1 11 Tf 60 700 Td (hidden) Tj ET '
                  b'BT 0 Tr /F1 11 Tf 60 600 Td (shown) Tj ET ')
    with pikepdf.open(str(src)) as p:
        assert owm._strip_invisible_ops(p.pages[0]) is None


def test_a_mixed_block_is_kept_whole(tmp_path):
    """Dropping one show operator would move every glyph after it."""
    import pikepdf
    src = U.make_scan_pdf(tmp_path / 'src.pdf', npages=1)
    _set_ops(src, b'BT /F1 11 Tf 60 700 Td 3 Tr (hidden) Tj 0 Tr (shown) Tj ET ')
    with pikepdf.open(str(src)) as p:
        assert owm._strip_invisible_ops(p.pages[0]) is None


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
@pytest.mark.skipif(_no_ocr is not None, reason=str(_no_ocr))
def test_replace_text_ocrs_the_page_instead_of_keeping_its_layer(tmp_path):
    src = U.make_ocr_layer_pdf(tmp_path / 'src.pdf', npages=2)
    out = tmp_path / 'out.pdf'
    res = owm.compress_one(str(src), str(out), 200, ocr=True, language='eng',
                           replace_text=True)
    assert not res.get('err'), res
    assert 'source text layer dropped on 2 pg' in res['note']
    texts = _texts(out)
    assert all('Scan page' in t for t in texts), 'every page carries the new OCR'
    assert not any(LAYER in t for t in texts), 'the old layer must be gone'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_without_replace_text_the_layer_is_kept(tmp_path):
    src = U.make_ocr_layer_pdf(tmp_path / 'src.pdf', npages=2)
    out = tmp_path / 'out.pdf'
    res = owm.compress_one(str(src), str(out), 200, ocr=True, language='eng')
    assert not res.get('err'), res
    assert all(LAYER in t for t in _texts(out))


def test_replace_text_needs_ocr():
    r = subprocess.run([sys.executable, str(U.REPO_ROOT / 'ocrmyworkshopmanual.py'),
                        'x.pdf', '--replace-text', '--no-ocr'],
                       capture_output=True, text=True)
    assert r.returncode == 1 and '--replace-text needs OCR' in r.stderr


# ── stencil-mask scans ────────────────────────────────────────────────────────

def test_stencil_only_pages_are_found(tmp_path):
    assert owm._stencil_only_pages(make_stencil_scan_pdf(tmp_path / 's.pdf')) == [1, 2]
    assert owm._stencil_only_pages(U.make_scan_pdf(tmp_path / 'n.pdf', npages=2)) == []


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_a_stencil_scan_reads_as_zero_dpi_to_ocrmypdf(tmp_path):
    """The premise of the route: if ocrmypdf ever counts a stencil, this fails and the
    route can go."""
    from ocrmypdf.pdfinfo import PdfInfo
    info = PdfInfo(make_stencil_scan_pdf(tmp_path / 's.pdf', npages=1))
    assert info[0].dpi.x == 0


try:
    import ocrmypdf_paddle  # noqa: F401
    _no_paddle = None
except Exception as ex:
    _no_paddle = f'paddle engine unavailable: {ex}'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
@pytest.mark.skipif(_no_paddle is not None, reason=str(_no_paddle))
def test_paddle_ocrs_a_stencil_scan_from_a_native_render(tmp_path, monkeypatch):
    monkeypatch.setattr(owm, 'OCR_ENGINE', 'paddle')
    monkeypatch.setattr(owm, 'MIN_COMPRESS_MB', 5.0)        # keep the original: OCR only
    src = make_stencil_scan_pdf(tmp_path / 's.pdf', npages=1)
    out = tmp_path / 'out.pdf'
    res = owm.compress_one(str(src), str(out), 200, ocr=True, language='eng')
    assert not res.get('err'), res
    assert 'stencil mask' in res['note']
    assert 'Scan page' in _texts(out)[0]


# ── exit status ───────────────────────────────────────────────────────────────

def test_a_failed_file_makes_the_run_exit_nonzero(tmp_path):
    bad = tmp_path / 'bad.pdf'
    bad.write_bytes(b'%PDF-1.4\nnot a pdf at all\n')
    r = subprocess.run([sys.executable, str(U.REPO_ROOT / 'ocrmyworkshopmanual.py'),
                        str(bad), '--dest', str(tmp_path / 'out'), '--no-ocr'],
                       capture_output=True, text=True)
    assert 'failed 1' in r.stdout, r.stdout[-500:]
    assert r.returncode == 1
