"""OCRmyPDF plugin: YomiToku as the OCR engine, instead of Tesseract.

    ocrmypdf --plugin ocrmypdf_yomitoku.py -l jpn+eng in.pdf out.pdf
    python ocrmyworkshopmanual.py SRC --ocr-engine yomitoku

Why it exists. Measured on 16 pages of 1998-2002 Japanese Subaru service, diagnosis, body
repair and wiring manuals (200-400 dpi scans), against 354 strings read off the page images
by eye -- found = the string appears verbatim in the page's text:

                                   printed lines  table cells  diagram labels   all   s/page
    this plugin (YomiToku 0.15)        104/109       133/136       106/109     343/354   1.3
    YomiToku, pages as they lie        104/109       121/136       106/109     331/354   1.3
    PaddleOCR PP-OCRv6 (ocrmypdf_paddle) 83/109      125/136        99/109     307/354   3.6
    NDLOCR-Lite 1.2                      91/109       89/136        29/109     209/354   1.4

PaddleOCR reads Chinese and Japanese with one model and puts simplified-Chinese forms into
Japanese words (交换 for 交換, 電压 for 電圧, 基準值 for 基準値); YomiToku is trained on
Japanese only. Its one measured weakness was a table printed sideways, read 28/40 as it
lies; this plugin turns such a page (see `_ORIENT_VERTICAL_SHARE`) and reads it 40/40, at
6.2 s for the page's three reads.

YomiToku runs on PyTorch: on an NVIDIA GPU when the installed torch is a CUDA build, else on
the CPU. Its models are fetched from Hugging Face on first use. Licence: YomiToku is
CC BY-NC-SA 4.0 -- free for non-commercial use only; it is an optional dependency that this
plugin calls, not part of this repository.

What it returns is positioned LINES, one word per line; OCRmyPDF lays its own invisible
text layer over them, so every other stage of a run is untouched.
"""
from __future__ import annotations

import threading
from importlib.metadata import version as _dist_version
from pathlib import Path

# Imported here, not lazily: loading the plugin is how a caller proves the environment
# OCRmyPDF actually runs in has them (`ocrmypdf --plugin ocrmypdf_yomitoku.py --version`),
# instead of discovering a missing package on the first page of a long run.
import numpy as np
import torch
from PIL import Image
from yomitoku import OCR

from ocrmypdf import hookimpl
from ocrmypdf.hocrtransform import BoundingBox, OcrClass, OcrElement
from ocrmypdf.pluginspec import OcrEngine, OrientationConfidence

# The release the measurements above were made with. Every YomiToku from 0.5.2 on requires
# Python < 3.14, so on 3.14 pip quietly installs 0.4.1 -- two years older, with different
# models -- and the plugin would still load. Refuse it here, where the tool's start-up check
# (loading the plugin through ocrmypdf) turns it into a clear error before the run.
_MIN_VERSION = (0, 15)
if tuple(int(p) for p in _dist_version('yomitoku').split('.')[:2]) < _MIN_VERSION:
    raise ImportError(f'yomitoku {_dist_version("yomitoku")} is older than the measured '
                      f'{".".join(map(str, _MIN_VERSION))}; YomiToku >= 0.5.2 needs Python < 3.14')

# What YomiToku reads, as Tesseract language codes (what `-l` carries): Japanese, and the
# Latin letters and digits Japanese manuals are full of. Anything else is refused, so the
# caller can route the file to Tesseract instead of OCR'ing it in the wrong script.
LANGUAGES = frozenset({'eng', 'jpn'})

# A detected line this much taller than wide is VERTICAL; OCRmyPDF's renderer suppresses a
# line whose box does not fit its text's shape, so it is given a text angle (`_line`). Same
# cut as ocrmypdf_paddle.
_VERTICAL_RATIO = 1.5

