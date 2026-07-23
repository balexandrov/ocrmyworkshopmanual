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
    """The whole point: a born-digital file arrives at dest byte-for-byte identical,
    with no render/OCR done to it."""
    src = U.make_born_digital_pdf(tmp_path / 'src.pdf', npages=3)
    dest = tmp_path / 'out' / 'src.pdf'
    res = U.owm.compress_one(str(src), str(dest), 200, ocr=False)
    assert res.get('action') == 'born_digital', res
    assert res.get('err') is None, res
    assert dest.read_bytes() == src.read_bytes(), 'born-digital output is not byte-identical to input'
