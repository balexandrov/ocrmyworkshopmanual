"""OCRmyPDF plugin: PaddleOCR PP-OCRv6 (medium) as the OCR engine, instead of Tesseract.

    ocrmypdf --plugin ocrmypdf_paddle.py -l jpn+eng in.pdf out.pdf
    python ocrmyworkshopmanual.py SRC --ocr-engine paddle

Why it exists. Measured on two Japanese Subaru wiring manuals, against labels and printed
lines read off the page images by eye:

    wiring-diagram labels (28, two pages)      found   wrong JP chars
        PP-OCRv6 medium                          25          3
        ABBYY FineReader (the file's own layer)  19         45
        Tesseract jpn+eng, as this tool ran it   14         13
    printed lines (63, two explanation pages)
        PP-OCRv6 medium                          57
        Tesseract jpn+eng                        46
        ABBYY FineReader Engine 11               42

Its one measured weakness: on dense, leader-dotted contents pages it can drop whole lines
(one page kept 2 of ~9 near-identical entries), where Tesseract loses none but garbles more.

It runs the PaddleOCR models through RapidOCR on onnxruntime rather than PaddlePaddle,
which has no wheel for current Python, and gets the same models. A GPU is used when the
installed onnxruntime offers one: `onnxruntime-directml` (any DirectX 12 card on Windows)
or `onnxruntime-gpu` (CUDA). Measured on a GTX 1060: 11.7 s a page against 75 s on the
CPU, with byte-identical text. The models are fetched by RapidOCR on first use.

What it returns is positioned LINES, one word per line; OCRmyPDF lays its own invisible
text layer over them, so every other stage of a run is untouched.
"""
from __future__ import annotations

import threading
from importlib.metadata import version as _dist_version
from pathlib import Path

# Imported here, not lazily: loading the plugin is how a caller proves the environment
# OCRmyPDF actually runs in has them (`ocrmypdf --plugin ocrmypdf_paddle.py --version`),
# instead of discovering a missing package on the first page of a long run.
import numpy as np
import onnxruntime
from PIL import Image
from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

from ocrmypdf import hookimpl
from ocrmypdf.hocrtransform import BoundingBox, OcrClass, OcrElement
from ocrmypdf.pluginspec import OcrEngine, OrientationConfidence

# What the medium model reads, as Tesseract language codes (what `-l` carries). The model
# also covers ~46 Latin-script languages, but only these four are named by the model card
# in a form that maps onto a code without guessing; anything else is refused, so the
# caller can route the file to Tesseract instead of OCR'ing it in the wrong script.
LANGUAGES = frozenset({'eng', 'jpn', 'chi_sim', 'chi_tra'})

# A detected line this much taller than wide is VERTICAL text (Japanese tategaki, or a
# label running up a diagram). OCRmyPDF's renderer suppresses a line whose box does not fit
# its text's shape, so an unrotated vertical line would silently vanish from the layer.
# 1.5 is also where RapidOCR turns a crop on its side before reading it.
_VERTICAL_RATIO = 1.5

# RapidOCR's own cut for a line's recognition score -- applied here, AFTER vertical lines are
# re-read (see `_orient_vertical`), not inside the engine: a label read upside down scores low
# and the engine would drop it, box and all, before it could be read the right way up.
_TEXT_SCORE = 0.5

_engine = None
_gpu = False
_lock = threading.Lock()
# OCRmyPDF runs pages on THREADS by default (use_threads), so one engine is called from
# several at once. On the CPU provider that is safe — measured: 6 threads, text identical to
# a single-thread run. On DirectML it is not: 6 threads crashed the process with an access
# violation (0xC0000005) within seconds, which OCRmyPDF reported only as its exit code, and
# on a real 138-page run the file shipped with no text layer. There is one GPU, so the
# recognition calls queue on it; page rendering and PDF assembly around them still overlap.
_gpu_lock = threading.Lock()

