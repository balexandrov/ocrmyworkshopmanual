"""--ocr-engine paddle: PaddleOCR PP-OCRv6 through the `ocrmypdf_paddle` plugin.

The plugin only hands OCRmyPDF positioned lines, so what these tests pin is the seam: that
the tool routes a file to the plugin exactly when the model can read its language, that the
text reaches the shipped layer on both OCR paths (compress, and keep-the-original), and that
vertical lines are not dropped by OCRmyPDF's renderer.
"""
import pytest
from pypdf import PdfReader

import _util as U

owm = U.owm
_missing = U.tools_missing()
try:
    import ocrmypdf_paddle as paddle
    _no_paddle = None
except Exception as ex:                      # rapidocr / onnxruntime not installed
    paddle, _no_paddle = None, f'paddle engine unavailable: {ex}'

needs_paddle = pytest.mark.skipif(_no_paddle is not None, reason=str(_no_paddle))


@pytest.fixture
def paddle_engine(monkeypatch):
    monkeypatch.setattr(owm, 'OCR_ENGINE', 'paddle')


# ── routing ───────────────────────────────────────────────────────────────────

def test_tesseract_is_the_default_and_adds_nothing():
    assert owm.OCR_ENGINE == 'tesseract'
    assert owm._engine_args('jpn+eng') == ([], '')


@needs_paddle
def test_a_language_the_model_reads_goes_to_the_plugin(paddle_engine):
    args, note = owm._engine_args('jpn+eng')
    assert args == ['--plugin', str(owm.PADDLE_PLUGIN)]
    assert note == ' (engine paddle)'


@needs_paddle
def test_a_language_it_does_not_read_stays_on_tesseract(paddle_engine):
    """Cyrillic OCR'd by a model without Cyrillic would replace a missing layer with noise."""
    args, note = owm._engine_args('rus+eng')
    assert args == []
    assert 'engine tesseract' in note and 'rus' in note


@needs_paddle
def test_the_startup_check_passes_where_the_plugin_loads():
    assert owm._paddle_error() == ''


# ── the plugin itself ─────────────────────────────────────────────────────────

@needs_paddle
def test_a_vertical_line_is_rotated_not_left_to_be_dropped():
    """OCRmyPDF suppresses a line whose box does not fit its text, so an unrotated
    vertical line would vanish from the layer."""
    tall = paddle._line([(0, 0), (40, 0), (40, 400), (0, 400)], 'コントロールユニット', 0.9)
    wide = paddle._line([(0, 0), (400, 0), (400, 40), (0, 40)], 'コントロールユニット', 0.9)
    one = paddle._line([(0, 0), (20, 0), (20, 60), (0, 60)], 'I', 0.9)
    assert tall.textangle == 270.0
    assert wide.textangle is None
    assert one.textangle is None, 'a single tall glyph is not a vertical line'


@needs_paddle
def test_ocr_page_reads_a_rendered_scan(tmp_path):
    src = U.make_scan_pdf(tmp_path / 's.pdf', npages=1)
    png = tmp_path / 'p.png'
    assert U.render_gray(src, 1, 300, png)
    page, text = paddle.ocr_page(png)
    assert 'Scan page 1 line' in text, text[:200]
    assert page.children and all(ln.children for ln in page.children)


@needs_paddle
def test_concurrent_pages_do_not_crash_the_engine(tmp_path):
    """OCRmyPDF calls the engine from several THREADS at once (its default executor). On a
    GPU provider that crashed the whole process (0xC0000005) until calls were serialised —
    measured with --jobs 6 on a 138-page manual, which then shipped without a text layer.
    On a machine without a GPU this runs the CPU path, which must stay concurrent-safe too."""
    import threading
    src = U.make_scan_pdf(tmp_path / 's.pdf', npages=4, dpi=150)
    pngs = []
    for n in range(1, 5):
        png = tmp_path / f'p{n}.png'
        assert U.render_gray(src, n, 150, png)
        pngs.append(png)
    got, errs = {}, []

    def work(n, png):
        try:
            got[n] = paddle.ocr_page(png)[1]
        except Exception as ex:
            errs.append(repr(ex))
    ts = [threading.Thread(target=work, args=(n, p)) for n, p in enumerate(pngs, 1)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs, errs
    for n in range(1, 5):
        assert f'Scan page {n} line' in got[n], (n, got[n][:120])


# ── end to end, through the tool ──────────────────────────────────────────────

@needs_paddle
@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_compress_path_ships_the_paddle_layer(tmp_path, paddle_engine):
    src = U.make_scan_pdf(tmp_path / 'src.pdf', npages=2)
    out = tmp_path / 'out.pdf'
    res = owm.compress_one(str(src), str(out), 200, ocr=True, language='eng')
    assert res.get('err') is None and res['action'] == 'compressed', res
    assert res['ocr_state'] == owm.OCR_NEW, res
    assert '(engine paddle)' in res['note'], res['note']
    text = PdfReader(str(out)).pages[1].extract_text() or ''
    assert 'Scan page 2 line' in text, text[:200]


@needs_paddle
@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_keep_original_path_also_uses_it(tmp_path, paddle_engine, monkeypatch):
    """Under the size floor the ORIGINAL images ship and OCR runs in --redo-ocr mode —
    a different ocrmypdf pipeline, so it has to be proven separately."""
    monkeypatch.setattr(owm, 'MIN_COMPRESS_MB', 5.0)
    src = U.make_scan_pdf(tmp_path / 'small.pdf', npages=1)
    out = tmp_path / 'out.pdf'
    res = owm.compress_one(str(src), str(out), 200, ocr=True, language='eng')
    assert res.get('err') is None and res['reason'] == owm.REASON_SMALL, res
    assert res['ocr_state'] == owm.OCR_NEW, res
    assert '(engine paddle)' in res['note'], res['note']
    assert 'Scan page 1 line' in (PdfReader(str(out)).pages[0].extract_text() or '')
