"""Born-digital SAFETY-CHECK tests.

A born-digital (vector/text) PDF must NEVER be rasterised — the tool must copy it to
dest byte-for-byte, untouched. A genuine scan must NOT be mistaken for born-digital
(or the whole archive would be skipped). These tests exercise both directions plus
the end-to-end copy-through via compress_one().
"""
import shutil

import pytest

import _util as U
from _util import TYPE_DIRS


def test_born_digital_pdf_is_detected(tmp_path):
    pdf = U.make_born_digital_pdf(tmp_path / 'born.pdf', npages=4)
    born, sig = U.owm.looks_born_digital(pdf)
    assert born is True, f'vector/text PDF not flagged born-digital: {sig}'
    assert sig['scan_pages'] == 0 and sig['text_pages'] > 0, sig


_missing = U.tools_missing()


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
@pytest.mark.parametrize('page_type', list(TYPE_DIRS))
def test_scanned_fixtures_are_not_born_digital(page_type):
    """Every real scanned fixture (and the synthetic image-based blank) must read as
    a scan, never born-digital — otherwise the tool would skip compressing it."""
    pdfs = U.fixture_pdfs(page_type)
    if not pdfs:
        pytest.skip(f'no {page_type} fixtures')
    for pdf in pdfs:
        born, sig = U.owm.looks_born_digital(pdf)
        assert born is False, f'{pdf.name} wrongly flagged born-digital: {sig}'


def test_compress_one_copies_born_digital_untouched(tmp_path):
    """The whole point: a born-digital file is never rasterised. With --no-lossless it
    arrives at dest byte-for-byte identical, with no render/OCR done to it."""
    src = U.make_born_digital_pdf(tmp_path / 'src.pdf', npages=3)
    dest = tmp_path / 'out' / 'src.pdf'
    res = U.owm.compress_one(str(src), str(dest), 200, ocr=False, lossless=False)
    assert res.get('action') == 'born_digital', res
    assert res.get('err') is None, res
    assert dest.read_bytes() == src.read_bytes(), 'born-digital output is not byte-identical to input'


# ── Page content wrapped in a Form XObject ───────────────────────────────────────
# The detectors disagreed: _largest_image_dpi recursed into Form XObjects while
# _visible_text_chars did not, so a page wrapping everything in one form read as
# "full-page raster, no text" — a scan. Measured on 25/26_Subaru (iText): ~1070 and 590
# chars/page of visible type, reported as 0, and 26 was rasterised to 39% and re-OCR'd.

def test_visible_text_inside_a_form_xobject_is_seen(tmp_path):
    """Text nested one level down still counts as real text to protect."""
    from pypdf import PdfReader
    pdf = U.make_form_wrapped_pdf(tmp_path / 'wrapped_text.pdf', with_text=True)
    page = PdfReader(str(pdf)).pages[0]
    assert U.owm._largest_image_dpi(page) >= U.owm.VECTOR_DPI_FLOOR, \
        'fixture must look like a full-page raster, else the test proves nothing'
    assert U.owm._visible_text_chars(page) >= 100, 'visible text inside the form was missed'
    born, sig = U.owm.looks_born_digital(pdf)
    assert born, f'a page of real vector type must not read as a scan: {sig}'


def test_image_only_form_wrapped_page_still_compresses(tmp_path):
    """The other half: a genuine image-only scan that happens to be wrapped in a form must
    NOT start claiming it has real text, or a large slice of the archive stops compressing.
    22 of the 24 wrapped files in the sample corpus are exactly this."""
    from pypdf import PdfReader
    pdf = U.make_form_wrapped_pdf(tmp_path / 'wrapped_scan.pdf', with_text=False)
    page = PdfReader(str(pdf)).pages[0]
    assert U.owm._visible_text_chars(page) == 0, 'an image-only page must report no text'
    born, sig = U.owm.looks_born_digital(pdf)
    assert not born, f'an image-only scan must stay compressible: {sig}'


def test_text_hidden_under_a_later_image_still_does_not_count(tmp_path):
    """The hidden-text rule must survive the new recursion. Text painted BEFORE a full-page
    image is under it — a searchable layer, not publisher content. Judging by render mode
    alone blocked compression of a 258 MB RAV4 manual, which is why the ordering test
    exists; it has to be evaluated inside each stream, never across the nesting boundary."""
    import pikepdf
    from pypdf import PdfReader
    pdf = U.make_form_wrapped_pdf(tmp_path / 'wrapped_hidden.pdf', with_text=True)
    with pikepdf.open(str(pdf), allow_overwriting_input=True) as p:
        form = p.pages[0].Resources.XObject.Xf1
        body = bytes(form.read_bytes())
        image_draw, text = body.split(b'\n', 1)[0], body.split(b'\n', 1)[1]
        form.write(text + b'\n' + image_draw + b'\n')   # text first, image painted over it
        p.save(str(tmp_path / 'hidden.pdf'))
    page = PdfReader(str(tmp_path / 'hidden.pdf')).pages[0]
    assert U.owm._visible_text_chars(page) == 0, \
        'text painted under a full-page image must not veto compression'
