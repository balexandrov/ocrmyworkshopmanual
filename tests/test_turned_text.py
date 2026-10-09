"""Text turned a quarter on the page -- labels running up or down beside the wires of a
wiring diagram -- read by the paddle plugin, the right way up, and laid into the text layer
along the label rather than squeezed into its thickness.

Measured on a Japanese wiring diagram before this: of four "(セダン)" labels the layer held
two, one read upside down as "(5.64)", both as 5x7 pt boxes beside the label; the "BrY" wire
colour each one starts with was missing.
"""
import subprocess
import sys

import pytest
from pypdf import PdfReader

import _util as U

try:
    import ocrmypdf_paddle as paddle
    _no_paddle = None
except Exception as ex:                      # rapidocr / onnxruntime not installed
    paddle, _no_paddle = None, f'paddle engine unavailable: {ex}'

needs_paddle = pytest.mark.skipif(_no_paddle is not None, reason=str(_no_paddle))

UP, DOWN = 'BRAKE LAMP SWITCH', 'OIL PRESSURE GAUGE'


def _turned_page(png, dpi=300):
    """A page of level text with one label reading bottom to top and one top to bottom."""
    from PIL import Image, ImageDraw, ImageFont
    w, h = round(8.5 * dpi), round(11 * dpi)
    try:
        font = ImageFont.truetype('arial.ttf', round(dpi * 0.13))
    except Exception:
        font = ImageFont.load_default()
    im = Image.new('L', (w, h), 255)
    d = ImageDraw.Draw(im)
    for ln in range(12):
        d.text((dpi // 2, dpi // 2 + ln * round(dpi * 0.21)),
               f'Level line {ln}: wiring diagram notes.', fill=0, font=font)
    for text, angle, x in ((UP, 90, w // 3), (DOWN, -90, 2 * w // 3)):
        box = font.getbbox(text)
        lab = Image.new('L', (box[2] + 20, box[3] + 20), 255)
        ImageDraw.Draw(lab).text((10, 10), text, fill=0, font=font)
        im.paste(lab.rotate(angle, expand=True, fillcolor=255), (x, h // 2))
    im.save(png, dpi=(dpi, dpi))
    return png


@needs_paddle
def test_turned_labels_are_read_and_say_which_way(tmp_path):
    page, text = paddle.ocr_page(_turned_page(tmp_path / 'p.png'))
    lines = {ln.children[0].text: ln.textangle for ln in page.children}
    up = [t for t in lines if 'BRAKE' in t]
    down = [t for t in lines if 'OIL' in t]
    assert up and down, f'both labels read: {sorted(lines)}'
    assert lines[up[0]] == 90.0, 'reads bottom to top'
    assert lines[down[0]] == 270.0, 'reads top to bottom'
    assert UP.replace(' ', '') in up[0].replace(' ', '')
    assert DOWN.replace(' ', '') in down[0].replace(' ', '')


@needs_paddle
def test_turned_labels_reach_the_text_layer(tmp_path):
    """OCRmyPDF drops a line whose box does not fit its text's shape: the label must ship."""
    import img2pdf
    png = _turned_page(tmp_path / 'p.png')
    src = tmp_path / 'in.pdf'
    src.write_bytes(img2pdf.convert(str(png), dpi=300))
    out = tmp_path / 'out.pdf'
    r = subprocess.run([sys.executable, '-m', 'ocrmypdf', '--plugin', str(U.owm.PADDLE_PLUGIN),
                        '-l', 'eng', '--output-type', 'pdf', '-q', str(src), str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]
    text = PdfReader(str(out)).pages[0].extract_text().replace(' ', '')
    assert 'BRAKELAMP' in text and 'OILPRESSURE' in text, text[:500]


@needs_paddle
def test_turned_labels_sit_on_their_label_in_the_layer(tmp_path):
    """Each turned line's glyphs lie across its label's column and run its length, as the
    rendered layer has them -- not as the plugin's boxes say, since what the renderer makes
    of a box has changed between OCRmyPDF releases. Measured: OCRmyPDF 16.x squeezed a 35 pt
    label's text into 9 pt of it; before 17.13 a label reading top to bottom was laid one
    thickness left of its ink (396-408 pt vs 410-417); and on 17.13.0 the plugin's old
    correction for that put it one thickness right (419-428)."""
    import img2pdf
    import numpy as np
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTChar
    from PIL import Image
    png = _turned_page(tmp_path / 'p.png')
    ink = np.array(Image.open(png)) < 128
    k = 300 / 72
    h, mid = ink.shape[0], ink.shape[1] // 2
    ink[:h // 2] = False
    drawn = {}
    for key, sel, off in (('BRAKE', ink[:, :mid], 0), ('OIL', ink[:, mid:], mid)):
        rows, cols = np.nonzero(sel.any(axis=1))[0], np.nonzero(sel.any(axis=0))[0] + off
        drawn[key] = ((cols.min() / k, cols.max() / k), ((h - rows.max()) / k, (h - rows.min()) / k))
    src = tmp_path / 'in.pdf'
    src.write_bytes(img2pdf.convert(str(png), dpi=300))
    out = tmp_path / 'out.pdf'
    r = subprocess.run([sys.executable, '-m', 'ocrmypdf', '--plugin', str(U.owm.PADDLE_PLUGIN),
                        '-l', 'eng', '--output-type', 'pdf', '-q', str(src), str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]
    chars = []

    def walk(o):
        if isinstance(o, LTChar):
            chars.append(o)
        elif hasattr(o, '__iter__'):
            for c in o:
                walk(c)
    for p in extract_pages(str(out)):
        walk(p)
    half = mid / k
    for key, ((x0, x1), (y0, y1)) in drawn.items():
        cs = [c for c in chars if not c.upright and c.get_text().strip()
              and (c.x0 < half) == (key == 'BRAKE')]
        assert cs, f'{key} label not in the layer'
        gx0, gx1 = min(c.x0 for c in cs), max(c.x1 for c in cs)
        gy0, gy1 = min(c.y0 for c in cs), max(c.y1 for c in cs)
        across = min(gx1, x1) - max(gx0, x0)
        assert across >= 0.5 * (x1 - x0), f'{key}: text at x {gx0:.0f}-{gx1:.0f}, label {x0:.0f}-{x1:.0f}'
        along = min(gy1, y1) - max(gy0, y0)
        assert along >= 0.8 * (y1 - y0), f'{key}: text at y {gy0:.0f}-{gy1:.0f}, label {y0:.0f}-{y1:.0f}'


@needs_paddle
def test_a_turned_line_carries_its_true_box():
    """The plugin hands OCRmyPDF the label's own box and lets the renderer turn it (see
    `_line`); a box bent to cancel one release's renderer misplaces it on the next."""
    tall = paddle._line([(0, 0), (40, 0), (40, 400), (0, 400)], 'BrY(セダン)', 0.9, up=False)
    assert tall.textangle == 270.0
    assert tall.children[0].bbox == tall.bbox
    assert (tall.bbox.left, tall.bbox.top, tall.bbox.right, tall.bbox.bottom) == (0, 0, 40, 400)
    level = paddle._line([(0, 0), (400, 0), (400, 40), (0, 40)], 'BrY(セダン)', 0.9)
    assert level.textangle is None and level.children[0].bbox == level.bbox


def _q(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


@needs_paddle
def test_the_merge_takes_labels_and_refuses_stacked_cells():
    """Turned sideways, a column of connector pin cells reads as one line ("72", "N3");
    measured, those were 0.24-0.56 of the page's line height thick, real labels 0.63-0.89,
    and the one thick enough lay over pin numbers read upright at 1.0."""
    upright = ([_q(0, 0, 300, 30), _q(0, 100, 300, 130),          # level text, 30 high
                _q(500, 500, 520, 520),                              # scrap "B" of a label
                _q(700, 500, 720, 520)],                             # pin "2", read sure
               ['Level text one', 'Level text two', 'B', '2'],
               [0.99, 0.99, 0.99, 1.0],
               [None, None, None, None])
    turned = [(_q(495, 450, 520, 560), 'BrY(セダン)', 0.92, True),   # label, 25 thick
              (_q(600, 450, 610, 560), '72', 0.78, True),             # cells, 10 thick
              (_q(695, 450, 720, 560), 'N3', 0.71, True)]             # over the sure "2"
    boxes, txts, scores, up = paddle._merge(upright, turned)
    assert 'BrY(セダン)' in txts and 'B' not in txts, 'the label replaces its scrap'
    assert '72' not in txts, 'too thin for this page'
    assert 'N3' not in txts and '2' in txts, 'a sure upright reading stands'


class _FakeEngine:
    """Classifier and recogniser keyed on where a crop's marker pixel sits: (0, 0) as cropped,
    bottom-right once turned over. `cls_flip` says which view the classifier prefers."""

    def __init__(self, as_cropped, turned_over, cls_flip):
        self.read = {False: as_cropped, True: turned_over}
        self.cls_flip = cls_flip

    @staticmethod
    def _over(img):
        return bool(img[-1, -1].any())

    def text_cls(self, crops):
        from types import SimpleNamespace
        return SimpleNamespace(cls_res=[('180' if self._over(c) == self.cls_flip else '0', 0.9)
                                        for c in crops])

    def text_rec(self, inp):
        from types import SimpleNamespace
        got = [self.read[self._over(c)] for c in inp.img]
        return SimpleNamespace(txts=[t for t, _ in got], scores=[s for _, s in got])


def _marked_crop():
    import numpy as np
    c = np.zeros((32, 320, 3), np.uint8)
    c[0, 0] = 255
    return c


@needs_paddle
def test_a_vertical_line_the_comparison_turns_wrong_is_not_lost():
    """Measured on a diagnosis table's tategaki header: picked upside down, "ライン圧ソレノイド"
    read "メハく" at 0.26 and was cut, where it reads 0.84 the way it was cropped."""
    eng = _FakeEngine(('ライン圧ソレノイド', 0.84), ('メハく', 0.26), cls_flip=False)
    # cls_flip=False: the classifier calls the as-cropped view upside down, so it is turned over
    txts, scores, turned = paddle._read_right_way_up(eng, [_marked_crop()])
    assert txts == ['ライン圧ソレノイド'] and scores == [0.84]
    assert turned == [False], 'read as cropped: top to bottom'


@needs_paddle
def test_a_vertical_line_the_comparison_turns_right_keeps_its_reading():
    """The "(セダン)" label: its wrong side scores 0.818, over the cut, so the comparison's
    pick stands even though the two scores alone could not tell them apart."""
    eng = _FakeEngine(('(5.64)', 0.818), ('(セタン)', 0.823), cls_flip=False)
    txts, scores, turned = paddle._read_right_way_up(eng, [_marked_crop()])
    assert txts == ['(セタン)'] and turned == [True]


@needs_paddle
def test_ruled_and_dotted_lines_are_not_text():
    """Measured on 200 dpi diagnosis tables: table rules and leaders read as "wwwwww" / "………"
    at 0.21-0.48 of the page's median line thickness, real text from 0.61."""
    boxes = [_q(0, 0, 300, 30), _q(0, 50, 300, 80), _q(0, 100, 200, 130),      # text, 30 thick
             _q(400, 0, 430, 300),                                              # vertical, 30 wide
             _q(0, 200, 140, 207),                                              # rule, 7 thick
             _q(0, 300, 200, 310),                                              # leader, 10 thick
             _q(0, 400, 11, 409),                                               # a thin "→"
             _q(0, 500, 100, 519)]                                              # small text, 0.63
    txts = ['Level one', 'Level two', 'Level three', 'ソレノイド', 'wwwwww', '………', '→', 'BT0614M']
    up = [None, None, None, False, None, None, None, None]
    kept = [txts[i] for i in paddle._not_rules(boxes, txts, up)]
    assert 'wwwwww' not in kept and '………' not in kept
    assert kept == ['Level one', 'Level two', 'Level three', 'ソレノイド', '→', 'BT0614M']
