"""--ocr-engine yomitoku: YomiToku through the `ocrmypdf_yomitoku` plugin.

Like the paddle plugin it only hands OCRmyPDF positioned lines, so what these tests pin is
the seam -- routing by language, the startup check, the text reaching the shipped layer on
both OCR paths -- plus what this plugin does on its own: reading a page printed sideways
turned upright, with the lines mapped back onto the page as it lies.
"""
import unicodedata

import pytest
from pypdf import PdfReader

import _util as U

owm = U.owm
_missing = U.tools_missing()
try:
    import ocrmypdf_yomitoku as yomi
    _no_yomi = None
except Exception as ex:                      # yomitoku / torch not installed
    yomi, _no_yomi = None, f'yomitoku engine unavailable: {ex}'

needs_yomi = pytest.mark.skipif(_no_yomi is not None, reason=str(_no_yomi))


@pytest.fixture
def yomi_engine(monkeypatch):
    monkeypatch.setattr(owm, 'OCR_ENGINE', 'yomitoku')


def _jp_font(px):
    """A Japanese TrueType face, or None where the machine has none."""
    from PIL import ImageFont
    for name in ('msgothic.ttc', 'YuGothM.ttc', 'NotoSansCJK-Regular.ttc',
                 'NotoSansJP-Regular.otf', 'ipaexg.ttf'):
        try:
            return ImageFont.truetype(name, px)
        except Exception:
            continue
    return None


def _n(s):
    """NFKC with spaces dropped: YomiToku writes ＆ as &, as any engine may."""
    return unicodedata.normalize('NFKC', s).replace(' ', '')


JP_LINES = ['フューエルポンプ＆フューエルフィルター', 'シートベルトスイッチ',
            'リヤドアコード', 'パーキングブレーキスイッチ', 'インストルメントパネル',
            'ハイトコントロールユニット', 'テンションレデューサ', 'サンルーフスイッチ']


def _jp_table_png(path, sideways: bool):
    """A page of Japanese table rows at 200 dpi -- printed level, or (sideways) the same
    page turned a quarter counter-clockwise, as a connector table is printed in a wiring
    manual. None if no Japanese font is installed."""
    from PIL import Image, ImageDraw
    font = _jp_font(44)
    if font is None:
        return None
    im = Image.new('L', (1700, 2200), 255)
    d = ImageDraw.Draw(im)
    for i, t in enumerate(JP_LINES):
        y = 200 + i * 110
        d.text((160, y), f'R{i + 1}', fill=0, font=font)
        d.text((400, y), t, fill=0, font=font)
        d.line((140, y - 20, 1500, y - 20), fill=0, width=3)
    if sideways:
        im = im.rotate(90, expand=True)
    im.save(path, dpi=(200, 200))
    return path


# ── routing ───────────────────────────────────────────────────────────────────

@needs_yomi
def test_japanese_goes_to_the_plugin(yomi_engine):
    args, note = owm._engine_args('jpn+eng')
    assert args == ['--plugin', str(owm.YOMITOKU_PLUGIN)]
    assert note == ' (engine yomitoku)'


@needs_yomi
def test_a_language_it_does_not_read_stays_on_tesseract(yomi_engine):
    """YomiToku is Japanese-only: Chinese or Cyrillic OCR'd with it would be noise."""
    for lang in ('rus+eng', 'chi_sim+eng'):
        args, note = owm._engine_args(lang)
        assert args == [], lang
        assert 'engine tesseract' in note and 'yomitoku does not read' in note, note


@needs_yomi
def test_the_startup_check_passes_where_the_plugin_loads():
    assert owm._plugin_error(owm.YOMITOKU_PLUGIN) == ''


def test_the_failed_ocr_advice_offers_tesseract_for_a_plugin_engine(monkeypatch):
    monkeypatch.setattr(owm, 'OCR_ENGINE', 'yomitoku')
    assert '--ocr-engine tesseract' in owm._ocr_failed_err('(OCR FAILED - boom)')


# ── the plugin itself ─────────────────────────────────────────────────────────

@needs_yomi
def test_a_vertical_line_is_given_a_text_angle():
    """OCRmyPDF suppresses a line whose box does not fit its text."""
    tall = yomi._line([(0, 0), (40, 0), (40, 400), (0, 400)], 'コントロールユニット', 0.9, False)
    up = yomi._line([(0, 0), (40, 0), (40, 400), (0, 400)], 'コントロールユニット', 0.9, True)
    wide = yomi._line([(0, 0), (400, 0), (400, 40), (0, 40)], 'コントロールユニット', 0.9)
    assert tall.textangle == 270.0 and up.textangle == 90.0
    assert wide.textangle is None


@needs_yomi
def test_ocr_page_reads_a_rendered_scan(tmp_path):
    src = U.make_scan_pdf(tmp_path / 's.pdf', npages=1)
    png = tmp_path / 'p.png'
    assert U.render_gray(src, 1, 300, png)
    page, text = yomi.ocr_page(png)
    assert 'page 1 line' in text, text[:200]
    assert page.children and all(ln.children for ln in page.children)


