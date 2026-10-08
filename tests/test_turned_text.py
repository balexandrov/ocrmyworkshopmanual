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
    """Each turned line's text starts inside its label's column. Measured before: a label
    reading top to bottom was laid one thickness left of its ink (396-408 pt vs 410-417)."""
    import img2pdf
    import numpy as np
    from PIL import Image
    png = _turned_page(tmp_path / 'p.png')
    ink = np.array(Image.open(png)) < 128
    k = 300 / 72
    lower = ink[ink.shape[0] // 2:, :]
    cols = np.nonzero(lower.any(axis=0))[0]
    mid = ink.shape[1] // 2
    drawn = {'BRAKE': (cols[cols < mid].min() / k, cols[cols < mid].max() / k),
             'OIL': (cols[cols >= mid].min() / k, cols[cols >= mid].max() / k)}
    src = tmp_path / 'in.pdf'
    src.write_bytes(img2pdf.convert(str(png), dpi=300))
    out = tmp_path / 'out.pdf'
    r = subprocess.run([sys.executable, '-m', 'ocrmypdf', '--plugin', str(U.owm.PADDLE_PLUGIN),
                        '-l', 'eng', '--output-type', 'pdf', '-q', str(src), str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]
    starts = {}

    def visit(text, cm, tm, font, size):
        for key in drawn:
            if key in text and key not in starts:
                starts[key] = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
    PdfReader(str(out)).pages[0].extract_text(visitor_text=visit)
    for key, (x0, x1) in drawn.items():
        assert key in starts, f'{key} label not in the layer'
        assert x0 - 3 <= starts[key] <= x1 + 3, f'{key}: text at x {starts[key]:.0f}, label {x0:.0f}-{x1:.0f}'


@needs_paddle
def test_a_turned_word_is_stretched_along_the_line_not_across_it():
    """OCRmyPDF stretches a word to its box's page width even on a turned line; measured,
    a 35 pt label's text covered 9 pt. The word's page width is therefore the line's length."""
    tall = paddle._line([(0, 0), (40, 0), (40, 400), (0, 400)], 'BrY(セダン)', 0.9, up=True)
    assert tall.textangle == 90.0
    word = tall.children[0].bbox
    assert word.right - word.left == 400
    assert (word.top, word.bottom) == (0, 400), 'its place along the line is unchanged'
    level = paddle._line([(0, 0), (400, 0), (400, 40), (0, 40)], 'BrY(セダン)', 0.9)
    assert level.children[0].bbox == level.bbox


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