# The engine is rebuilt every this many pages, because one engine's memory only ever grows.
# Measured on DirectML (GTX 1060): ~5.8 GB private after the first 300 dpi page, then +120 MB
# for each further page of a new size, never returned -- with onnxruntime's CPU arena off (as
# RapidOCR ships it) and with its memory-pattern optimisation off alike. Over a real book
# OCRmyPDF's process grew 85 MB a page on 300 dpi scans and ~490 MB a page on high-resolution
# ones: 36.9 GB on a 432-page manual, 1.4 GB of 47.9 left free. Dropping the engine returns it
# all (6.9 GB -> 0.8 GB) and a rebuild takes ~2 s, about 1.5% of ten pages' recognition.
_RECYCLE_EVERY = 10
_pages_done = 0


def _providers() -> tuple[bool, bool]:
    """(use_dml, use_cuda): whichever GPU provider this onnxruntime build offers."""
    have = set(onnxruntime.get_available_providers())
    return 'DmlExecutionProvider' in have, 'CUDAExecutionProvider' in have


def _engine_params(dml: bool, cuda: bool) -> dict:
    params = {
        'Global.log_level': 'warning',
        # 4000 px keeps an A4 scan at ~340 dpi; the default 2000 halves a 600 dpi page
        'Global.max_side_len': 4000,
        'Global.text_score': 0.0,   # cut in `_recognise`, after `_orient_vertical`
        'Det.engine_type': EngineType.ONNXRUNTIME,
        'Rec.engine_type': EngineType.ONNXRUNTIME,
        'Det.ocr_version': OCRVersion.PPOCRV6, 'Det.model_type': ModelType.MEDIUM,
        'Rec.ocr_version': OCRVersion.PPOCRV6, 'Rec.model_type': ModelType.MEDIUM,
        'EngineConfig.onnxruntime.use_dml': dml,
        'EngineConfig.onnxruntime.use_cuda': cuda,
    }
    if not (dml or cuda):
        # OCRmyPDF parallelises over pages; an engine must not add threads of its own
        params['EngineConfig.onnxruntime.intra_op_num_threads'] = 1
        params['EngineConfig.onnxruntime.inter_op_num_threads'] = 1
    return params


def _get_engine():
    """One RapidOCR per process: OCRmyPDF runs pages in worker processes and loading the
    models costs seconds, so it is built on the first page and reused."""
    global _engine, _gpu
    with _lock:
        if _engine is None:
            dml, cuda = _providers()
            _gpu = dml or cuda
            _engine = RapidOCR(params=_engine_params(dml, cuda))
        return _engine


def _drop_engine():
    """Forget the engine (a GPU fault leaves it unusable); the next page builds a fresh one."""
    global _engine
    with _lock:
        _engine = None
    import gc
    gc.collect()


def _is_gpu_fault(ex) -> bool:
    """An error raised inside onnxruntime -- RapidOCR wraps them as ONNXRuntimeError."""
    return 'ONNXRuntimeError' in f'{type(ex).__name__} {ex}'


def _recognise_on_gpu(src):
    """`_recognise` on the GPU, surviving a fault there: the page is retried once on a fresh
    engine, and if that faults too it is read on the CPU, alone. Measured on a wiring manual
    of 600-800 dpi A3 scans: DirectML failed the WHOLE file on one page -- 'Not enough memory
    resources' (8007000E), and after that was fixed 'The GPU will not respond to more
    commands' (887A0006, the driver logging resets) -- three runs out of three, while the
    same pages read one at a time went through. One page now costs seconds, not the book."""
    try:
        return _recognise(src)
    except Exception as ex:
        if not _is_gpu_fault(ex):
            raise
        _drop_engine()
    try:
        return _recognise(src)
    except Exception as ex:
        if not _is_gpu_fault(ex):
            raise
        _drop_engine()
    return _recognise(src, RapidOCR(params=_engine_params(False, False)))


def _count_page():
    """Drop the engine after every `_RECYCLE_EVERY` pages, so the next page builds a fresh
    one. On a GPU this runs under `_gpu_lock`, so no other page is inside the engine and it
    is torn down here; on the CPU a thread still inside the old engine keeps its own
    reference, and the old engine's memory goes when the last such call returns."""
    global _engine, _pages_done
    with _lock:
        _pages_done += 1
        if _pages_done % _RECYCLE_EVERY:
            return
        _engine = None
    import gc
    gc.collect()


