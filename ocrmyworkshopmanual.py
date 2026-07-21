#!/usr/bin/env python3
"""
ocrmyworkshopmanual.py

Turn a folder tree of scanned (image-only) PDFs into small, SEARCHABLE PDFs.
For each file, per page:  render (Ghostscript) -> threshold + despeckle ->
generic JBIG2 (jbig2enc) -> then add an invisible OCR text layer (ocrmypdf).

Why not just `ocrmypdf --optimize 3`? ocrmypdf only JBIG2-compresses images that
are ALREADY 1-bit; it won't binarize grayscale scans, so on these it lands ~37%
(lossy JPEG) vs ~8% here. And its JBIG2 page-grouping is no longer controllable,
so it can emit shared-dictionary JBIG2 that renders BLANK in Chrome/Edge (PDFium).
This tool binarizes first (→ ~8%) and uses GENERIC JBIG2 (no shared dictionary,
so it renders everywhere), then hands the result to ocrmypdf purely for the text
layer (--optimize 0, images untouched).

One worker process per file → uses all cores. Originals are never touched; output
mirrors the source tree under a sibling "(COMPRESSED)" folder (or --dest).
Skip-if-exists, so it is resumable. Typical result on clean B&W scans: ~8-12% of
original, crisp, and full-text searchable.
NOTE: for SCANNED/image PDFs only. A SAFETY CHECK (looks_born_digital) detects
born-digital/vector/text PDFs and copies them to dest byte-for-byte, untouched
(never rasterised); disable with --no-skip-born-digital. Every folder run also
writes a report log (which file, what was done, final stats); disable with --no-log.

Usage:
  python ocrmyworkshopmanual.py "M:\\path\\to\\folder"           # compress + OCR a tree
  python ocrmyworkshopmanual.py "one_manual.pdf"                # a single file -> sibling (COMPRESSED).pdf
  python ocrmyworkshopmanual.py SRC --dry-run                    # preview only, write nothing
  python ocrmyworkshopmanual.py SRC --dest OUT --workers 10
  python ocrmyworkshopmanual.py SRC --limit 3                    # test first N files
  python ocrmyworkshopmanual.py SRC --no-ocr                     # compress only
  python ocrmyworkshopmanual.py SRC --ocr-only                   # add text layer only, no compression
  python ocrmyworkshopmanual.py SRC --language eng+fra+spa+deu   # multilingual OCR
  python ocrmyworkshopmanual.py SRC --symbol                     # smaller, GS/Acrobat only

Page-type router: classify_page() sorts each page into a PageType (PT_LINE/PT_BLANK
  bitonal, PT_PHOTO_GRAY, PT_PHOTO_COLOR) and the router dispatches it to that type's
  strategy; add a page kind by extending classify_page() + the router branch.

Tuning notes (learned on Toyota FSM scans):
  OCR (default on) adds a searchable text layer via ocrmypdf; --no-ocr to skip.
                   Needs Tesseract on PATH and ocrmypdf installed.
  GENERIC (default) vs --symbol: symbol mode shares one glyph dictionary across
                   all pages → ~30% smaller, BUT PDFium (Chrome/Edge) renders a
                   large shared dictionary as BLANK pages. Generic works everywhere.
  --dpi 200        good speed/quality balance for on-screen viewing (~native ~220).
  ADAPTIVE (default) binarization = background-flatten + Sauvola: keeps faint strokes
                   and dotted leaders on low-contrast/yellowed scans and resolves a
                   gray shaded wash (foldout wiring diagrams) cleanly, where a fixed
                   global threshold either erodes ink or (high) makes salt-and-pepper.
                   --sauvola-k tunes boldness; --global-threshold restores fixed --threshold.
  photo pages      grayscale photo/mixed pages are paper-whitened + edge-trimmed
                   (--no-photo-clean off); colour detection is cast-robust so a sepia
                   B&W page stays whitened-grayscale, not a yellow colour JPEG.
  --min-size 10    drop black connected components smaller than N px (scan speckle).

Dependencies:
  pip:       numpy, scipy, Pillow, ocrmypdf, pypdf, img2pdf  (pip install -r requirements.txt)
  Ghostscript: on PATH (or set env JBIG2_GS). ghostscript.com / apt / brew.
  jbig2enc:  the `jbig2` binary on PATH (or env JBIG2_BIN). apt install jbig2enc /
             brew install jbig2enc / Windows build at github.com/agl/jbig2enc/releases.
             The jbig2topdf.py wrapper ships in this repo's tools/.
  Tesseract: on PATH (for OCR). Windows: auto-added from C:\\Program Files\\Tesseract-OCR.
"""

import argparse
import concurrent.futures as cf
import csv
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import namedtuple
from pathlib import Path

import img2pdf
import numpy as np
from PIL import Image
from pypdf import PdfReader, PdfWriter
from scipy import ndimage

sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)

Image.MAX_IMAGE_PIXELS = None  # trusted local scans; foldout pages can be huge

_STRUCT8 = np.ones((3, 3), bool)  # 8-connectivity for speckle labeling
SCRIPT_DIR = Path(__file__).resolve().parent

# ── Page types ───────────────────────────────────────────────────────────────
# Each scanned page is classified into one PageType, and the router dispatches it
# to a per-type strategy. To handle a new kind of page, add a type here, a rule in
# classify_page(), and a branch in the router (see compress_one) — nothing else.
PT_BLANK = 'blank'              # near-empty  -> folds into the bitonal run (JBIG2 ~nothing)
PT_LINE = 'line'               # text / line-art (incl. gray-wash/shadow) -> flatten+Sauvola -> JBIG2
PT_PHOTO_GRAY = 'photo_gray'   # B&W photo / halftone / stipple -> whiten paper + trim edges -> gray JPEG
PT_PHOTO_COLOR = 'photo_color' # genuine colour (covers, colour diagrams) -> colour JPEG
_PT_BITONAL = (PT_BLANK, PT_LINE)  # types that share the grouped-JBIG2 path

# classify_page() result: the page's type, plus a pre-rendered colour PNG for photo
# pages (so the strategy doesn't re-render), else None.
PageClass = namedtuple('PageClass', 'type color_png')


# ── Tool discovery (recomputed on import in each spawned worker) ──────────────

def _find_ghostscript():
    env = os.environ.get('JBIG2_GS')
    if env and Path(env).exists():
        return env
    for name in ('gswin64c', 'gswin32c', 'gs'):
        found = shutil.which(name)
        if found:
            return found
    for base in (r'C:\Program Files\gs', r'C:\Program Files (x86)\gs'):
        b = Path(base)
        if b.exists():
            hits = sorted(b.glob('*/bin/gswin64c.exe')) or sorted(b.glob('*/bin/gswin32c.exe'))
            if hits:
                return str(hits[-1])  # newest version
    return None


def _find_jbig2_binary():
    """jbig2enc encoder: env JBIG2_BIN, then PATH, then a local tools/ dir."""
    env = os.environ.get('JBIG2_BIN')
    if env and Path(env).exists():
        return env
    found = shutil.which('jbig2') or shutil.which('jbig2.exe')
    if found:
        return found
    exe = 'jbig2.exe' if os.name == 'nt' else 'jbig2'
    dirs = [Path(os.environ['JBIG2_BIN_DIR'])] if os.environ.get('JBIG2_BIN_DIR') else []
    dirs += [SCRIPT_DIR / 'tools' / 'jbig2', SCRIPT_DIR / 'tools']
    for d in dirs:
        if (d / exe).exists():
            return str(d / exe)
    return None


def _find_wrapper():
    """jbig2topdf.py (bundled in tools/; also accept one on PATH)."""
    for c in (SCRIPT_DIR / 'tools' / 'jbig2topdf.py', SCRIPT_DIR / 'jbig2topdf.py'):
        if c.exists():
            return str(c)
    return shutil.which('jbig2topdf.py')


GS = _find_ghostscript()
JBIG = _find_jbig2_binary()
WRAP = _find_wrapper()
PY = sys.executable

# make Tesseract discoverable even if PATH wasn't refreshed this session
for _d in (r'C:\Program Files\Tesseract-OCR', r'C:\Program Files (x86)\Tesseract-OCR'):
    if os.path.isdir(_d) and _d not in os.environ.get('PATH', ''):
        os.environ['PATH'] = _d + os.pathsep + os.environ.get('PATH', '')


def set_below_normal_priority():
    """Lower this process (and thus its subprocess children, which inherit it) to
    below-normal priority so long runs keep the machine responsive."""
    try:
        if os.name == 'nt':
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL
        else:
            os.nice(10)
    except Exception:
        pass


def _ocrmypdf_ok():
    try:
        return subprocess.run([PY, '-m', 'ocrmypdf', '--version'],
                              capture_output=True).returncode == 0
    except Exception:
        return False