# A page whose text is at least this share vertical (by characters) is also read turned a
# quarter each way, and the turned reading kept when it is `_TURNED_BETTER` times as
# confident. YomiToku reads the lines of a page printed sideways as vertical text and
# garbles them. Measured on the 16-page bench above: upright pages 0-3% vertical; a
# connector table printed sideways 69%, read 28/40 as it lies, 40/40 upright.
_ORIENT_VERTICAL_SHARE = 0.5
# Confidence here is sum(characters x recognition score). Measured: the sideways table
# 440 as it lies, 713 turned clockwise; upright pages read turned scored up to 1.0x their
# upright reading (476 and 476 on a two-column procedure page) -- so the total alone cannot
# pick the side, and a turn must clearly win.
_TURNED_BETTER = 1.25

# No recognition-score cut: YomiToku's score is not a usable confidence. Measured on the
# bench pages, correctly read lines scored as low as 0.23 ("(3)ECU端子ーボデイ間の電圧測定")
# and 0.32 ("3.ECU~ボディ間の"); a 0.5 cut, as ocrmypdf_paddle uses, would drop them.

_engine = None
_device = 'cpu'
_lock = threading.Lock()
# OCRmyPDF runs pages on THREADS by default (use_threads), so the one engine would be called
# from several at once. There is one GPU and one model in memory; recognition queues on it,
# while page rendering and PDF assembly around it still overlap.
_run_lock = threading.Lock()


def _get_engine():
    """One YomiToku OCR per process: loading the models costs seconds, so it is built on the
    first page and reused."""
    global _engine, _device
    with _lock:
        if _engine is None:
            _device = 'cuda' if torch.cuda.is_available() else 'cpu'
            _engine = OCR(device=_device, visualize=False)
        return _engine


def _drop_engine():
    """Forget the engine (a GPU fault leaves it unusable); the next page builds a fresh one."""
    global _engine
    with _lock:
        _engine = None
    import gc
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _is_gpu_fault(ex) -> bool:
    """A CUDA error or out-of-memory raised inside torch."""
    return isinstance(ex, torch.OutOfMemoryError) or 'CUDA' in f'{type(ex).__name__} {ex}'


def _words(engine, img) -> list:
    """(quad, text, score, direction) for each word YomiToku finds on `img` (an RGB/BGR
    uint8 array)."""
    r = engine(img)
    r = r[0] if isinstance(r, tuple) else r
    return [([(float(x), float(y)) for x, y in w.points], w.content, float(w.rec_score),
             w.direction) for w in r.words if w.content and w.content.strip()]


def _mass(words) -> float:
    return sum(len(t.strip()) * s for _, t, s, _ in words)


def _vertical_share(words) -> float:
    n = sum(len(t.strip()) for _, t, _, _ in words)
    return sum(len(t.strip()) for _, t, _, d in words if d == 'vertical') / n if n else 0.0


def _read(engine, img):
    """(quads, texts, scores, up) on the page as it lies, or -- for a page that reads mostly
    vertical -- turned a quarter, whichever `_TURNED_BETTER` picks. `up` is, per line, True
    for a line reading bottom to top on the page, False for top to bottom, None for level."""
    H, W = img.shape[:2]
    words = _words(engine, img)
    out = [(q, t, s, False if d == 'vertical' else None) for q, t, s, d in words]
    if _vertical_share(words) < _ORIENT_VERTICAL_SHARE:
        return out
    best = _mass(words)
    for k, to_page, up in (
            # clockwise: turned (x, y) came from page (y, H-1-x); level there = bottom to top
            (-1, lambda x, y: (y, H - 1 - x), True),
            # counter-clockwise: turned (x, y) came from page (W-1-y, x); level = top to bottom
            (1, lambda x, y: (W - 1 - y, x), False)):
        turned = _words(engine, np.ascontiguousarray(np.rot90(img, k)))
        if _mass(turned) >= _TURNED_BETTER * best:
            best = _mass(turned)
            out = [([to_page(x, y) for x, y in q], t, s, up if d != 'vertical' else None)
                   for q, t, s, d in turned]
    return out


def _recognise(img):
    """`_read` on the shared engine; on a GPU fault the page is retried once on a fresh
    engine, then read on the CPU -- one page costs seconds, not the book."""
    engine = _get_engine()
    if _device != 'cuda':
        return _read(engine, img)
    for _ in range(2):
        try:
            return _read(engine, img)
        except Exception as ex:
            if not _is_gpu_fault(ex):
                raise
        del engine
        _drop_engine()
        engine = _get_engine()
    return _read(OCR(device='cpu', visualize=False), img)


