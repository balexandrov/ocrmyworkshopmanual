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
_VERTICAL_RATIO = 1.5

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


def _providers() -> tuple[bool, bool]:
    """(use_dml, use_cuda): whichever GPU provider this onnxruntime build offers."""
    have = set(onnxruntime.get_available_providers())
    return 'DmlExecutionProvider' in have, 'CUDAExecutionProvider' in have


def _get_engine():
    """One RapidOCR per process: OCRmyPDF runs pages in worker processes and loading the
    models costs seconds, so it is built on the first page and reused."""
    global _engine, _gpu
    with _lock:
        if _engine is None:
            dml, cuda = _providers()
            _gpu = dml or cuda
            params = {
                'Global.log_level': 'warning',
                # 4000 px keeps an A4 scan at ~340 dpi; the default 2000 halves a 600 dpi page
                'Global.max_side_len': 4000,
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
            _engine = RapidOCR(params=params)
        return _engine


def _line(quad, text: str, score: float) -> OcrElement:
    xs = [float(p[0]) for p in quad]
    ys = [float(p[1]) for p in quad]
    box = BoundingBox(left=min(xs), top=min(ys), right=max(xs), bottom=max(ys))
    w, h = box.right - box.left, box.bottom - box.top
    # Vertical text reads top to bottom: a horizontal line rotated 90 degrees clockwise,
    # which is 270 counter-clockwise in hOCR's convention.
    angle = 270.0 if len(text) > 1 and h > _VERTICAL_RATIO * w else None
    word = OcrElement(ocr_class=OcrClass.WORD, bbox=box, text=text, confidence=float(score))
    return OcrElement(ocr_class=OcrClass.LINE, bbox=box, textangle=angle, children=[word])


def ocr_page(input_file: Path, page_number: int = 0) -> tuple[OcrElement, str]:
    """OCR one page image into an OcrElement page and its plain text, lines in reading
    order (top to bottom, then left to right). Split out of the plugin class so it can be
    called and tested without an OCRmyPDF run."""
    with Image.open(input_file) as img:
        width, height = img.size
        dpi = img.info.get('dpi', (300, 300))
        dpi = float(dpi[0] if isinstance(dpi, tuple) else dpi)
    page = OcrElement(ocr_class=OcrClass.PAGE,
                      bbox=BoundingBox(left=0, top=0, right=width, bottom=height),
                      dpi=dpi, page_number=page_number)
    engine = _get_engine()
    if _gpu:
        with _gpu_lock:
            r = engine(str(input_file))
    else:
        r = engine(str(input_file))
    if r.boxes is None:
        return page, ''
    items = sorted(zip(r.boxes, r.txts, r.scores),
                   key=lambda t: (round(min(p[1] for p in t[0]) / 20), min(p[0] for p in t[0])))
    lines = [(q, s, c) for q, s, c in items if s and s.strip()]
    page.children = [_line(q, s, c) for q, s, c in lines]
    return page, '\n'.join(s for _, s, _ in lines)


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
        return ocr_page(input_file, page_number)

    @staticmethod
    def generate_hocr(input_file: Path, output_hocr: Path, output_text: Path, options):
        raise NotImplementedError('ocrmypdf_paddle implements generate_ocr only')

    @staticmethod
    def generate_pdf(input_file: Path, output_pdf: Path, output_text: Path, options):
        raise NotImplementedError('ocrmypdf_paddle implements generate_ocr only '
                                  '(use the default --pdf-renderer)')


@hookimpl
def get_ocr_engine(options):
    """Loading this plugin is the opt-in: it then answers for every file of the run."""
    return PaddleOcrEngine()