@needs_yomi
def test_a_page_printed_sideways_is_read_turned_upright(tmp_path):
    """Measured on a wiring manual's connector table printed sideways: YomiToku reads its
    lines as vertical text and garbles them (28 of 40 hand-checked entries), and reads all
    40 once the page is upright. The lines must come back in the coordinates of the page
    as it lies, running up it."""
    level = _jp_table_png(tmp_path / 'level.png', sideways=False)
    side = _jp_table_png(tmp_path / 'side.png', sideways=True)
    if level is None:
        pytest.skip('no Japanese font installed to draw the page')
    _, t_level = yomi.ocr_page(level)
    page, t_side = yomi.ocr_page(side)
    for t in JP_LINES:
        assert _n(t) in _n(t_level), (t, t_level)
        assert _n(t) in _n(t_side), (t, t_side)
    W, H = page.bbox.right, page.bbox.bottom
    assert (W, H) == (2200, 1700), 'the page keeps the size it lies at'
    rows = [ln for ln in page.children if _n(ln.children[0].text) in map(_n, JP_LINES)]
    assert len(rows) == len(JP_LINES)
    for ln in rows:
        b = ln.bbox
        assert 0 <= b.left < b.right <= W and 0 <= b.top < b.bottom <= H, b
        assert b.bottom - b.top > b.right - b.left, f'a row runs up the sideways page: {b}'
        assert ln.textangle == 90.0, 'turned counter-clockwise, so it reads bottom to top'


@needs_yomi
def test_level_pages_are_not_turned(tmp_path, monkeypatch):
    """Turning costs two more reads; a page that reads mostly level never pays them."""
    src = U.make_scan_pdf(tmp_path / 's.pdf', npages=1, dpi=150)
    png = tmp_path / 'p.png'
    assert U.render_gray(src, 1, 150, png)
    calls = []
    real = yomi._words
    monkeypatch.setattr(yomi, '_words', lambda e, img: calls.append(img.shape) or real(e, img))
    yomi.ocr_page(png)
    assert len(calls) == 1, calls


@needs_yomi
def test_concurrent_pages_are_serialised(tmp_path):
    """OCRmyPDF calls the engine from several THREADS at once; there is one model."""
    import threading
    src = U.make_scan_pdf(tmp_path / 's.pdf', npages=3, dpi=150)
    pngs = []
    for n in range(1, 4):
        png = tmp_path / f'p{n}.png'
        assert U.render_gray(src, n, 150, png)
        pngs.append(png)
    got, errs = {}, []

    def work(n, png):
        try:
            got[n] = yomi.ocr_page(png)[1]
        except Exception as ex:
            errs.append(repr(ex))
    ts = [threading.Thread(target=work, args=(n, p)) for n, p in enumerate(pngs, 1)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs, errs
    for n in range(1, 4):
        assert f'page {n} line' in got[n], (n, got[n][:120])


@needs_yomi
def test_a_gpu_fault_costs_the_page_a_retry_not_the_file(tmp_path, monkeypatch):
    """A CUDA error on a page drops the engine and reads the page again on a fresh one."""
    src = U.make_scan_pdf(tmp_path / 's.pdf', npages=1, dpi=150)
    png = tmp_path / 'p.png'
    assert U.render_gray(src, 1, 150, png)
    monkeypatch.setattr(yomi, '_device', 'cuda')
    monkeypatch.setattr(yomi, '_get_engine', lambda: object())
    monkeypatch.setattr(yomi, '_drop_engine', lambda: None)
    faults = [RuntimeError('CUDA error: an illegal memory access was encountered')]

    def read(engine, img):
        if faults:
            raise faults.pop()
        return [([(0, 0), (100, 0), (100, 20), (0, 20)], 'ok', 1.0, None)]
    monkeypatch.setattr(yomi, '_read', read)
    _, text = yomi.ocr_page(png)
    assert text == 'ok' and not faults


@needs_yomi
def test_an_error_that_is_not_the_gpus_still_fails_the_page(tmp_path, monkeypatch):
    src = U.make_scan_pdf(tmp_path / 's.pdf', npages=1, dpi=150)
    png = tmp_path / 'p.png'
    assert U.render_gray(src, 1, 150, png)
    monkeypatch.setattr(yomi, '_device', 'cuda')
    monkeypatch.setattr(yomi, '_get_engine', lambda: object())

    def read(engine, img):
        raise ValueError('not a GPU fault')
    monkeypatch.setattr(yomi, '_read', read)
    with pytest.raises(ValueError):
        yomi.ocr_page(png)


# ── end to end, through the tool ──────────────────────────────────────────────

@needs_yomi
@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_compress_path_ships_the_yomitoku_layer(tmp_path, yomi_engine):
    src = U.make_scan_pdf(tmp_path / 'src.pdf', npages=2)
    out = tmp_path / 'out.pdf'
    res = owm.compress_one(str(src), str(out), 200, ocr=True, language='eng')
    assert res.get('err') is None and res['action'] == 'compressed', res
    assert res['ocr_state'] == owm.OCR_NEW, res
    assert '(engine yomitoku)' in res['note'], res['note']
    text = PdfReader(str(out)).pages[1].extract_text() or ''
    assert 'page 2 line' in text, text[:200]


@needs_yomi
@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_keep_original_path_also_uses_it(tmp_path, yomi_engine, monkeypatch):
    """Under the size floor the ORIGINAL images ship and OCR runs in --redo-ocr mode --
    a different ocrmypdf pipeline, so it has to be proven separately."""
    monkeypatch.setattr(owm, 'MIN_COMPRESS_MB', 5.0)
    src = U.make_scan_pdf(tmp_path / 'small.pdf', npages=1)
    out = tmp_path / 'out.pdf'
    res = owm.compress_one(str(src), str(out), 200, ocr=True, language='eng')
    assert res.get('err') is None and res['reason'] == owm.REASON_SMALL, res
    assert res['ocr_state'] == owm.OCR_NEW, res
    assert '(engine yomitoku)' in res['note'], res['note']
    assert 'page 1 line' in (PdfReader(str(out)).pages[0].extract_text() or '')
