"""Carrying a scan's OWN invisible text layer onto its compressed pages.

A scan an OCR engine already made searchable used to be re-OCR'd by Tesseract when it was
compressed, and the audit refused the result whenever Tesseract read worse than the engine
had. Measured on a 138-page Japanese manual made searchable by ABBYY FineReader: word recall
0.20, FAILED, original kept. The engine's layer is separable from the scan, so it is carried
across instead — and every page whose text is NOT safely separable still goes through OCR.
"""
import pytest
from pypdf import PdfReader

import _util as U

_missing = U.tools_missing()
owm = U.owm


def _texts(pdf):
    return [(p.extract_text() or '').split() for p in PdfReader(str(pdf)).pages]


def _page(pdf, k=0):
    import pikepdf
    p = pikepdf.open(str(pdf))
    return p, p.pages[k]


# ── which pages count as separable ────────────────────────────────────────────

def test_engine_layer_over_a_form_wrapped_scan_is_separable(tmp_path):
    """The ABBYY shape: an image-only Form for the scan, one invisible BT..ET. The Form must
    not be mistaken for the text-holding kind."""
    f = U.make_engine_layer_pdf(tmp_path / 'e.pdf', npages=3)
    assert owm._carried_text_pages(f) == {0, 1, 2}


def test_inline_ocr_layer_is_separable(tmp_path):
    f = U.make_ocr_layer_pdf(tmp_path / 'o.pdf', npages=3)
    assert owm._carried_text_pages(f) == {0, 1, 2}


def test_visible_text_is_not_separable(tmp_path):
    """Publisher type drawn visibly: lifting only the text out would lose nothing, but the
    page is not an OCR layer, and its fate is the existing path's call, not this one's."""
    f = U.make_stamped_body_over_scan_pdf(tmp_path / 'v.pdf', npages=3)
    p, pg = _page(f)
    with p:
        assert owm._invisible_text_ops(pg) is None
    assert owm._carried_text_pages(f) == set()


def test_text_inside_a_form_is_not_separable(tmp_path):
    """Only the page's own stream is carried, so text a Form holds would be silently lost."""
    f = U.make_form_wrapped_pdf(tmp_path / 'w.pdf', with_text=True)
    assert owm._carried_text_pages(f) == set()


def test_a_scan_without_text_has_nothing_to_carry(tmp_path):
    f = U.make_scan_pdf(tmp_path / 's.pdf', npages=2)
    assert owm._carried_text_pages(f) == set()


def test_rotated_page_is_not_carried(tmp_path):
    """Ghostscript bakes /Rotate into the render, so a plain scale would misplace the text."""
    import pikepdf
    f = U.make_engine_layer_pdf(tmp_path / 'r.pdf', npages=2)
    with pikepdf.open(str(f), allow_overwriting_input=True) as p:
        p.pages[1].obj['/Rotate'] = 90
        p.save()
    assert owm._carried_text_pages(f) == {0}


def test_a_stamp_only_layer_is_not_carried(tmp_path):
    """An invisible layer that only repeats the same line on every page is a stamp, not a
    text layer; carrying it would stand in for the OCR the page needs."""
    f = U.make_engine_layer_pdf(tmp_path / 'b.pdf', npages=4,
                                line=lambda k, j: 'www.example-manuals.test' if j == 0 else '')
    boiler = owm._text_sample(f).boiler
    assert boiler, 'fixture must repeat a line on every page'
    assert owm._carried_text_pages(f, boiler) == set()


def test_text_is_mapped_onto_a_differently_sized_page(tmp_path):
    """The compressed page's box need not equal the source's; the Form's /Matrix scales the
    layer onto it, so each word stays over its pixels."""
    f = U.make_engine_layer_pdf(tmp_path / 'm.pdf', npages=1)
    p, pg = _page(f)
    with p:
        sb = [float(v) for v in pg.mediabox]
        xo = owm._carried_text_xobject(p, pg, [0, 0, sb[2] / 2, sb[3] / 2])
        assert [float(v) for v in xo.Matrix] == [0.5, 0, 0, 0.5, 0, 0]
        assert b'Do' not in xo.read_bytes(), 'the scan must not be drawn a second time'


# ── end to end ────────────────────────────────────────────────────────────────

@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_engine_layer_survives_compression_word_for_word(tmp_path, monkeypatch):
    """The regression: compressed, not FAILED; every page's text identical to the source's;
    and Tesseract never ran, because no page needed it."""
    src = U.make_engine_layer_pdf(tmp_path / 'src.pdf', npages=3, dpi=300)
    monkeypatch.setattr(owm, '_ocr_source', lambda *a, **k: pytest.fail('OCR must not run'))
    out = tmp_path / 'out.pdf'
    res = owm.compress_one(str(src), str(out), 200, ocr=True, language='eng')
    assert res.get('err') is None and res['action'] == 'compressed', res
    assert res['ocr_state'] == owm.OCR_KEPT, res
    assert 'source text layer kept on 3 of 3 pg' in res['note'], res['note']
    assert _texts(out) == _texts(src)
    assert out.stat().st_size < src.stat().st_size


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_no_ocr_still_keeps_the_existing_layer(tmp_path):
    """--no-ocr means "run no OCR", not "drop the text the file already has"."""
    src = U.make_engine_layer_pdf(tmp_path / 'src.pdf', npages=2, dpi=300)
    out = tmp_path / 'out.pdf'
    res = owm.compress_one(str(src), str(out), 200, ocr=False)
    assert res.get('err') is None and res['action'] == 'compressed', res
    assert res['ocr_state'] == owm.OCR_NONE
    assert _texts(out) == _texts(src)


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
@pytest.mark.skipif(U.ocr_missing() is not None, reason=str(U.ocr_missing()))
def test_only_the_page_without_a_layer_is_ocred(tmp_path):
    """Mixed file: pages with a layer keep it, the one without gets OCR — and only that one
    is handed to OCR."""
    import pikepdf
    src = U.make_engine_layer_pdf(tmp_path / 'src.pdf', npages=3, dpi=300)
    with pikepdf.open(str(src), allow_overwriting_input=True) as p:
        p.pages[1].Contents = pikepdf.Stream(p, b'q /Fg Do Q ')
        p.save()
    seen = []
    real = owm._ocr_render_pdf

    def spy(work, pngs, page_dpi, base_dpi, skip_pages):
        seen.append(set(skip_pages))
        return real(work, pngs, page_dpi, base_dpi, skip_pages)
    import unittest.mock as um
    out = tmp_path / 'out.pdf'
    with um.patch.object(owm, '_ocr_render_pdf', spy):
        res = owm.compress_one(str(src), str(out), 200, ocr=True, language='eng')
    assert res.get('err') is None and res['action'] == 'compressed', res
    assert seen == [{0, 2}], seen
    got, want = _texts(out), _texts(src)
    assert got[0] == want[0] and got[2] == want[2]
    assert got[1], 'the page without a layer must have been OCR\'d'