def check_tools(want_ocr: bool):
    """Return an error string if a required tool is missing, else None."""
    if not GS:
        return ('Ghostscript not found. Install it (ghostscript.com / apt install '
                'ghostscript / brew install ghostscript) and put it on PATH, or set env JBIG2_GS.')
    if not JBIG:
        return ('jbig2enc not found. Install it and put `jbig2` on PATH (or set env JBIG2_BIN). '
                'Linux: apt install jbig2enc  |  macOS: brew install jbig2enc  |  '
                'Windows: github.com/agl/jbig2enc/releases (unzip, add bin/ to PATH).')
    if not WRAP:
        return (f'jbig2topdf.py wrapper missing (expected in {SCRIPT_DIR / "tools"}). '
                'It ships with this repo — restore tools/jbig2topdf.py.')
    if want_ocr:
        if not shutil.which('tesseract'):
            return ('Tesseract not found (needed for OCR). Install it '
                    '(choco install tesseract / winget install UB-Mannheim.TesseractOCR), '
                    'or run with --no-ocr.')
        if not _ocrmypdf_ok():
            return 'ocrmypdf not available (pip install ocrmypdf), or run with --no-ocr.'
    return None


# ── Per-page cleanup ─────────────────────────────────────────────────────────

def _flatten_bg(g: np.ndarray, win: int, f: int = 4) -> np.ndarray:
    """Flatten uneven paper: estimate the background field (grey-closing fills the
    ink/detail, a box blur smooths what's left) and divide the page by it, so a
    yellow cast, binding-shadow washes and edge darkening all normalise toward white.
    Returns uint8 gray. Large detail (e.g. a photo) exceeds the window and is kept.

    The background field is low-frequency, so it is estimated on an f× downscaled
    copy and upscaled back — on large-format pages (tens of MP) this is several times
    faster than grey-closing at full resolution, for a pixel-identical result (the
    divide itself stays full-res)."""
    h, w = g.shape
    sm = np.asarray(Image.fromarray(g).resize((max(1, w // f), max(1, h // f)), Image.BILINEAR))
    ws = max(3, round(win / f))
    bg = ndimage.grey_closing(sm, size=(ws, ws)).astype(np.float32)
    bg = np.maximum(ndimage.uniform_filter(bg, ws), 1.0)
    bg = np.asarray(Image.fromarray(np.clip(bg, 0, 255).astype(np.uint8))
                    .resize((w, h), Image.BILINEAR)).astype(np.float32)
    return np.clip(g.astype(np.float32) / np.maximum(bg, 1.0) * 255.0, 0, 255).astype(np.uint8)


def _sauvola_ink(g: np.ndarray, win: int, k: float, R: float = 128.0) -> np.ndarray:
    """Sauvola local adaptive threshold: T = m*(1 + k*(s/R - 1)) with local mean m
    and std s over a `win`-px window (O(1)/px via box filters). Returns a boolean ink
    mask (True where ink). Because the cutoff adapts per region, faint low-contrast
    strokes survive where a single global threshold erodes them, and a mid-gray wash
    resolves cleanly instead of breaking into salt-and-pepper speckle."""
    gf = g.astype(np.float32)   # float32 halves the box-filter cost; precision is ample here
    m = ndimage.uniform_filter(gf, win)
    s = np.sqrt(np.maximum(ndimage.uniform_filter(gf * gf, win) - m * m, 0.0))
    return g < m * (1.0 + k * (s / R - 1.0))


def binarize_png(path: Path, adaptive: bool, thresh: int, min_size: int,
                 despeckle: bool, dpi: int, sauvola_k: float = 0.30, ink_floor: int = 100):
    """In place: turn a grayscale page PNG into a 1-bit PNG. ADAPTIVE (default) does
    background-flatten + Sauvola, so low-contrast/yellowed scans keep their faint
    strokes and dotted leaders and a gray wash doesn't speckle; GLOBAL uses a fixed
    `thresh` (legacy, --global-threshold). Then optionally drops black connected
    components smaller than min_size px (scan speckle). Window sizes scale with dpi.
    ink_floor: any flattened pixel darker than this is forced to ink — Sauvola alone
    HOLLOWS OUT solid-black interiors (bold display type, filled tabs) because a big
    uniform-dark area has ~no local variance, so this floor keeps blacks solid."""
    g = np.asarray(Image.open(path).convert('L'))
    if adaptive:
        flat_win = max(21, round(dpi * 0.30))  # ~background/paper scale
        sauv_win = max(15, round(dpi * 0.20))  # ~a few characters
        flat = _flatten_bg(g, flat_win)
        ink = _sauvola_ink(flat, sauv_win, sauvola_k)
        ink |= flat < ink_floor                # keep solid-black fills solid
    else:
        ink = g < thresh
    if despeckle:
        lbl, _ = ndimage.label(ink, structure=_STRUCT8)
        counts = np.bincount(lbl.ravel())
        small = np.where(counts < min_size)[0]
        small = small[small != 0]
        if small.size:
            ink = ink & ~np.isin(lbl, small)
    arr = np.where(ink, 0, 255).astype('uint8')
    Image.fromarray(arr).convert('1').save(path)


def _paper_envelope(g: np.ndarray, f: int = 8) -> np.ndarray:
    """Smooth BRIGHT-paper estimate that survives large solid-dark regions: on an f×
    downscaled copy take a wide local maximum (≈ paper luminance, which fills even big
    black fills), smooth it, upscale. Dividing a page by this normalises paper→white and
    lighting/shadow gradients WITHOUT washing solid blacks (dark / bright stays dark) —
    a small-window background estimate instead divides a big black fill by itself → gray."""
    h, w = g.shape
    small = np.asarray(Image.fromarray(g).resize((max(1, w // f), max(1, h // f))))
    env = ndimage.grey_dilation(small, size=31)
    env = ndimage.uniform_filter(env.astype(np.float32), 31)
    bg = np.asarray(Image.fromarray(env.astype(np.uint8)).resize((w, h))).astype(np.float32)
    return np.maximum(bg, 1.0)


def _soft_levels(norm: np.ndarray, bp: float = 0.28, wp: float = 0.98,
                 knee: float = 0.85) -> np.ndarray:
    """Contrast curve for photo/mixed pages. Linearly map [bp, wp] -> [0, 1] (the black
    point bp deepens shadows = more contrast, less 'washed'), but SOFT-KNEE the highlights
    above `knee` so a photograph's bright tones roll off gently toward white instead of a
    hard clip to paper-white — a hard white-point blows out the photo's light detail (sky/
    chrome/background). Input `norm` is the page divided by its paper envelope (~1.0=paper)."""
    x = np.clip((norm - bp) / (wp - bp), 0.0, 1.2)
    hi = x > knee
    x[hi] = knee + (1 - knee) * (1 - np.exp(-(x[hi] - knee) / (1 - knee)))
    return np.clip(x, 0.0, 1.0)


def _clean_paper(g: np.ndarray, dpi: int, descreen: float = 0.6) -> np.ndarray:
    """Whiten the paper on a photo/mixed page: optionally DESCREEN (a mild gaussian that
    merges the scan's halftone dot grain into smooth tone — less 'dithering', smaller
    JPEG, negligible line softening; sigma scales with dpi, 0 disables), flat-field
    divide by a bright-paper envelope (removes the yellow cast and uneven lighting while
    keeping solid blacks black), apply a soft-levels tone curve (contrast without blowing
    out the photo's highlights), and blank a dark scan-edge border. g / return: uint8 gray."""
    if descreen > 0:
        g = ndimage.gaussian_filter(g, descreen * dpi / 150.0)
    bg = _paper_envelope(g)
    out = (_soft_levels(g.astype(np.float32) / bg) * 255.0).astype(np.uint8)
    H, W = out.shape
    m = max(4, int(min(H, W) * 0.02))
    for strip in (np.s_[:m, :], np.s_[-m:, :], np.s_[:, :m], np.s_[:, -m:]):
        if (out[strip] < 110).mean() > 0.4:  # a mostly-dark margin = scan-edge shadow
            out[strip] = 255
    return out


def photo_coverage(png: Path, dpi: int) -> float:
    """Fraction of the page covered by dense continuous-tone TILES. A whole-page
    average misses a photo that only fills part of an otherwise-blank page, so we
    tile (~0.85 inch) and count tiles that are mostly mid-tone. High on any page
    containing a photo (even partial); ~0 on pure line-art/text."""
    a = np.asarray(Image.open(png).convert('L'))
    mid = ((a >= 40) & (a <= 215)).astype(np.float32)
    tile = max(32, round(0.85 * dpi))
    H, W = mid.shape
    ny, nx = H // tile, W // tile
    if ny == 0 or nx == 0:
        return float(mid.mean())
    blocks = mid[:ny * tile, :nx * tile].reshape(ny, tile, nx, tile).mean(axis=(1, 3))
    return float((blocks > 0.35).mean())


def _is_color(a: np.ndarray) -> bool:
    """True if the page has genuine colour, robust to a uniform yellow/sepia paper
    cast. White-balance each channel to its 95th percentile (removing the cast), then
    require real chroma on actual marks (non-near-white pixels). A sepia B&W scan goes
    neutral -> False; a colour cover/diagram keeps its saturation -> True. (A naive
    max-minus-min test flags every yellowed page as 'colour'.) a: HxWx3 int array."""
    a = a.astype(np.float32)
    wp = np.maximum(np.percentile(a.reshape(-1, 3), 95, axis=0), 1.0)
    b = np.clip(a * (255.0 / wp), 0, 255)
    mx, mn = b.max(2), b.min(2)
    marks = mn < 200
    if int(marks.sum()) < 50:
        return False
    return float(((mx - mn)[marks] > 45).mean()) > 0.06


def classify_page(png: Path, page_no: int, src_p: Path, work: Path, dpi: int,
                  detect_photos: bool, photo_thresh: float, photo_dpi: int,
                  blank_ink: float = 0.0008) -> PageClass:
    """Route one rendered grayscale page to a PageType. Cheap signals: ink fraction
    (BLANK), tiled continuous-tone coverage (PHOTO vs LINE), and — for photo pages —
    a colour render + cast-robust colour test (PHOTO_GRAY vs PHOTO_COLOR). The colour
    PNG is rendered once here and handed to the strategy via PageClass.color_png.

    A page is only BLANK when it has neither ink NOR continuous-tone coverage: bright
    colour pages (an orange/pastel cover) convert to a grayscale luminance that is all
    >= 100, so the ink test alone would call them blank and destroy them as bitonal —
    the coverage guard keeps them on the photo/colour path."""
    g = np.asarray(Image.open(png).convert('L'))
    cov = photo_coverage(png, dpi) if detect_photos else 0.0
    if float((g < 100).mean()) < blank_ink and cov <= photo_thresh:
        return PageClass(PT_BLANK, None)
    if not detect_photos or cov <= photo_thresh:
        return PageClass(PT_LINE, None)
    # continuous-tone page: render colour once, decide gray vs colour
    d = photo_dpi or dpi
    cpng = work / f'color{page_no}.png'
    subprocess.run([GS, '-sDEVICE=png16m', f'-r{d}', f'-dFirstPage={page_no}', f'-dLastPage={page_no}',
                    '-dNOPAUSE', '-dBATCH', '-dQUIET', '-sOutputFile=' + str(cpng), win_long(src_p)],
                   capture_output=True)
    if not cpng.exists():
        return PageClass(PT_LINE, None)  # colour render failed -> treat as line
    a = np.asarray(Image.open(cpng).convert('RGB')).astype(np.int16)
    return PageClass(PT_PHOTO_COLOR if _is_color(a) else PT_PHOTO_GRAY, cpng)


def photo_seg_pdf(pc: PageClass, out_pdf: Path, work: Path, page_no: int,
                  d: int, quality: int, clean: bool, descreen: float = 0.6):
    """Strategy for PHOTO_GRAY / PHOTO_COLOR: JPEG the pre-rendered colour page and wrap
    it to a 1-page PDF sized (via embedded dpi) to match the bitonal pages. Colour pages
    are kept as-is; grayscale (B&W photo/mixed/stipple) pages get descreen + paper-whitening
    + dark scan-edge cleanup (skipped on a full-bleed photo with little paper) via _clean_paper."""
    im = Image.open(pc.color_png).convert('RGB')
    if pc.type == PT_PHOTO_COLOR:
        out_im = im
    else:
        g = np.asarray(im.convert('L'))
        if clean and float((g > 200).mean()) > 0.10:  # paper present -> document page, not full-bleed
            g = _clean_paper(g, d, descreen)
        out_im = Image.fromarray(g)
    jpg = work / f'photo{page_no}.jpg'
    out_im.save(jpg, 'JPEG', quality=quality, dpi=(d, d))
    pc.color_png.unlink(missing_ok=True)
    with open(out_pdf, 'wb') as f:
        f.write(img2pdf.convert(str(jpg)))


def has_text(pdf: Path, sample: int = 6, min_chars: int = 40) -> bool:
    """True if the PDF already has a real text layer (sampled first pages)."""
    try:
        r = PdfReader(str(pdf))
        n = min(sample, len(r.pages))
        return sum(len((r.pages[i].extract_text() or '').strip()) for i in range(n)) >= min_chars
    except Exception:
        return False


def _largest_image_dpi(page) -> float:
    """Effective DPI of the LARGEST single raster image on a page:
    sqrt(image_pixel_area / page_area_in_sq_inches). A full-page scan yields the
    scan resolution (~72-600); a small logo/figure on a born-digital page yields a
    tiny number (a page-filling image and a stamp-sized one are worlds apart here).
    Recurses into Form XObjects. Returns 0.0 if the page has no image."""
    try:
        mb = page.mediabox
        area_in = max((float(mb.width) / 72.0) * (float(mb.height) / 72.0), 1e-6)
    except Exception:
        return 0.0
    largest = 0

    def walk(res, depth=0):
        nonlocal largest
        if not res or depth > 4:
            return
        try:
            xo = res.get_object().get('/XObject')
            if not xo:
                return
            xo = xo.get_object()
            for name in xo:
                obj = xo[name].get_object()
                sub = obj.get('/Subtype')
                if sub == '/Image':
                    largest = max(largest, int(obj.get('/Width', 0)) * int(obj.get('/Height', 0)))
                elif sub == '/Form':
                    walk(obj.get('/Resources'), depth + 1)
        except Exception:
            return

    try:
        walk(page.get('/Resources'))
    except Exception:
        pass
    return (largest / area_in) ** 0.5 if largest else 0.0


def looks_born_digital(src_p: Path, scan_fraction: float = 0.5,
                       sample: int = 8, min_chars: int = 100, dpi_floor: int = 50):
    """SAFETY heuristic: is this a born-digital (vector/text) PDF rather than a scan?
    A scanned page is dominated by a full-page raster image; a born-digital page is
    text/vector with at most small images. We sample pages and count 'scan pages'
    (those carrying a full-page image, DPI-equiv >= dpi_floor). The file is called
    born-digital when the scan-page fraction is below `scan_fraction`.

    Deliberately conservative toward NEVER skipping a real scan: a genuine scanned
    archive has a full-page image on ~every page (scan_frac ~1.0), while a born-
    digital file has ~none (scan_frac ~0.0), so the two separate cleanly and ties
    fall to 'scanned'. An all-raster 'image PDF' (e.g. images exported to PDF) reads
    as scanned and is compressed — only genuine vector/text content is protected.
    Returns (is_born_digital, signals_dict) — signals go to the run log for review."""
    sig = {'sampled': 0, 'scan_pages': 0, 'text_pages': 0, 'scan_frac': 0.0, 'chars': 0}
    try:
        r = PdfReader(str(src_p))
        n = len(r.pages)
    except Exception as ex:
        sig['error'] = f'unreadable ({ex})'
        return False, sig                      # let the normal path try (and error-report) it
    if n == 0:
        sig['error'] = 'no pages'
        return False, sig
    k = min(sample, n)
    idxs = sorted({round(i * (n - 1) / max(1, k - 1)) for i in range(k)})
    scan = text = readable = 0
    for i in idxs:
        try:
            page = r.pages[i]
        except Exception:
            continue
        readable += 1
        try:
            nchars = len((page.extract_text() or '').strip())
        except Exception:
            nchars = 0
        sig['chars'] += nchars
        if _largest_image_dpi(page) >= dpi_floor:
            scan += 1
        elif nchars >= min_chars:
            text += 1
    if readable == 0:
        sig['error'] = 'no readable pages'
        return False, sig
    frac = scan / readable
    sig.update(sampled=readable, scan_pages=scan, text_pages=text, scan_frac=round(frac, 3))
    return frac < scan_fraction, sig


def _gs_repair(src_p: Path, work: Path, timeout: int = 0):
    """Try to repair a malformed/corrupt PDF by rewriting it through Ghostscript's
    pdfwrite device (which tolerates and reconstructs a lot of broken structure).
    Returns the repaired Path on success, else None. Used as a fallback before a
    file is given up on — one bad download shouldn't just be lost in a big batch."""
    out = work / 'repaired.pdf'
    try:
        r = subprocess.run(
            [GS, '-o', str(out), '-sDEVICE=pdfwrite', '-dQUIET', '-dNOPAUSE', '-dBATCH',
             win_long(src_p)], capture_output=True, text=True, timeout=timeout or None)
    except Exception:
        return None
    return out if (r.returncode == 0 and out.exists() and out.stat().st_size > 0) else None


def _verify_output(dest_p: Path, expect_pages) -> str:
    """Cheap trust check on a written output: it must open as a PDF and (when we know
    the expected page count) have that many pages. Returns '' if OK, else a warning
    string to append to the note — so a silently-corrupt result in a big batch is
    visible in the log rather than shipped unnoticed."""
    try:
        got = len(PdfReader(str(dest_p)).pages)
    except Exception as ex:
        return f' (WARN: output failed to open: {ex})'
    if expect_pages and got != expect_pages:
        return f' (WARN: output has {got} pages, expected {expect_pages})'
    return ''


def _ocr_and_place(base: Path, dest_p: Path, src_p: Path, orig: int, work: Path,
                   ocr: bool, language: str, pages: int, kept: bool, note: str,
                   timeout: int = 0, verify: bool = True) -> dict:
    """Add an OCR text layer to `base` (only if it has none), then atomically place
    it at dest. Shared by the compress path and the --ocr-only path. `timeout` (secs,
    0=off) bounds the OCR step; `verify` re-opens the output and checks its page count."""
    final = base
    if ocr:
        if has_text(base):
            note += ' (had text, OCR skipped)'
        else:
            ocr_pdf = work / 'ocr.pdf'
            r = subprocess.run(
                [PY, '-m', 'ocrmypdf', '--language', language, '--optimize', '0',
                 '--output-type', 'pdf', '--skip-text', '--quiet', '--jobs', '1',
                 str(base), str(ocr_pdf)], capture_output=True, text=True,
                timeout=timeout or None)
            if r.returncode == 0 and ocr_pdf.exists() and ocr_pdf.stat().st_size > 0:
                final = ocr_pdf
            else:
                note += ' (OCR FAILED)'
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = dest_p.with_suffix(dest_p.suffix + '.part')
    shutil.copyfile(str(final), str(tmp_out))
    os.replace(str(tmp_out), str(dest_p))
    if verify:
        note += _verify_output(dest_p, pages)
    return {'src': src_p.name, 'orig': orig, 'new': dest_p.stat().st_size,
            'pages': pages, 'note': note, 'kept': kept, 'err': None}


def sample_projection(src_p: Path, work: Path, dpi: int, despeckle: bool, thresh: int,
                      min_size: int, photo_thresh: float, photo_dpi: int, jpeg_quality: int,
                      adaptive: bool = True, sauvola_k: float = 0.30, photo_clean: bool = True,
                      photo_descreen: float = 0.6, k: int = 10) -> float:
    """Estimate the whole-file compressed/original ratio by running the per-page
    pipeline on k evenly-spaced SAMPLE pages only (cheap 'will this compress?'
    pre-check). Returns projected ratio; ~0 if unreadable (-> just try compressing).
    Validated: good compressors project <=~0.7, non-benefiters >=~0.9."""
    try:
        n = len(PdfReader(str(src_p)).pages)
    except Exception:
        return 0.0
    orig = src_p.stat().st_size
    if n == 0 or orig == 0:
        return 1.0
    sub = work / 'sample'
    sub.mkdir(exist_ok=True)
    kk = min(k, n)
    idxs = sorted(set(round(i * (n - 1) / max(1, kk - 1)) + 1 for i in range(kk)))
    comp = got = 0
    for p in idxs:
        png = sub / f'p{p}.png'
        subprocess.run([GS, '-sDEVICE=pnggray', f'-r{dpi}', f'-dFirstPage={p}', f'-dLastPage={p}',
                        '-dNOPAUSE', '-dBATCH', '-dQUIET', '-sOutputFile=' + str(png), win_long(src_p)],
                       capture_output=True)
        if not png.exists():
            continue
        got += 1
        pc = classify_page(png, p, src_p, sub, dpi, True, photo_thresh, photo_dpi)
        if pc.type in _PT_BITONAL:
            if adaptive or despeckle:
                binarize_png(png, adaptive, thresh, min_size, despeckle, dpi, sauvola_k)
            r = subprocess.run([JBIG, '-p', '-a', '-D', str(dpi), png.name], cwd=sub, capture_output=True)
            comp += len(r.stdout)
        else:
            d = photo_dpi or dpi
            jpg = sub / f'j{p}.jpg'
            photo_seg_pdf(pc, sub / f'seg{p}.pdf', sub, p, d, jpeg_quality, photo_clean, photo_descreen)
            # photo_seg_pdf writes photo{p}.jpg then a tiny PDF wrapper; size ~ the JPEG
            comp += (sub / f'photo{p}.jpg').stat().st_size
    shutil.rmtree(sub, ignore_errors=True)
    return ((comp / got) * n / orig) if got else 0.0


# ── One file (runs in a worker process) ──────────────────────────────────────

def compress_one(src: str, dest: str, dpi: int,
                 despeckle: bool = True, thresh: int = 125, min_size: int = 10,
                 symbol: bool = False, ocr: bool = True, language: str = 'eng',
                 detect_photos: bool = True, photo_thresh: float = 0.02,
                 photo_dpi: int = 150, jpeg_quality: int = 60,
                 min_savings: float = 0.25, ocr_only: bool = False,
                 precheck: bool = True, precheck_skip: float = 0.75,
                 adaptive: bool = True, sauvola_k: float = 0.30,
                 photo_clean: bool = True, photo_descreen: float = 0.6,
                 skip_born_digital: bool = True, scan_fraction: float = 0.5,
                 timeout: int = 0, verify_output: bool = True, repair: bool = True) -> dict:
    """Render -> classify each page into a PageType -> per-type strategy -> merge -> OCR.

    PAGE-TYPE ROUTER (detect_photos=True): classify_page() sorts each page into LINE/
    BLANK (bitonal), PHOTO_GRAY or PHOTO_COLOR; consecutive bitonal pages are grouped
    into tiny generic JBIG2, photo pages become one JPEG each, all merged back in order.
    --no-photo / detect_photos=False forces every page bitonal. Binarization is ADAPTIVE
    by default (background-flatten + Sauvola: keeps faint strokes/leaders on low-contrast
    yellowed scans, resolves gray washes cleanly); adaptive=False falls back to the fixed
    global `thresh`. photo_clean whitens the paper and trims dark scan edges on grayscale
    photo/mixed pages. Colour detection is cast-robust, so a sepia B&W page is kept as
    (whitened) grayscale rather than a yellow colour JPEG. Add a page kind by extending
    classify_page() + the router branch (see the PageType constants).
    Default is GENERIC JBIG2 (each page self-contained → renders in Chrome/Edge);
    SYMBOL mode (smaller shared dict) goes BLANK in PDFium, so it's GS/Acrobat-only
    and skips photo detection.
    -D <dpi> embeds resolution so pages are sized correctly. With ocr=True,
    ocrmypdf adds an invisible text layer at the end (--optimize 0, images intact).
    """
    src_p, dest_p = Path(src), Path(dest)
    orig = src_p.stat().st_size
    work = Path(tempfile.mkdtemp(prefix='jb_'))
    try:
        # SAFETY: never rasterise a born-digital (vector/text) PDF. If the file does
        # not look like a scan, copy it through to dest byte-for-byte, untouched
        # (no render, no binarize, no OCR) — this tool is for scanned/image PDFs only.
        if skip_born_digital:
            born, bsig = looks_born_digital(src_p, scan_fraction)
            if born:
                dest_p.parent.mkdir(parents=True, exist_ok=True)
                tmp_out = dest_p.with_suffix(dest_p.suffix + '.part')
                shutil.copyfile(str(src_p), str(tmp_out))
                os.replace(str(tmp_out), str(dest_p))
                return {'src': src_p.name, 'orig': orig, 'new': dest_p.stat().st_size,
                        'pages': bsig.get('sampled'), 'kept': True, 'err': None,
                        'action': 'born_digital', 'signals': bsig,
                        'note': f' (born-digital: copied untouched; '
                                f'scan_frac={bsig.get("scan_frac")})'}
        note0 = ' (OCR-only, not compressed)' if ocr_only else ''
        # cheap pre-check: sample-compress a few pages; if it won't beat the original,
        # skip full compression and just OCR the original (avoids wasted work + growth).
        if not ocr_only and not symbol and precheck:
            proj = sample_projection(src_p, work, dpi, despeckle, thresh, min_size,
                                     photo_thresh, photo_dpi, jpeg_quality,
                                     adaptive, sauvola_k, photo_clean, photo_descreen)
            if proj >= precheck_skip:
                ocr_only = True
                note0 = f' (compression skipped: sample projected {proj*100:.0f}% of original)'
        if ocr_only:
            # No (worthwhile) compression: keep the original images, just add the OCR layer.
            base = work / 'orig.pdf'
            shutil.copyfile(str(src_p), str(base))
            res = _ocr_and_place(base, dest_p, src_p, orig, work, ocr, language,
                                 len(PdfReader(str(base)).pages), True, note0,
                                 timeout, verify_output)
            res['action'] = 'ocr_only'
            return res
        # 1) render pages to grayscale PNG
        render_src = src_p
        did_repair = False

        def _render():
            return subprocess.run(
                [GS, '-sDEVICE=pnggray', f'-r{dpi}', '-dNOPAUSE', '-dBATCH', '-dQUIET',
                 '-sOutputFile=' + str(work / 'p%04d.png'), win_long(render_src)],
                capture_output=True, text=True, timeout=timeout or None)

        r = _render()
        pngs = sorted(p.name for p in work.glob('p*.png'))
        if (r.returncode != 0 or not pngs) and repair:
            # malformed PDF? try a Ghostscript pdfwrite rewrite, then render the repaired copy
            fixed = _gs_repair(src_p, work, timeout)
            if fixed:
                render_src, did_repair = fixed, True
                for old in work.glob('p*.png'):
                    old.unlink(missing_ok=True)
                r = _render()
                pngs = sorted(p.name for p in work.glob('p*.png'))
        if r.returncode != 0 or not pngs:
            return {'src': src_p.name, 'orig': orig, 'new': 0,
                    'err': f'render failed rc={r.returncode} {r.stderr[:200]}'}
        comp = work / 'compressed.pdf'
        n_photo = 0
        n_color = 0

        def _gen_jbig2(name, k):
            """Binarize (adaptive/global + optional despeckle) + generic self-contained
            JBIG2 for one page → .jb2 name."""
            if adaptive or despeckle:
                binarize_png(work / name, adaptive, thresh, min_size, despeckle, dpi, sauvola_k)
            jb = f'g{k:05d}.jb2'
            with open(work / jb, 'wb') as jf:
                rr = subprocess.run([JBIG, '-p', '-a', '-D', str(dpi), name],
                                    cwd=work, stdout=jf, stderr=subprocess.PIPE, text=True)
            if rr.returncode != 0 or (work / jb).stat().st_size == 0:
                raise RuntimeError(f'jbig2 page {k} rc={rr.returncode} {rr.stderr[:160]}')
            return jb

        if symbol:
            # SYMBOL mode: pure bitonal, one shared dictionary (GS/Acrobat only, no photo detect)
            if adaptive or despeckle:
                for name in pngs:
                    binarize_png(work / name, adaptive, thresh, min_size, despeckle, dpi, sauvola_k)
            r = subprocess.run([JBIG, '-s', '-p', '-a', '-D', str(dpi), '-b', 'out', *pngs],
                               cwd=work, capture_output=True, text=True)
            if r.returncode != 0 or not (work / 'out.sym').exists():
                return {'src': src_p.name, 'orig': orig, 'new': 0,
                        'err': f'jbig2 failed rc={r.returncode} {r.stderr[:200]}'}
            with open(comp, 'wb') as fout:
                r = subprocess.run([PY, WRAP, 'out'], cwd=work, stdout=fout,
                                   stderr=subprocess.PIPE, text=True)
            if r.returncode != 0 or comp.stat().st_size == 0:
                return {'src': src_p.name, 'orig': orig, 'new': 0,
                        'err': f'wrap failed rc={r.returncode} {r.stderr[:200]}'}
        else:
            # GENERIC + PAGE-TYPE ROUTER: classify every page, then dispatch each to its
            # strategy. Consecutive BITONAL pages (LINE/BLANK) are grouped into one
            # multi-page JBIG2 PDF (smaller); PHOTO_* pages become one JPEG PDF each.
            d = photo_dpi or dpi
            classes = [classify_page(work / n, k + 1, render_src, work, dpi,
                                     detect_photos, photo_thresh, photo_dpi)
                       for k, n in enumerate(pngs)]
            n_photo = sum(c.type not in _PT_BITONAL for c in classes)
            n_color = sum(c.type == PT_PHOTO_COLOR for c in classes)
            seg_pdfs = []
            i = 0
            try:
                while i < len(pngs):
                    seg = work / f's{i:05d}.pdf'
                    if classes[i].type in _PT_BITONAL:
                        # a run of consecutive bitonal pages -> one multi-page JBIG2 PDF
                        jbs, j = [], i
                        while j < len(pngs) and classes[j].type in _PT_BITONAL:
                            jbs.append(_gen_jbig2(pngs[j], j)); j += 1
                        with open(seg, 'wb') as fout:
                            r = subprocess.run([PY, WRAP, '-s', *jbs], cwd=work, stdout=fout,
                                               stderr=subprocess.PIPE, text=True)
                        if r.returncode != 0 or seg.stat().st_size == 0:
                            return {'src': src_p.name, 'orig': orig, 'new': 0,
                                    'err': f'wrap failed rc={r.returncode} {r.stderr[:200]}'}
                        i = j
                    else:  # PHOTO_GRAY / PHOTO_COLOR
                        photo_seg_pdf(classes[i], seg, work, i + 1, d, jpeg_quality, photo_clean, photo_descreen)
                        i += 1
                    seg_pdfs.append(seg)
            except RuntimeError as ex:
                return {'src': src_p.name, 'orig': orig, 'new': 0, 'err': str(ex)}

            # merge segments in page order
            if len(seg_pdfs) == 1:
                os.replace(str(seg_pdfs[0]), str(comp))
            else:
                w = PdfWriter()
                for sp in seg_pdfs:
                    w.append(str(sp))
                with open(comp, 'wb') as f:
                    w.write(f)

        with open(comp, 'rb') as f:
            if f.read(4) != b'%PDF':
                return {'src': src_p.name, 'orig': orig, 'new': 0, 'err': 'output not a PDF'}

        # 3.5) Only keep the compressed version if it is meaningfully smaller. If our
        #      re-render didn't help (already-efficient photo/colour scans), keeping it
        #      would only grow the file and risk generational quality loss -> instead
        #      keep the ORIGINAL and just add the OCR layer to it (images untouched).
        kept_original = comp.stat().st_size >= orig * (1 - min_savings)
        if kept_original:
            base = work / 'orig.pdf'
            shutil.copyfile(str(src_p), str(base))
            n_photo = n_color = 0
        else:
            base = comp

        # 4) build the per-file note, then OCR (only if no text) + place into dest.
        if kept_original:
            note = ' (kept original — compression not worthwhile)'
        elif n_photo:
            gray = n_photo - n_color
            bits = ([f'{gray} gray'] if gray else []) + ([f'{n_color} color'] if n_color else [])
            note = f' [{n_photo} photo pg: {", ".join(bits)}]'
        else:
            note = ''
        if did_repair:
            note += ' (repaired malformed PDF)'
        res = _ocr_and_place(base, dest_p, src_p, orig, work, ocr, language,
                             len(pngs), kept_original, note, timeout, verify_output)
        res['action'] = 'kept_original' if kept_original else 'compressed'
        return res
    except subprocess.TimeoutExpired as ex:
        return {'src': src_p.name, 'orig': orig, 'new': 0,
                'err': f'timed out after {timeout}s ({getattr(ex, "cmd", ["?"])[0]})'}
    except Exception as ex:
        return {'src': src_p.name, 'orig': orig, 'new': 0, 'err': repr(ex)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def win_long(p) -> str:
    """Windows extended-length path (\\\\?\\...) so Ghostscript can open inputs
    whose full path exceeds MAX_PATH (260 chars); no-op elsewhere."""
    if os.name == 'nt':
        ap = os.path.abspath(str(p))
        return ap if ap.startswith('\\\\?\\') else '\\\\?\\' + ap
    return str(p)


def mb(n): return n / 1048576


# ── Dry-run preview (runs in a worker process) ────────────────────────────────

def preview_one(src: str, dpi: int, despeckle: bool, thresh: int, min_size: int,
                ocr_only: bool, detect_photos: bool, photo_thresh: float, photo_dpi: int,
                jpeg_quality: int, min_savings: float, precheck: bool, precheck_skip: float,
                adaptive: bool, sauvola_k: float, photo_clean: bool, photo_descreen: float,
                skip_born_digital: bool, scan_fraction: float) -> dict:
    """Predict what compress_one WOULD do to a file, WITHOUT writing anything. Used by
    --dry-run so a huge collection can be previewed (born-digital? scanned? projected
    size?) before committing to a full run. Uses the same born-digital check and the
    same sample pre-check the real run uses, so the prediction tracks reality."""
    src_p = Path(src)
    orig = src_p.stat().st_size
    work = Path(tempfile.mkdtemp(prefix='jbprev_'))
    try:
        if skip_born_digital:
            born, bsig = looks_born_digital(src_p, scan_fraction)
            if born:
                return {'src': src_p.name, 'orig': orig, 'new': orig, 'pages': bsig.get('sampled'),
                        'kept': True, 'err': None, 'action': 'born_digital', 'signals': bsig,
                        'note': f' (would copy untouched; scan_frac={bsig.get("scan_frac")})'}
        if ocr_only:
            return {'src': src_p.name, 'orig': orig, 'new': orig, 'pages': None, 'kept': True,
                    'err': None, 'action': 'ocr_only', 'note': ' (OCR-only mode; not compressed)'}
        proj = sample_projection(src_p, work, dpi, despeckle, thresh, min_size,
                                 photo_thresh, photo_dpi, jpeg_quality,
                                 adaptive, sauvola_k, photo_clean, photo_descreen)
        est_new = int(proj * orig)
        if precheck and proj >= precheck_skip:
            action, note = 'ocr_only', f' (would skip compression: projected {proj*100:.0f}% of original)'
            est_new = orig
        elif proj >= (1 - min_savings):
            action, note = 'kept_original', f' (projected {proj*100:.0f}% — likely keep original)'
            est_new = orig
        else:
            action, note = 'compressed', f' (projected {proj*100:.0f}% of original)'
        return {'src': src_p.name, 'orig': orig, 'new': est_new, 'pages': None,
                'kept': action != 'compressed', 'err': None, 'action': action, 'note': note,
                'signals': {'scan_frac': round(proj, 3)}}
    except Exception as ex:
        return {'src': src_p.name, 'orig': orig, 'new': 0, 'err': repr(ex)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ── Run log ──────────────────────────────────────────────────────────────────

def _action_label(res: dict) -> str:
    """Human label for what happened to one file (drives both the per-file line and
    the summary tally in the run log)."""
    if res.get('err'):
        return 'FAILED'
    return {
        'born_digital': 'born-digital (copied untouched)',
        'ocr_only': 'OCR-only (not compressed)',
        'kept_original': 'kept original',
        'compressed': 'compressed',
    }.get(res.get('action'), 'processed')


def _flag_duplicates(results: list) -> int:
    """Annotate results that are byte-identical (share a content hash). Does NOT skip
    or merge anything — every file is still fully processed and gets its own output;
    twins (which may legitimately belong to different manuals) are only FLAGGED in the
    report so you're aware of them. Returns the number of duplicate sets found."""
    from collections import defaultdict
    groups = defaultdict(list)
    for r in results:
        if r.get('hash'):
            groups[r['hash']].append(r)
    sets = 0
    for members in groups.values():
        if len(members) > 1:
            sets += 1
            rels = sorted(m['rel'] for m in members)
            for m in members:
                others = [x for x in rels if x != m['rel']]
                m['duplicate_of'] = '; '.join(others)
                m['note'] = (m.get('note') or '') + \
                    f' [DUPLICATE — same content as: {", ".join(others)}]'
    return sets


def _report_path(log_path, dest_root: Path, report_dir, t0: float, dry_run: bool) -> Path:
    """The report .log path: an explicit --log if given, else a timestamped file in
    `report_dir` (dry-run: beside the source) or the dest root."""
    if log_path:
        return Path(log_path)
    ts = time.strftime('%Y%m%d_%H%M%S', time.localtime(t0))
    suffix = '_DRYRUN' if dry_run else ''
    return (report_dir or dest_root) / f'_ocrmyworkshopmanual_report_{ts}{suffix}.log'


def _csv_row(fields: list) -> str:
    """One correctly-quoted CSV line (used for the per-file live-flushed CSV)."""
    import io
    buf = io.StringIO()
    csv.writer(buf).writerow(fields)
    return buf.getvalue()


# Human-friendly report columns, shared by the live-flushed CSV and the final one
# so the two can never drift. Sizes are MB (2 dp), not raw bytes.
REPORT_COLUMNS = ['file', 'action', 'orig size (MB)', 'new size (MB)', '%',
                  'duplicate of', 'note', 'error']


def _report_row(r: dict) -> list:
    """Format one result dict as a human-friendly report row (matching REPORT_COLUMNS)."""
    err = r.get('err')
    o, n = r.get('orig') or 0, r.get('new') or 0
    pct = (n * 100 // o) if (not err and o) else ''
    return [r.get('rel', ''), _action_label(r),
            f'{o / 1048576:.2f}' if o else '0.00',
            f'{n / 1048576:.2f}' if (not err and n) else '',
            pct, r.get('duplicate_of', ''), (r.get('note') or '').strip(), err or '']


# Per-folder rollup: one summary row per source subfolder (+ a grand total).
FOLDER_COLUMNS = ['folder', 'files', 'orig size (MB)', 'new size (MB)', '%', 'saved (MB)']


def _folder_rows(results: list) -> list:
    """Aggregate results by their source subfolder → one summary row each (files,
    orig MB, new MB, %, saved MB), sorted by folder, with a final TOTAL row."""
    from collections import defaultdict
    agg = defaultdict(lambda: {'n': 0, 'orig': 0, 'new': 0})
    tot = {'n': 0, 'orig': 0, 'new': 0}
    for r in results:
        folder = os.path.dirname(r.get('rel', '')) or '(root)'
        for bucket in (agg[folder], tot):
            bucket['n'] += 1
            if not r.get('err'):
                bucket['orig'] += r.get('orig') or 0
                bucket['new'] += r.get('new') or 0

    def row(name, a):
        pct = (a['new'] * 100 // a['orig']) if a['orig'] else ''
        return [name, a['n'], f"{a['orig'] / 1048576:.2f}", f"{a['new'] / 1048576:.2f}",
                pct, f"{(a['orig'] - a['new']) / 1048576:.2f}"]

    rows = [row(folder, agg[folder]) for folder in sorted(agg)]
    if len(agg) > 1:
        rows.append(row('(TOTAL)', tot))
    return rows


def write_run_log(log_path, dest_root: Path, src_root: Path, results: list, settings: dict,
                  t0: float, dt: float, n_found: int, skipped: int, limit: int,
                  fail: int, done: int, kept: int, dry_run: bool = False,
                  report_dir: Path = None) -> Path:
    """Write a human-readable report of the folder run: which file, what was done
    (with the born-digital scan signals), and the final work stats. Also writes a
    machine-readable CSV sibling (same path, .csv) for filtering/sorting at scale.
    Returns the .log path. In dry_run mode sizes are projections, not actuals."""
    from collections import Counter
    import csv as _csv
    log_path = _report_path(log_path, dest_root, report_dir, t0, dry_run)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    counts = Counter(_action_label(r) for r in results)
    tot_orig = sum(r['orig'] for r in results if not r.get('err'))
    tot_new = sum(r['new'] for r in results if not r.get('err'))
    title = 'ocrmyworkshopmanual — DRY-RUN preview (nothing written)' if dry_run \
        else 'ocrmyworkshopmanual — run report'

    L = ['=' * 78, title, '=' * 78,
         f'Started : {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0))}',
         f'Finished: {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0 + dt))}',
         f'Elapsed : {dt/60:.1f} min',
         f'Source  : {src_root}',
         f'Dest    : {dest_root}',
         f'Tools   : GS={GS} | jbig2={JBIG}',
         'Settings: ' + ', '.join(f'{k}={v}' for k, v in settings.items()),
         '',
         f'PDFs found: {n_found} | already done (skipped, dest existed): {skipped}'
         + (f' | --limit {limit}' if limit else '')
         + f' | processed this run: {len(results)}',
         '', '-' * 78, 'Per-file (this run):', '-' * 78]

    for r in sorted(results, key=lambda x: x['rel'].lower()):
        if r.get('err'):
            L.append(f'[FAILED]  {r["rel"]}')
            L.append(f'           error: {r["err"]}')
            continue
        pct = r['new'] * 100 // r['orig'] if r.get('orig') else 0
        L.append(f'[{_action_label(r)}]  {r["rel"]}')
        L.append(f'           {mb(r["orig"]):.2f} -> {mb(r["new"]):.2f} MB ({pct}%){r.get("note", "")}')
        sg = r.get('signals')
        if sg:
            L.append(f'           scan signals: scan_frac={sg.get("scan_frac")} '
                     f'scan_pages={sg.get("scan_pages")}/{sg.get("sampled")} '
                     f'text_pages={sg.get("text_pages")} chars={sg.get("chars")}'
                     + (f' [{sg["error"]}]' if sg.get('error') else ''))

    L += ['', '-' * 78, 'Summary', '-' * 78]
    for label in ('compressed', 'kept original', 'OCR-only (not compressed)',
                  'born-digital (copied untouched)', 'FAILED'):
        L.append(f'  {label:33s}: {counts.get(label, 0)}')
    L.append(f'  {"skipped (dest already existed)":33s}: {skipped}')
    n_dup = sum(1 for r in results if r.get('duplicate_of'))
    if n_dup:
        L.append(f'  {"duplicate files (flagged, still processed)":33s}: {n_dup}')
    L.append('')
    if tot_orig:
        word = 'Projected total' if dry_run else 'Total size'
        saved = 'would save' if dry_run else 'saved'
        L.append(f'  {word}: {mb(tot_orig):.1f} MB -> {mb(tot_new):.1f} MB '
                 f'({tot_new*100//tot_orig}% ; {saved} {mb(tot_orig-tot_new):.1f} MB)')
    L.append('')

    # per-folder rollup section
    frows = _folder_rows(results)
    if frows:
        L += ['-' * 78, 'Per-folder summary  (files | orig MB -> new MB | % | saved MB)', '-' * 78]
        for folder, n, omb, nmb, pct, smb in frows:
            L.append(f'  {folder}')
            L.append(f'      {n} files | {omb} -> {nmb} MB | {pct}% | saved {smb} MB')
        L.append('')

    log_path.write_text('\n'.join(L), encoding='utf-8')

    # machine-readable siblings for filtering/sorting a large collection
    with open(log_path.with_suffix('.csv'), 'w', newline='', encoding='utf-8') as f:
        w = _csv.writer(f)
        w.writerow(REPORT_COLUMNS)
        for r in sorted(results, key=lambda x: x['rel'].lower()):
            w.writerow(_report_row(r))
    # per-folder summary: one row per folder (folder + aggregate numbers only)
    with open(log_path.parent / (log_path.stem + '_by_folder.csv'), 'w', newline='',
              encoding='utf-8') as f:
        w = _csv.writer(f)
        w.writerow(FOLDER_COLUMNS)
        w.writerows(frows)
    return log_path


# ── Config file / dedup / retry helpers ──────────────────────────────────────

def _apply_config_defaults(ap: argparse.ArgumentParser) -> None:
    """Load a TOML config of default option values and fold them into the parser
    (so an explicit CLI flag still overrides). Uses --config if given, else
    ./ocrmyworkshopmanual.toml when present. Keys are long option names with dashes
    as underscores (e.g. `dpi = 300`, `no_ocr = true`, `dest = "OUT"`)."""
    import tomllib
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument('--config', type=Path)
    known, _ = pre.parse_known_args()
    path = known.config or (Path('ocrmyworkshopmanual.toml')
                            if Path('ocrmyworkshopmanual.toml').exists() else None)
    if not path:
        return
    if not path.exists():
        print(f'ERROR: config file not found: {path}', file=sys.stderr); sys.exit(1)
    try:
        with open(path, 'rb') as f:
            cfg = tomllib.load(f)
    except Exception as ex:
        print(f'ERROR: could not parse config {path}: {ex}', file=sys.stderr); sys.exit(1)
    types = {a.dest: a.type for a in ap._actions}
    valid = set(types) - {'help', 'config', 'src'}
    mapped, unknown = {}, []
    for k, v in cfg.items():
        dest = k.replace('-', '_')
        if dest in valid:
            if types.get(dest) is Path and isinstance(v, str):
                v = Path(v)
            mapped[dest] = v
        else:
            unknown.append(k)
    if unknown:
        print(f'WARNING: ignoring unknown config keys: {", ".join(unknown)}', file=sys.stderr)
    ap.set_defaults(**mapped)
    print(f'Config: loaded {len(mapped)} setting(s) from {path}')


def _file_hash(path: Path, chunk: int = 1 << 20) -> str:
    """SHA-1 of a file's bytes (for byte-identical duplicate detection)."""
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(chunk), b''):
            h.update(block)
    return h.hexdigest()


def _read_failed_rels(csv_path: Path) -> list:
    """Return the rel-paths marked FAILED (non-empty error column) in a report CSV."""
    rels = []
    with open(csv_path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if (row.get('error') or '').strip():
                rels.append(row['file'])
    return rels


# ── Batch driver ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description='Compress scanned PDFs to small generic-JBIG2 and add a searchable OCR text layer.')
    ap.add_argument('src', type=Path,
                    help='source: a folder tree of scanned PDFs, OR a single .pdf file')
    ap.add_argument('--dest', type=Path, default=None,
                    help='output root for a folder (default: sibling "<src> (COMPRESSED)"), '
                         'or the output path/folder for a single-file src (default: sibling '
                         '"<name> (COMPRESSED).pdf")')
    ap.add_argument('--dpi', type=int, default=200, help='render dpi (default 200; good speed/quality)')
    ap.add_argument('--workers', type=int, default=(os.cpu_count() or 4),
                    help='parallel worker processes (default: one per logical core, '
                         'or 4 if the core count cannot be detected)')
    ap.add_argument('--limit', type=int, default=0, help='process only first N files (test)')
    ap.add_argument('--no-despeckle', action='store_true', help='disable background speckle removal')
    ap.add_argument('--global-threshold', action='store_true',
                    help='use the legacy fixed global threshold instead of adaptive binarization '
                         '(adaptive = background-flatten + Sauvola, the default, is much better on '
                         'low-contrast/yellowed scans)')
    ap.add_argument('--sauvola-k', type=float, default=0.30,
                    help='adaptive threshold sensitivity (default 0.30; lower=bolder/thicker ink, '
                         'higher=thinner/cleaner). Ignored with --global-threshold')
    ap.add_argument('--threshold', type=int, default=125,
                    help='gray<T => ink for --global-threshold mode only (keep low, ~125)')
    ap.add_argument('--min-size', type=int, default=10, help='remove black blobs smaller than N px')
    ap.add_argument('--no-photo-clean', action='store_true',
                    help='disable paper-whitening + dark-edge cleanup on grayscale photo/mixed pages')
    ap.add_argument('--photo-descreen', type=float, default=0.6,
                    help='descreen grayscale photo pages: gaussian sigma (scaled to dpi) that merges '
                         'halftone dot grain into smooth tone — less dithering + smaller (0 = off; default 0.6)')
    ap.add_argument('--symbol', action='store_true',
                    help='shared-dictionary mode: smaller, but BLANK in Chrome/Edge (PDFium). '
                         'Only for Ghostscript/Acrobat viewing.')
    ap.add_argument('--no-ocr', action='store_true', help='skip the searchable OCR text layer')
    ap.add_argument('--language', default='eng', help='Tesseract OCR language(s), e.g. eng or eng+fra+spa+deu')
    ap.add_argument('--no-photo', action='store_true',
                    help='force all pages bitonal (skip photo detection; photos will look bad)')
    ap.add_argument('--photo-threshold', type=float, default=0.02,
                    help='page kept as image if this fraction of tiles are continuous-tone (default 0.02)')
    ap.add_argument('--photo-dpi', type=int, default=150,
                    help='downsample photo pages to this dpi (0 = keep full render dpi; default 150)')
    ap.add_argument('--jpeg-quality', type=int, default=60, help='JPEG quality for photo pages (default 60)')
    ap.add_argument('--min-savings', type=float, default=0.25,
                    help='keep the compressed file only if it is at least this fraction smaller than '
                         'the original; else keep the original and OCR only (default 0.25)')
    ap.add_argument('--ocr-only', action='store_true',
                    help='do not compress at all: copy each original and just add the OCR text layer '
                         '(skips files that already have text)')
    ap.add_argument('--no-precheck', action='store_true',
                    help='disable the sample pre-check that skips compression for files it would not shrink')
    ap.add_argument('--precheck-threshold', type=float, default=0.75,
                    help='skip full compression if a sample projects the result >= this fraction of the '
                         'original (default 0.75); the --min-savings fallback still guards the rest')
    ap.add_argument('--no-skip-born-digital', action='store_true',
                    help='disable the born-digital SAFETY check (which by default copies any pdf that '
                         'does not look like a scan straight to dest, untouched); with this flag EVERY '
                         'pdf is rasterised/compressed, including vector/text ones')
    ap.add_argument('--scan-fraction', type=float, default=0.5,
                    help='a pdf is treated as SCANNED (eligible for compression) only if at least this '
                         'fraction of sampled pages carry a full-page raster image; below this it is '
                         'considered born-digital and copied through untouched (default 0.5)')
    ap.add_argument('--log', type=Path, default=None,
                    help='path for the run report log (default: a timestamped file in the dest root)')
    ap.add_argument('--no-log', action='store_true', help='do not write a run report log')
    ap.add_argument('--dry-run', action='store_true',
                    help='preview only: classify each pdf (born-digital? scanned?) and project its '
                         'compressed size, report what WOULD happen + projected savings, write NOTHING')
    ap.add_argument('--timeout', type=int, default=7200,
                    help='max seconds for the page-render step and for the OCR step per file; a file '
                         'that exceeds it is marked FAILED and the batch continues (0 = no timeout; '
                         'default 7200 = 2h, generous enough for the largest manuals while still '
                         'rescuing a genuinely hung/corrupt pdf)')
    ap.add_argument('--no-verify-output', action='store_true',
                    help='skip the post-write check that each output opens and its page count matches '
                         'the source (the check flags silently-corrupt outputs in the log)')
    ap.add_argument('--no-repair', action='store_true',
                    help='do not attempt a Ghostscript pdfwrite repair on a malformed pdf before '
                         'giving up on it (repair is on by default)')
    ap.add_argument('--min-free-gb', type=float, default=1.0,
                    help='abort before starting if the destination drive has less than this many GB '
                         'free (default 1.0; 0 disables the check)')
    ap.add_argument('--no-duplicate-check', action='store_true',
                    help='disable the default duplicate flagging (which hashes each processed file and '
                         'notes in the report when two files are byte-identical). Files are ALWAYS '
                         'processed and get their own output — duplicates are only flagged, never '
                         'skipped (byte-identical files can legitimately belong to different manuals)')
    ap.add_argument('--retry-failed', type=Path, default=None, metavar='REPORT.csv',
                    help='reprocess ONLY the files marked FAILED in a previous run report .csv '
                         '(re-runs them even if a dest exists)')
    ap.add_argument('--config', type=Path, default=None,
                    help='TOML config file of default option values (CLI flags override it); if omitted, '
                         './ocrmyworkshopmanual.toml is loaded when present. Keys match long option '
                         'names with dashes as underscores, e.g. dpi = 300, no_ocr = true')
    _apply_config_defaults(ap)
    args = ap.parse_args()

    err = check_tools(want_ocr=not args.no_ocr)
    if err:
        print(f'ERROR: {err}', file=sys.stderr); sys.exit(1)

    src_root = args.src
    # SINGLE-FILE mode: src is one .pdf. rel_base is its folder so the report shows just
    # the filename; default output is a sibling "<name> (COMPRESSED).pdf".
    single_dest = None
    if src_root.is_file():
        if src_root.suffix.lower() != '.pdf':
            print(f'ERROR: not a PDF: {src_root}', file=sys.stderr); sys.exit(1)
        rel_base = src_root.parent
        if args.dest:
            single_dest = args.dest if args.dest.suffix.lower() == '.pdf' \
                else args.dest / src_root.name
        else:
            single_dest = src_root.with_name(f'{src_root.stem} (COMPRESSED){src_root.suffix}')
        dest_root = single_dest.parent
        pdfs = [src_root]
    elif src_root.is_dir():
        rel_base = src_root
        dest_root = args.dest or src_root.parent / (src_root.name + ' (COMPRESSED)')
        pdfs = sorted(p for p in src_root.rglob('*.pdf'))
    else:
        print(f'ERROR: source not found (need a folder or a .pdf file): {src_root}',
              file=sys.stderr); sys.exit(1)

    # disk-space guard: abort before doing work if the dest drive is nearly full
    if args.min_free_gb and not args.dry_run:
        try:
            probe = dest_root if dest_root.exists() else dest_root.parent
            free_gb = shutil.disk_usage(str(probe)).free / 1e9
            if free_gb < args.min_free_gb:
                print(f'ERROR: only {free_gb:.1f} GB free on the destination drive '
                      f'(< --min-free-gb {args.min_free_gb}); aborting before writing.',
                      file=sys.stderr)
                sys.exit(1)
        except Exception:
            pass

    if single_dest is not None:
        jobs = [] if single_dest.exists() else [(str(src_root), str(single_dest))]
        skipped = len(pdfs) - len(jobs)
    elif args.retry_failed:
        if not args.retry_failed.exists():
            print(f'ERROR: --retry-failed report not found: {args.retry_failed}',
                  file=sys.stderr); sys.exit(1)
        want = sorted(set(_read_failed_rels(args.retry_failed)))
        jobs = [(str(src_root / rel), str(dest_root / rel)) for rel in want
                if (src_root / rel).exists()]
        skipped = len(pdfs) - len(jobs)
        print(f'Retry-failed mode: {len(jobs)} previously-FAILED file(s) '
              f'from {args.retry_failed.name}')
    else:
        jobs = []
        for src in pdfs:
            dest = dest_root / src.relative_to(rel_base)
            if dest.exists():
                continue
            jobs.append((str(src), str(dest)))
        skipped = len(pdfs) - len(jobs)
    if args.limit:
        jobs = jobs[:args.limit]

    print(f'Ghostscript : {GS}')
    print(f'jbig2enc    : {JBIG}')
    print(f'Source      : {src_root}')
    print(f'Dest        : {dest_root}')
    ocr_desc = f'OCR({args.language})' if not args.no_ocr else 'no OCR'
    bd_desc = ('no born-digital check' if args.no_skip_born_digital
               else f'born-digital-safe (scan-frac>={args.scan_fraction:g})')
    if args.ocr_only:
        print(f'{len(pdfs)} PDFs found, {skipped} already done, {len(jobs)} to process '
              f'@ {args.workers} workers, OCR-ONLY (no compression), {ocr_desc}, {bd_desc}\n')
    else:
        photo_desc = 'no photo-detect' if (args.no_photo or args.symbol) else f'photo>{args.photo_threshold:g}@{args.photo_dpi}dpi'
        bin_desc = (f'global T={args.threshold}' if args.global_threshold
                    else f'adaptive(sauvola k={args.sauvola_k:g})')
        print(f'{len(pdfs)} PDFs found, {skipped} already done, {len(jobs)} to process '
              f'@ {args.dpi} dpi, {args.workers} workers, '
              f'{"symbol" if args.symbol else "generic"} mode, {bin_desc}, '
              f'{"despeckle" if not args.no_despeckle else "no despeckle"}, '
              f'{photo_desc}, {ocr_desc}, {bd_desc}\n')
    if not jobs:
        print('Nothing to do.'); return
    if args.dry_run:
        print('*** DRY-RUN: previewing only — nothing will be written. ***\n')

    set_below_normal_priority()
    t0 = time.time()
    done = fail = kept = 0
    tot_orig = tot_new = 0
    results = []  # accumulated for the run log

    # open the report CSV up front and flush a row per file, so progress is visible
    # live (the full .log + a final complete .csv are (re)written at the end).
    report_dir = src_root.parent if args.dry_run else None
    report_log_path = _report_path(args.log, dest_root, report_dir, t0, args.dry_run)
    csv_live = None
    if not args.no_log:
        try:
            report_log_path.parent.mkdir(parents=True, exist_ok=True)
            csv_live = open(report_log_path.with_suffix('.csv'), 'w', newline='', encoding='utf-8')
            csv_live.write(_csv_row(REPORT_COLUMNS))
            csv_live.flush()
        except Exception as ex:
            print(f'(could not open live CSV: {ex})', file=sys.stderr); csv_live = None

    with cf.ProcessPoolExecutor(max_workers=args.workers,
                                initializer=set_below_normal_priority) as ex:
        if args.dry_run:
            futs = {ex.submit(preview_one, s, args.dpi,
                              not args.no_despeckle, args.threshold, args.min_size, args.ocr_only,
                              not args.no_photo, args.photo_threshold, args.photo_dpi, args.jpeg_quality,
                              args.min_savings, not args.no_precheck, args.precheck_threshold,
                              not args.global_threshold, args.sauvola_k,
                              not args.no_photo_clean, args.photo_descreen,
                              not args.no_skip_born_digital, args.scan_fraction): (s, d)
                    for s, d in jobs}
        else:
            futs = {ex.submit(compress_one, s, d, args.dpi,
                              not args.no_despeckle, args.threshold, args.min_size, args.symbol,
                              not args.no_ocr, args.language,
                              not args.no_photo, args.photo_threshold, args.photo_dpi, args.jpeg_quality,
                              args.min_savings, args.ocr_only,
                              not args.no_precheck, args.precheck_threshold,
                              not args.global_threshold, args.sauvola_k,
                              not args.no_photo_clean, args.photo_descreen,
                              not args.no_skip_born_digital, args.scan_fraction,
                              args.timeout, not args.no_verify_output,
                              not args.no_repair): (s, d)
                    for s, d in jobs}
        N = len(jobs)
        # duplicate check is skipped in dry-run (a preview shouldn't hash every byte)
        dup_check = not args.no_duplicate_check and not args.dry_run
        seen_hash = {}   # content-hash -> first rel seen (for a live console marker)
        for i, fut in enumerate(cf.as_completed(futs), 1):
            s, d = futs[fut]
            res = fut.result()
            res['rel'] = os.path.relpath(s, str(rel_base))
            results.append(res)
            dmark, live_dup = '', ''
            if dup_check:
                try:
                    res['hash'] = _file_hash(Path(s))
                except Exception:
                    res['hash'] = None
                if res.get('hash'):
                    if res['hash'] in seen_hash:
                        live_dup = seen_hash[res['hash']]
                        res['duplicate_of'] = live_dup
                        dmark = f'  [dup of {live_dup}]'
                    else:
                        seen_hash[res['hash']] = res['rel']
            elapsed = time.time() - t0
            eta = (N - i) * (elapsed / i) if i < N and elapsed > 0 else 0
            eta_str = f'  [ETA {eta/60:.0f}m]' if eta >= 30 else ''
            if res['err']:
                fail += 1
                print(f'  [{i}/{N}] FAIL {res["src"]}: {res["err"]}{dmark}{eta_str}')
            else:
                done += 1
                if res.get('kept'):
                    kept += 1
                tot_orig += res['orig']; tot_new += res['new']
                pct = res['new'] * 100 // res['orig'] if res['orig'] else 0
                arrow = f'~{mb(res["new"]):.0f}' if args.dry_run else f'{mb(res["new"]):.0f}'
                print(f'  [{i}/{N}] {res["src"][:60]}  '
                      f'{mb(res["orig"]):.0f}->{arrow} MB ({pct}%){res.get("note", "")}{dmark}{eta_str}')
            if csv_live:   # flush a row per file for live progress
                csv_live.write(_csv_row(_report_row(res)))
                csv_live.flush()

    if csv_live:
        csv_live.close()
    dup_sets = _flag_duplicates(results) if dup_check else 0
    dt = time.time() - t0
    n_born = sum(1 for r in results if r.get('action') == 'born_digital')
    verb = 'Previewed' if args.dry_run else 'processed'
    print(f'\nDone in {dt/60:.1f} min. {verb} {done} ({done - kept} '
          f'{"would compress" if args.dry_run else "compressed"}, '
          f'{kept} kept-original/OCR-only incl. {n_born} born-digital), failed {fail}')
    if tot_orig:
        word = 'Projected' if args.dry_run else 'Total'
        saved = 'would save' if args.dry_run else 'saved'
        print(f'{word}: {mb(tot_orig):.0f} MB -> {mb(tot_new):.0f} MB '
              f'({tot_new*100//tot_orig}% ; {saved} {mb(tot_orig-tot_new):.0f} MB)')
    if dup_sets:
        n_dup_files = sum(1 for r in results if r.get('duplicate_of'))
        print(f'Duplicates: {n_dup_files} file(s) in {dup_sets} set(s) flagged '
              f'(byte-identical; all still processed — see report)')
    print('Output: (dry-run — nothing written)' if args.dry_run else f'Output: {dest_root}')

    if not args.no_log:
        settings = {
            'dpi': args.dpi, 'workers': args.workers, 'mode': 'symbol' if args.symbol else 'generic',
            'binarization': f'global T={args.threshold}' if args.global_threshold
                            else f'adaptive sauvola-k={args.sauvola_k:g}',
            'despeckle': not args.no_despeckle, 'min_size': args.min_size,
            'photo_detect': not (args.no_photo or args.symbol),
            'photo_threshold': args.photo_threshold, 'photo_dpi': args.photo_dpi,
            'jpeg_quality': args.jpeg_quality, 'photo_clean': not args.no_photo_clean,
            'photo_descreen': args.photo_descreen, 'ocr': ocr_desc, 'ocr_only': args.ocr_only,
            'min_savings': args.min_savings, 'precheck': not args.no_precheck,
            'precheck_threshold': args.precheck_threshold,
            'born_digital_check': not args.no_skip_born_digital, 'scan_fraction': args.scan_fraction,
            'timeout': args.timeout, 'verify_output': not args.no_verify_output,
            'repair': not args.no_repair, 'duplicate_check': not args.no_duplicate_check,
            'retry_failed': str(args.retry_failed) if args.retry_failed else False,
            'dry_run': args.dry_run,
        }
        try:
            # reuse the same path the live CSV was written to (report_dir/report_log_path
            # were computed before the run); write_run_log (re)writes the full .log + .csv
            log_path = write_run_log(report_log_path, dest_root, src_root, results, settings,
                                     t0, dt, len(pdfs), skipped, args.limit, fail, done, kept,
                                     dry_run=args.dry_run, report_dir=report_dir)
            print(f'Log: {log_path}  (+ .csv)')
        except Exception as ex:
            print(f'(could not write run log: {ex})', file=sys.stderr)


if __name__ == '__main__':
    main()