def _recognise(src, engine=None):
    """Run the engine (the shared one unless given) on one page, then count the page (which
    may drop the shared engine).

    Returns (boxes, txts, scores, up) with lines under `_TEXT_SCORE` dropped; `up` holds, per
    line, True for a vertical line that reads bottom to top, False for top to bottom, None
    for a horizontal one."""
    engine = engine or _get_engine()
    r = engine(src)
    out = None
    if r.boxes is not None:
        img = engine.load_img(src)
        out = _merge(_orient_vertical(engine, img, r), _turned_lines(engine, img))
    del engine
    _count_page()
    if out is None:
        return None
    keep = [i for i, s in enumerate(out[2]) if s >= _TEXT_SCORE]
    return tuple([part[i] for i in keep] for part in out)


def _is_vertical(quad) -> bool:
    """RapidOCR's own test for turning a crop on its side (`get_rotate_crop_image`)."""
    w, h = _sides(quad)
    return w > 0 and h / w >= _VERTICAL_RATIO


def _sides(quad) -> tuple[float, float]:
    p = [(float(x), float(y)) for x, y in quad]
    return max(_dist(p[0], p[1]), _dist(p[2], p[3])), max(_dist(p[0], p[3]), _dist(p[1], p[2]))


def _dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _right_way_up(engine, crops):
    """Each crop turned whichever way up the direction classifier finds MORE upright, and
    whether that took a half turn.

    The classifier's own rule -- flip only when over 90% sure it is upside down -- misreads
    small rotated labels. Measured on a Japanese wiring diagram, a "(セダン)" label as RapidOCR
    crops it: "upright" at 0.554 as cropped, 0.701 turned over; RapidOCR kept it as cropped
    and read "(5.64)" at 0.818 -- a higher score than the right reading "(セタン)" at 0.823 had
    margin over it, so the recogniser's score cannot decide either. Comparing the two views
    picks the right side on every label measured, including ones the classifier was 0.9999
    sure of."""
    if not crops:
        return [], []
    flips = [np.ascontiguousarray(np.rot90(c, 2)) for c in crops]
    res = engine.text_cls([c.copy() for c in crops] + [f.copy() for f in flips]).cls_res
    n = len(crops)

    def p_up(lab_score):
        lab, sc = lab_score
        return float(sc) if '0' == lab else 1.0 - float(sc)

    turned = [p_up(res[n + i]) > p_up(res[i]) for i in range(n)]
    return [f if t else c for c, f, t in zip(crops, flips, turned)], turned


def _orient_vertical(engine, img, r):
    """Re-read every vertical line the right way up, and say which way it runs.

    RapidOCR turns a vertical crop 90 degrees counter-clockwise, which leaves a label that
    reads bottom to top upside down, and only turns it back when the direction classifier is
    over 90% sure (see `_right_way_up`). A crop it reads upside down turned over is a label
    reading bottom to top."""
    from rapidocr.ch_ppocr_rec.typings import TextRecInput
    from rapidocr.utils.process_img import get_rotate_crop_image
    boxes, txts, scores = list(r.boxes), list(r.txts), list(r.scores)
    up = [None] * len(boxes)
    idx = [i for i, q in enumerate(boxes) if _is_vertical(q)]
    if not idx:
        return boxes, txts, scores, up
    crops = [get_rotate_crop_image(img, np.array(boxes[i], dtype=np.float32)) for i in idx]
    crops, turned = _right_way_up(engine, crops)
    rec = engine.text_rec(TextRecInput(img=crops))
    for i, t, txt, sc in zip(idx, turned, rec.txts, rec.scores):
        txts[i], scores[i], up[i] = txt, float(sc), t
    return boxes, txts, scores, up