def _line(quad, text: str, score: float, up: bool | None = None) -> OcrElement:
    """One LINE holding one WORD, with a text angle for a vertical line (see the same
    function in ocrmypdf_paddle for the hOCR convention and the ocrmypdf>=17.13 floor)."""
    xs = [float(p[0]) for p in quad]
    ys = [float(p[1]) for p in quad]
    box = BoundingBox(left=min(xs), top=min(ys), right=max(xs), bottom=max(ys))
    w, h = box.right - box.left, box.bottom - box.top
    angle = None
    if len(text) > 1 and h > _VERTICAL_RATIO * w:
        angle = 90.0 if up else 270.0
    word = OcrElement(ocr_class=OcrClass.WORD, bbox=box, text=text, confidence=float(score))
    return OcrElement(ocr_class=OcrClass.LINE, bbox=box, textangle=angle, children=[word])


def _load(input_file: Path):
    """(BGR uint8 array, width, height, dpi) -- read through PIL, which takes every format
    OCRmyPDF hands an engine (1-bit and grey PNG/TIFF included) and any path."""
    with Image.open(input_file) as img:
        dpi = img.info.get('dpi', (300, 300))
        dpi = float(dpi[0] if isinstance(dpi, tuple) else dpi)
        rgb = np.asarray(img.convert('RGB'))
    return np.ascontiguousarray(rgb[:, :, ::-1]), rgb.shape[1], rgb.shape[0], dpi


def ocr_page(input_file: Path, page_number: int = 0) -> tuple[OcrElement, str]:
    """OCR one page image into an OcrElement page and its plain text, lines in reading
    order (top to bottom, then left to right). Split out of the plugin class so it can be
    called and tested without an OCRmyPDF run."""
    img, width, height, dpi = _load(input_file)
    page = OcrElement(ocr_class=OcrClass.PAGE,
                      bbox=BoundingBox(left=0, top=0, right=width, bottom=height),
                      dpi=dpi, page_number=page_number)
    with _run_lock:
        lines = _recognise(img)
    lines = sorted(lines, key=lambda t: (round(min(p[1] for p in t[0]) / 20), min(p[0] for p in t[0])))
    page.children = [_line(q, t, s, u) for q, t, s, u in lines]
    return page, '\n'.join(t for _, t, _, _ in lines)


class YomiTokuOcrEngine(OcrEngine):
    """YomiToku's OCR stage (text detection + recognition) on PyTorch."""

    @staticmethod
    def version() -> str:
        return f'yomitoku-{_dist_version("yomitoku")}'

    @staticmethod
    def creator_tag(options) -> str:
        return f'YomiToku {YomiTokuOcrEngine.version()}'

    def __str__(self) -> str:
        return f'YomiToku {self.version()}'

    @staticmethod
    def languages(options) -> set[str]:
        return set(LANGUAGES)

    @staticmethod
    def get_orientation(input_file: Path, options) -> OrientationConfidence:
        # No page-orientation guess: only used by --rotate-pages, which this tool never
        # passes. A page printed sideways is turned inside `_read`.
        return OrientationConfidence(angle=0, confidence=0.0)

    @staticmethod
    def supports_generate_ocr() -> bool:
        return True

    @staticmethod
    def generate_ocr(input_file: Path, options, page_number: int = 0):
        return ocr_page(input_file, page_number)

    @staticmethod
    def generate_hocr(input_file: Path, output_hocr: Path, output_text: Path, options):
        raise NotImplementedError('ocrmypdf_yomitoku implements generate_ocr only')

    @staticmethod
    def generate_pdf(input_file: Path, output_pdf: Path, output_text: Path, options):
        raise NotImplementedError('ocrmypdf_yomitoku implements generate_ocr only '
                                  '(use the default --pdf-renderer)')


@hookimpl
def get_ocr_engine(options):
    """Loading this plugin is the opt-in: it then answers for every file of the run."""
    return YomiTokuOcrEngine()