def _turned_lines(engine, img):
    """Lines that run up or down the page, found on the page turned a quarter clockwise.

    The detector is trained on level text and finds a label running up beside a wire badly:
    measured on a Japanese wiring diagram, of four "(セダン)" labels it found two, as boxes cut
    short of the "BrY" each label starts with, and the other two only as one-glyph scraps.
    On the page turned clockwise the same labels are level text and it found all four whole
    ("BrY(セダン)", "(セダン)BrY", "B(セダン)", "(セダン)B" with B read as 日). Detection only;
    just the level lines it finds are cropped and read. Returns (page quad, text, score, up).

    Detection runs on the page shrunk within `Global.max_side_len`, as the engine's own call
    does it, and the boxes are scaled back to crop from the full page. Measured: detecting on
    a 9652x6785 scan at full size ran the GPU out of memory (DirectML 8007000E) and failed
    the whole manual."""
    from rapidocr.ch_ppocr_rec.typings import TextRecInput
    from rapidocr.utils.process_img import get_rotate_crop_image
    H = img.shape[0]
    turned_img = np.ascontiguousarray(np.rot90(img, -1))       # clockwise
    small, op = engine.preprocess_img(turned_img)
    ry, rx = op['preprocess']['ratio_h'], op['preprocess']['ratio_w']
    det = engine.text_det(small)
    if det.boxes is None or not len(det.boxes):
        return []
    boxes = [[(x * rx, y * ry) for x, y in q] for q in det.boxes]
    level = [q for q in boxes if (lambda wh: wh[1] > 0 and wh[0] / wh[1] >= _VERTICAL_RATIO)(_sides(q))]
    if not level:
        return []
    crops = [get_rotate_crop_image(turned_img, np.array(q, dtype=np.float32)) for q in level]
    crops, flipped = _right_way_up(engine, crops)
    rec = engine.text_rec(TextRecInput(img=crops))
    out = []
    for q, f, txt, sc in zip(level, flipped, rec.txts, rec.scores):
        # turned point (x, y) came from page point (y, H - 1 - x)
        page_q = [(float(y), float(H - 1 - x)) for x, y in q]
        # level and upright on the clockwise-turned page = reads bottom to top on the page
        out.append((page_q, txt, float(sc), not f))
    return out


def _area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _aabb(q):
    xs = [p[0] for p in q]
    ys = [p[1] for p in q]
    return (min(xs), min(ys), max(xs), max(ys))


def _overlap(a, b):
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


# A line found on the turned page must be at least this thick, as a fraction of the page's
# median level line. Measured on three Japanese wiring diagrams: 18 real rotated labels (wire
# colours, "(セダン)", "ATコントロールユニット") were 0.63-0.89 of it; columns of connector
# pin cells, which turned sideways read as one line ("72", "C.7", "a1", "-+"), 0.24-0.56.
_TURNED_THICK = 0.6
# An upright reading this confident is not replaced by a turned line that does not contain
# its text: a stacked pair of pin numbers read "2" and "3" at 1.0 upright and "N3" turned.
_UPRIGHT_SURE = 0.95


def _merge(upright, turned):
    """The upright pass's lines, with the turned pass's vertical lines added where they are
    new or more complete.

    A turned line is taken only if it looks like real text by the upright pass's own account:
    - thick enough for this page's text (`_TURNED_THICK`), and
    - not lying over (half of its box) a level line of two or more characters, nor over a
      confident upright reading whose text it does not contain (`_UPRIGHT_SURE`) -- columns
      of stacked table entries and pin numbers read as one "vertical line" turned.
    Once taken, it replaces the upright lines it covers: the cut-short vertical boxes and the
    one-glyph scraps of the same label."""
    boxes, txts, scores, up = (list(x) for x in upright)
    ub = [_aabb(q) for q in boxes]
    level = sorted(b[3] - b[1] for b, t, u in zip(ub, txts, up)
                   if u is None and len(t.strip()) >= 2 and b[2] - b[0] > b[3] - b[1])
    line_h = level[len(level) // 2] if level else 0.0
    drop = set()
    add = []
    for q, txt, sc, u in turned:
        if sc < _TEXT_SCORE or len(txt.strip()) < 2:
            continue
        tb = _aabb(q)
        if tb[2] - tb[0] < _TURNED_THICK * line_h:
            continue
        covered = [i for i, b in enumerate(ub) if _area(b) and _overlap(b, tb) >= 0.5 * _area(b)]
        if any(up[i] is None and len(txts[i].strip()) >= 2 and ub[i][2] - ub[i][0] > ub[i][3] - ub[i][1]
               for i in covered):
            continue
        if any(scores[i] >= _UPRIGHT_SURE and txts[i].strip() not in txt for i in covered):
            continue
        drop.update(covered)
        add.append((q, txt, sc, u))
    keep = [i for i in range(len(boxes)) if i not in drop]
    return ([boxes[i] for i in keep] + [a[0] for a in add],
            [txts[i] for i in keep] + [a[1] for a in add],
            [scores[i] for i in keep] + [a[2] for a in add],
            [up[i] for i in keep] + [a[3] for a in add])


def _line(quad, text: str, score: float, up: bool | None = None) -> OcrElement:
    xs = [float(p[0]) for p in quad]
    ys = [float(p[1]) for p in quad]
    box = BoundingBox(left=min(xs), top=min(ys), right=max(xs), bottom=max(ys))
    w, h = box.right - box.left, box.bottom - box.top
    # A vertical line is a horizontal one turned a quarter: top to bottom is 90 degrees
    # clockwise, 270 counter-clockwise in hOCR's convention; bottom to top is 90.
    angle = None
    word_box = box
    if len(text) > 1 and h > _VERTICAL_RATIO * w:
        angle = 90.0 if up else 270.0
        # OCRmyPDF's renderer (16.x, fpdf_renderer) stretches a word to its box's width ACROSS
        # THE PAGE, even on a turned line, so a label running up a diagram was squeezed into
        # its own thickness: measured, "BrY(セダン)" on a 35 pt label covered 9 pt of it. The
        # word's page-x extent is read for nothing else on a turned line (its place along the
        # line comes from its page-y extent), so it is set to the line's length.
        word_box = BoundingBox(left=box.left, top=box.top, right=box.left + h, bottom=box.bottom)
        if not up:
            # The renderer turns a line about its box's top-left corner, so a line reading
            # top to bottom is laid one thickness LEFT of its box: measured on a drawn label
            # with ink at x 410-417 pt, the layer sat at 396-408. Shifting the box right by
            # its width lands it on the label; the width, the length and the place along the
            # line are unchanged.
            box = BoundingBox(left=box.left + w, top=box.top, right=box.right + w, bottom=box.bottom)
            word_box = BoundingBox(left=box.left, top=box.top, right=box.left + h, bottom=box.bottom)
    word = OcrElement(ocr_class=OcrClass.WORD, bbox=word_box, text=text, confidence=float(score))
    return OcrElement(ocr_class=OcrClass.LINE, bbox=box, textangle=angle, children=[word])


def ocr_page(input_file: Path, page_number: int = 0,
             max_dpi: float = 0) -> tuple[OcrElement, str]:
    """OCR one page image into an OcrElement page and its plain text, lines in reading
    order (top to bottom, then left to right). Split out of the plugin class so it can be
    called and tested without an OCRmyPDF run.

    `max_dpi` (0 = off) reads a page scanned finer than that at `max_dpi` instead, then maps
    the lines back onto the full-size image, so the text layer still fits the page it is
    laid over. It is the speed knob: measured on a 138-page Japanese manual scanned at
    600 dpi, recognition at native resolution took ~22 s a page on a GTX 1060 and filled its
    6 GB, while 300 dpi took 12-18 s — at a small cost, Japanese recall 0.805 against 0.819,
    with the same 25 of 28 hand-checked diagram labels found."""
    with Image.open(input_file) as img:
        width, height = img.size
        known = 'dpi' in img.info
        dpi = img.info.get('dpi', (300, 300))
        dpi = float(dpi[0] if isinstance(dpi, tuple) else dpi)
        scale = max_dpi / dpi if (max_dpi and known and dpi > max_dpi) else 1.0
        src = (img.resize((round(width * scale), round(height * scale)), Image.LANCZOS)
               if scale < 1.0 else str(input_file))
    page = OcrElement(ocr_class=OcrClass.PAGE,
                      bbox=BoundingBox(left=0, top=0, right=width, bottom=height),
                      dpi=dpi, page_number=page_number)
    if any(_providers()):
        # Building, using and dropping an engine all touch the GPU, so all of it queues on
        # the lock. Recycling with only the recognition call locked crashed DirectML with an
        # access violation (0xC0000005) after ~100 rebuilds on a 1,071-page manual: a new
        # session was created, or an old one torn down, while another page was mid-call.
        with _gpu_lock:
            r = _recognise_on_gpu(src)
    else:
        r = _recognise(src)
    if r is None:
        return page, ''
    boxes, txts, scores, up = r
    if scale < 1.0:
        boxes = [[(x / scale, y / scale) for x, y in q] for q in boxes]
    return _page_from(page, boxes, txts, scores, up)


def _page_from(page: OcrElement, boxes, txts, scores, up=None) -> tuple[OcrElement, str]:
    """Fill `page` with one LINE per detected text box, in reading order."""
    up = up if up is not None else [None] * len(boxes)
    items = sorted(zip(boxes, txts, scores, up),
                   key=lambda t: (round(min(p[1] for p in t[0]) / 20), min(p[0] for p in t[0])))
    lines = [(q, s, c, u) for q, s, c, u in items if s and s.strip()]
    page.children = [_line(q, s, c, u) for q, s, c, u in lines]
    return page, '\n'.join(s for _, s, _, _ in lines)


class PaddleOcrEngine(OcrEngine):
    """PP-OCRv6 medium through RapidOCR/onnxruntime."""

    @staticmethod
    def version() -> str:
        return f'PP-OCRv6-medium/rapidocr-{_dist_version("rapidocr")}'

    @staticmethod
    def creator_tag(options) -> str:
        return f'PaddleOCR {PaddleOcrEngine.version()}'

    def __str__(self) -> str:
        return f'PaddleOCR {self.version()}'

    @staticmethod
    def languages(options) -> set[str]:
        return set(LANGUAGES)

    @staticmethod
    def get_orientation(input_file: Path, options) -> OrientationConfidence:
        # No page-orientation guess: only used by --rotate-pages, which this tool never
        # passes. Rotated LINES are still read — the detector finds them as they lie.
        return OrientationConfidence(angle=0, confidence=0.0)

    @staticmethod
    def supports_generate_ocr() -> bool:
        return True

    @staticmethod
    def generate_ocr(input_file: Path, options, page_number: int = 0):
        return ocr_page(input_file, page_number,
                        max_dpi=getattr(options, 'paddle_dpi', 0) or 0)

    @staticmethod
    def generate_hocr(input_file: Path, output_hocr: Path, output_text: Path, options):
        raise NotImplementedError('ocrmypdf_paddle implements generate_ocr only')

    @staticmethod
    def generate_pdf(input_file: Path, output_pdf: Path, output_text: Path, options):
        raise NotImplementedError('ocrmypdf_paddle implements generate_ocr only '
                                  '(use the default --pdf-renderer)')


@hookimpl
def add_options(parser):
    g = parser.add_argument_group('PaddleOCR', 'Options for the ocrmypdf_paddle plugin')
    g.add_argument('--paddle-dpi', type=int, default=0, metavar='DPI',
                   help='read pages scanned finer than DPI at DPI (faster; the text layer is '
                        'still mapped onto the full page). 0 = native resolution (default). '
                        'Measured on 600 dpi scans: 300 took 12-18 s a page against ~22 s, '
                        'Japanese recall 0.805 against 0.819')


@hookimpl
def check_options(options):
    from ocrmypdf.exceptions import BadArgsError
    if (getattr(options, 'paddle_dpi', 0) or 0) < 0:
        raise BadArgsError('--paddle-dpi must be >= 0')


@hookimpl
def get_ocr_engine(options):
    """Loading this plugin is the opt-in: it then answers for every file of the run."""
    return PaddleOcrEngine()
