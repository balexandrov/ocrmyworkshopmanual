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
(never rasterised) — always on, no flag to disable it. Every folder run also
writes a report log (which file, what was done, final stats); disable with --no-log.

Usage:
  python ocrmyworkshopmanual.py "M:\\path\\to\\folder"           # compress + OCR a tree
  python ocrmyworkshopmanual.py "one_manual.pdf"                # a single file -> sibling (COMPRESSED).pdf
  python ocrmyworkshopmanual.py SRC --in-place                   # OVERWRITE the source PDFs (back up first)
  python ocrmyworkshopmanual.py SRC --dry-run                    # preview only, write nothing
  python ocrmyworkshopmanual.py SRC --dest OUT --workers 10
  python ocrmyworkshopmanual.py SRC --limit 3                    # test first N files
  python ocrmyworkshopmanual.py SRC --no-ocr                     # compress only
  python ocrmyworkshopmanual.py SRC --ocr-only                   # add text layer only, no compression
  python ocrmyworkshopmanual.py SRC --language eng+fra+spa+deu   # multilingual OCR

Page-type router: classify_page() sorts each page into a PageType (PT_LINE/PT_BLANK
  bitonal, PT_PHOTO_GRAY, PT_PHOTO_COLOR) and the router dispatches it to that type's
  strategy; add a page kind by extending classify_page() + the router branch.

Tuning notes (learned on Toyota FSM scans):
  OCR (default on) adds a searchable text layer via ocrmypdf; --no-ocr to skip.
                   Needs Tesseract on PATH and ocrmypdf installed.
  GENERIC JBIG2 only: each page is a self-contained JBIG2 stream (no shared glyph
                   dictionary), so it renders everywhere — PDFium (Chrome/Edge)
                   renders a shared dictionary as BLANK pages, so that mode isn't offered.
  --dpi 200        good speed/quality balance for on-screen viewing (~native ~220).
  ADAPTIVE binarization (the only mode) = background-flatten + Sauvola: keeps faint
                   strokes and dotted leaders on low-contrast/yellowed scans and
                   resolves a gray shaded wash (foldout wiring diagrams) cleanly,
                   where a fixed global threshold either erodes ink or (high) makes
                   salt-and-pepper. --sauvola-k tunes boldness.
  photo pages      grayscale photo/mixed pages are always paper-whitened + edge-trimmed
                   (--photo-descreen 0 to skip only the halftone-smoothing blur);
                   colour detection is cast-robust so a sepia B&W page stays
                   whitened-grayscale, not a yellow colour JPEG.
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

__version__ = '0.1.0'

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
PT_COLOR_LINE = 'color_line'   # colour LINE ART (colour wiring diagrams / schematics): low
                               # continuous-tone coverage but real colour -> lossless source-
                               # page pass-through (NEVER binarize, so the colour survives)
PT_VECTOR = 'vector'           # born-digital / vector page (no full-page raster: TOC, nav,
                               # text) inside an otherwise-scanned PDF -> lossless source-page
                               # pass-through, so its text, colour and hyperlinks survive
_PT_BITONAL = (PT_BLANK, PT_LINE)      # types that share the grouped-JBIG2 path
_PT_PASSTHROUGH = (PT_COLOR_LINE, PT_VECTOR)  # types copied through from the source, untouched

# A page whose largest raster image is below this effective DPI has no full-page scan
# on it -> treat it as a born-digital/vector page and pass it through untouched.
VECTOR_DPI_FLOOR = 50
# Cap for auto-raising the render DPI to a scan's native resolution (avoid huge foldouts).
MAX_RENDER_DPI = 400

# DPI for the cheap colour PROBE that rescues colour line art from the bitonal path.
# Only low enough to let _is_color() see chroma; the page itself is passed through at
# full resolution (never re-rendered), so this never limits output quality.
COLOR_PROBE_DPI = 100

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


def _find_ocrmypdf_cmd():
    """The command prefix used to invoke ocrmypdf. Prefer the installed console
    script (`ocrmypdf` / `ocrmypdf.exe`): on some Windows setups `python -m ocrmypdf`
    hangs on import, while the console-script launcher runs fine. Fall back to
    `-m ocrmypdf` only if no script is found. Override with env JBIG2_OCRMYPDF."""
    env = os.environ.get('JBIG2_OCRMYPDF')
    if env:
        return [env]
    exe = shutil.which('ocrmypdf')
    if not exe:
        d = Path(sys.executable).parent
        for name in ('ocrmypdf.exe', 'ocrmypdf'):
            if (d / name).exists():
                exe = str(d / name)
                break
    return [exe] if exe else [PY, '-m', 'ocrmypdf']


OCRMYPDF = _find_ocrmypdf_cmd()

# make Tesseract discoverable even if PATH wasn't refreshed this session
for _d in (r'C:\Program Files\Tesseract-OCR', r'C:\Program Files (x86)\Tesseract-OCR'):
    if os.path.isdir(_d) and _d not in os.environ.get('PATH', ''):
        os.environ['PATH'] = _d + os.pathsep + os.environ.get('PATH', '')

TESS = shutil.which('tesseract')

# Tesseract OSD script name -> ocrmypdf language. Cyrillic docs often carry some
# English too, so pair rus+eng. Scripts without an installed pack fall back to eng.
_SCRIPT_LANG = {'Cyrillic': 'rus+eng', 'Latin': 'eng'}

# Minimum Tesseract OSD "Script confidence" to TRUST a page's script vote. OSD is
# unreliable on sparse text (a wiring-diagram page with few words), where it emits a
# low-confidence — often wrong — guess (measured: English diagrams mislabelled Cyrillic
# at conf 0.6-1.3, while genuine dense pages score ~15-20). Votes below this floor are
# ignored, so a Latin/English doc is no longer mislabelled rus+eng (slow, lower quality)
# by a couple of noisy sparse pages; a genuinely Russian manual still clears it easily.
MIN_OSD_SCRIPT_CONF = 3.0

_INSTALLED_LANGS = None


def _installed_langs() -> set:
    """Tesseract language packs actually installed (cached per process). `tesseract
    --list-langs` also prints script/ combos and a header line — keep only plain packs."""
    global _INSTALLED_LANGS
    if _INSTALLED_LANGS is None:
        langs: set = set()
        if TESS:
            try:
                r = subprocess.run([TESS, '--list-langs'], capture_output=True,
                                   text=True, timeout=30)
                for ln in (r.stdout or '').splitlines():
                    ln = ln.strip()
                    if ln and 'List of' not in ln and '/' not in ln and '\\' not in ln:
                        langs.add(ln)
            except Exception:
                pass
        _INSTALLED_LANGS = langs
    return _INSTALLED_LANGS


def _available_ocr_lang(lang: str) -> str:
    """Filter an OCR language spec (e.g. 'rus+eng') to packs that are actually installed;
    if none survive, fall back to 'eng' (or any installed pack). So a missing language
    pack NEVER silently drops the whole OCR text layer — it degrades to what Tesseract
    can actually run, instead of erroring the file out with no text at all."""
    inst = _installed_langs()
    if not inst:
        return lang                        # couldn't enumerate -> don't second-guess
    keep = [p for p in lang.split('+') if p in inst]
    if keep:
        return '+'.join(keep)
    return 'eng' if 'eng' in inst else sorted(inst)[0]


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


def _say(*a, **k):
    """print() that never aborts a long run if stdout is gone (the reader end of a
    pipe closed, terminal detached, etc.). A dropped progress line must not kill the
    batch — the per-file report CSV is the durable record, the console is just a view."""
    try:
        print(*a, **k)
    except (BrokenPipeError, OSError):
        pass


def _sweep_stale_scratch(max_age_h: float = 6.0) -> None:
    """Remove leftover render-scratch dirs (jb_* / jbprev_*) in the system temp dir left
    behind by a prior run whose worker was OS-KILLED (OOM, BrokenProcessPool, task kill) —
    a killed process skips its `finally` cleanup, so scratch can pile up to many GB. Only
    dirs untouched for `max_age_h` hours are removed, so a concurrently-running instance's
    ACTIVE scratch (files are being written, mtime stays fresh) is never disturbed."""
    tmp = Path(tempfile.gettempdir())
    cutoff = time.time() - max_age_h * 3600
    n = freed = 0
    for d in list(tmp.glob('jb_*')) + list(tmp.glob('jbprev_*')):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                freed += sum(f.stat().st_size for f in d.rglob('*') if f.is_file())
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except Exception:
            pass
    if n:
        _say(f'(swept {n} stale scratch dir(s) from earlier interrupted runs, '
             f'~{freed/1e9:.1f} GB reclaimed)')


def _ocrmypdf_ok():
    try:
        return subprocess.run(OCRMYPDF + ['--version'],
                              capture_output=True, timeout=120).returncode == 0
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


def _validate_numeric_args(args) -> str:
    """Return an error string if a numeric option is out of a sane range, else None.
    Catches typos/mistakes early with a clear message instead of a confusing failure
    deep in a subprocess (Ghostscript, jbig2enc, Tesseract) partway through a run."""
    checks = [
        (args.dpi > 0, '--dpi must be > 0'),
        (args.workers >= 1, '--workers must be >= 1'),
        (1 <= args.jpeg_quality <= 100, '--jpeg-quality must be between 1 and 100'),
        (args.sauvola_k > 0, '--sauvola-k must be > 0'),
        (args.min_size >= 0, '--min-size must be >= 0'),
        (0.0 <= args.photo_threshold <= 1.0, '--photo-threshold must be between 0 and 1'),
        (args.photo_dpi >= 0, '--photo-dpi must be >= 0 (0 = keep render dpi)'),
        (0.0 <= args.min_savings <= 1.0, '--min-savings must be between 0 and 1'),
        (args.photo_descreen >= 0, '--photo-descreen must be >= 0 (0 = off)'),
        (args.timeout >= 0, '--timeout must be >= 0 (0 = no timeout)'),
        (args.min_free_gb >= 0, '--min-free-gb must be >= 0 (0 = disabled)'),
        (args.limit >= 0, '--limit must be >= 0 (0 = no limit)'),
    ]
    bad = [msg for ok, msg in checks if not ok]
    return '; '.join(bad) if bad else None


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


def binarize_png(path: Path, min_size: int, despeckle: bool, dpi: int,
                 sauvola_k: float = 0.30, ink_floor: int = 100):
    """In place: turn a grayscale page PNG into a 1-bit PNG via background-flatten +
    Sauvola adaptive threshold, so low-contrast/yellowed scans keep their faint
    strokes and dotted leaders and a gray wash doesn't speckle. Then optionally drops
    black connected components smaller than min_size px (scan speckle). Window sizes
    scale with dpi. ink_floor: any flattened pixel darker than this is forced to ink —
    Sauvola alone HOLLOWS OUT solid-black interiors (bold display type, filled tabs)
    because a big uniform-dark area has ~no local variance, so this floor keeps
    blacks solid."""
    g = np.asarray(Image.open(path).convert('L'))
    flat_win = max(21, round(dpi * 0.30))  # ~background/paper scale
    sauv_win = max(15, round(dpi * 0.20))  # ~a few characters
    flat = _flatten_bg(g, flat_win)
    ink = _sauvola_ink(flat, sauv_win, sauvola_k)
    ink |= flat < ink_floor                # keep solid-black fills solid
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
    chroma = (mx - mn)[marks]
    # (a) A SMALL amount of strongly saturated ink still means "colour-coded": a wiring
    # diagram that is mostly a black text table but has a few vivid red/blue/yellow wires
    # carries its meaning in those wires. Judging by SHARE of ink alone missed exactly
    # that case (a real Chrysler diagram: 5.5% coloured -> binarized, colour destroyed).
    # Measured separation is wide: non-colour scans reach 0.0019 / 66 px of chroma>60,
    # while every genuinely colour page starts at 0.053 / 2875 px.
    strong = chroma > 60
    if int(strong.sum()) >= 500 and float(strong.mean()) > 0.01:
        return True
    # (b) broadly colourful page (pastel/faded colour spread over much of the ink)
    return float((chroma > 45).mean()) > 0.06


def _render_color(src_p: Path, page_no: int, out_png: Path, dpi: int) -> bool:
    """Render one page to a full-colour (png16m / RGB) PNG at `dpi`. True on success."""
    subprocess.run([GS, '-sDEVICE=png16m', f'-r{dpi}', f'-dFirstPage={page_no}',
                    f'-dLastPage={page_no}', '-dNOPAUSE', '-dBATCH', '-dQUIET',
                    '-sOutputFile=' + str(out_png), win_long(src_p)], capture_output=True)
    return out_png.exists()


_GRAY_CS = {'/DeviceGray', '/CalGray'}  # colour spaces that can NEVER carry colour


def _page_color_capable(page) -> bool:
    """Cheap METADATA pre-filter (no pixels): COULD this page contain colour?

    Walks the page's image XObjects (recursing into Form XObjects) and inspects each
    image's /ColorSpace. Returns True as soon as ANY image is colour-capable
    (DeviceRGB/CMYK/Lab, Indexed/Separation/DeviceN, or ICCBased with N>=3), so a big
    gray scan with a small colour inset still gets probed. 'When unsure, probe': also
    True on a parse error, when `page` is None, or when no image is found — because a
    wrong 'gray' verdict silently destroys colour (the exact bug this guards), whereas
    a wrong 'colour' verdict only costs one cheap probe render. Returns False only when
    image(s) were found and ALL resolve as confidently gray / 1-bit — the common
    grayscale scan, which then keeps the fast bitonal path with no colour render."""
    if page is None:
        return True

    def cs_is_color(cs) -> bool:
        try:
            cs = cs.get_object() if hasattr(cs, 'get_object') else cs
        except Exception:
            return True
        if isinstance(cs, (list, tuple)) and cs:
            head = str(cs[0])
            if head == '/ICCBased':
                try:
                    return int(cs[1].get_object().get('/N', 3)) >= 3
                except Exception:
                    return True
            if head in ('/Indexed', '/Separation', '/DeviceN'):
                return True   # palette / separation may carry colour -> let _is_color arbitrate
            return head not in _GRAY_CS
        return str(cs) not in _GRAY_CS

    found = [False]
    color = [False]

    def walk(res, depth=0):
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
                    found[0] = True
                    if obj.get('/ImageMask'):
                        continue          # 1-bit stencil -> not colour
                    if cs_is_color(obj.get('/ColorSpace')):
                        color[0] = True
                elif sub == '/Form':
                    walk(obj.get('/Resources'), depth + 1)
        except Exception:
            color[0] = True               # unsure -> probe

    try:
        walk(page.get('/Resources'))
    except Exception:
        return True
    return color[0] or not found[0]


def _page_has_color(work: Path, src_p: Path, page_no: int, dpi: int) -> bool:
    """Authoritative COLOUR test for a low-coverage line-art page: render it in colour
    at a low DPI via Ghostscript and run the cast-robust _is_color() test. We render
    (not extract via pypdf) deliberately — pypdf silently MIS-decodes Indexed/palette
    images (drops the lookup table -> a gray-looking fallback -> false 'not colour',
    which would re-binarize the very colour diagrams this guards); Ghostscript applies
    the palette/ICC correctly. Gated by the cheap _page_color_capable() pre-filter, so
    plain DeviceGray scans never reach here."""
    cpng = work / f'cl{page_no}.png'
    got = _render_color(src_p, page_no, cpng, min(dpi, COLOR_PROBE_DPI))
    res = _is_color(np.asarray(Image.open(cpng).convert('RGB')).astype(np.int16)) if got else False
    cpng.unlink(missing_ok=True)
    return res


def classify_page(png: Path, page_no: int, src_p: Path, work: Path, dpi: int,
                  detect_photos: bool, photo_thresh: float, photo_dpi: int,
                  blank_ink: float = 0.0008, page=None) -> PageClass:
    """Route one rendered grayscale page to a PageType. Cheap signals: ink fraction
    (BLANK), tiled continuous-tone coverage (PHOTO vs LINE), a colour render + cast-
    robust colour test for photo pages (PHOTO_GRAY vs PHOTO_COLOR), and — critically —
    a colour probe on LOW-coverage line art so a colour wiring diagram / schematic is
    routed to PT_COLOR_LINE (lossless pass-through) instead of being binarized to b&w.
    The photo colour PNG is rendered once and handed to the strategy via color_png.

    `page` is the source PdfReader page (for the cheap colourspace pre-filter); callers
    that classify many pages should pass it (one reader) — else it is loaded on demand.

    A page is only BLANK when it has neither ink NOR continuous-tone coverage: bright
    colour pages (an orange/pastel cover) convert to a grayscale luminance that is all
    >= 100, so the ink test alone would call them blank and destroy them as bitonal —
    the coverage guard keeps them on the photo/colour path."""
    if page is None:
        try:
            page = PdfReader(str(src_p)).pages[page_no - 1]
        except Exception:
            page = None

    # BORN-DIGITAL / VECTOR page (TOC, nav, text — no full-page raster) inside an
    # otherwise-scanned PDF: never rasterize it. Pass it through untouched so its vector
    # text, colour and hyperlinks survive (rasterizing to b&w destroys all three).
    if page is not None:
        try:
            if _largest_image_dpi(page) < VECTOR_DPI_FLOOR:
                return PageClass(PT_VECTOR, None)
        except Exception:
            pass

    g = np.asarray(Image.open(png).convert('L'))
    cov = photo_coverage(png, dpi) if detect_photos else 0.0

    # continuous-tone page: render colour once, decide gray vs colour
    if detect_photos and cov > photo_thresh:
        d = photo_dpi or dpi
        cpng = work / f'color{page_no}.png'
        if _render_color(src_p, page_no, cpng, d):
            a = np.asarray(Image.open(cpng).convert('RGB')).astype(np.int16)
            return PageClass(PT_PHOTO_COLOR if _is_color(a) else PT_PHOTO_GRAY, cpng)
        # colour render failed -> fall through to the bitonal decision below

    # LOW-coverage LINE ART: rescue genuine COLOUR (colour wiring diagrams / schematics)
    # before it is binarized. The metadata pre-filter skips the probe for plain
    # grayscale scans (the common case), so this adds no render there.
    if detect_photos and _page_color_capable(page) and _page_has_color(work, src_p, page_no, dpi):
        return PageClass(PT_COLOR_LINE, None)

    if float((g < 100).mean()) < blank_ink and cov <= photo_thresh:
        return PageClass(PT_BLANK, None)
    return PageClass(PT_LINE, None)


def photo_seg_pdf(pc: PageClass, out_pdf: Path, work: Path, page_no: int,
                  d: int, quality: int, descreen: float = 0.6):
    """Strategy for PHOTO_GRAY / PHOTO_COLOR: JPEG the pre-rendered colour page and wrap
    it to a 1-page PDF sized (via embedded dpi) to match the bitonal pages. Colour pages
    are kept as-is; grayscale (B&W photo/mixed/stipple) pages get descreen + paper-whitening
    + dark scan-edge cleanup (skipped on a full-bleed photo with little paper) via _clean_paper."""
    im = Image.open(pc.color_png).convert('RGB')
    if pc.type == PT_PHOTO_COLOR:
        out_im = im
    else:
        g = np.asarray(im.convert('L'))
        if float((g > 200).mean()) > 0.10:  # paper present -> document page, not full-bleed
            g = _clean_paper(g, d, descreen)
        out_im = Image.fromarray(g)
    jpg = work / f'photo{page_no}.jpg'
    out_im.save(jpg, 'JPEG', quality=quality, dpi=(d, d))
    pc.color_png.unlink(missing_ok=True)
    with open(out_pdf, 'wb') as f:
        f.write(img2pdf.convert(str(jpg)))


def _color_line_seg(src_pdf: Path, page_no: int, out_pdf: Path) -> None:
    """Strategy for PT_COLOR_LINE: copy the ORIGINAL page through LOSSLESSLY (no
    re-encode) so a colour wiring diagram keeps its exact colours and crisp lines at a
    tiny size — the whole point of not binarizing it. Falls back to a re-rendered,
    palette-quantized PNG only if the lossless page copy fails (near-never)."""
    try:
        r = PdfReader(str(src_pdf))
        w = PdfWriter()
        w.add_page(r.pages[page_no - 1])
        with open(out_pdf, 'wb') as f:
            w.write(f)
        if out_pdf.stat().st_size > 0:
            return
    except Exception:
        pass
    # fallback: re-render colour, quantize to a small lossless indexed PNG, wrap to PDF
    tmp = out_pdf.with_name(out_pdf.stem + '_clr.png')
    if _render_color(src_pdf, page_no, tmp, 200):
        try:
            Image.open(tmp).convert('RGB').quantize(colors=64).save(tmp, 'PNG', dpi=(200, 200))
        except Exception:
            pass
        with open(out_pdf, 'wb') as f:
            f.write(img2pdf.convert(str(tmp), dpi=200))
    tmp.unlink(missing_ok=True)


def _graft_into_source(src_pdf: Path, comp_path: Path) -> bool:
    """Put the COMPRESSED page content back into the ORIGINAL document, instead of
    shipping a freshly-built PDF that carries only pages.

    Rebuilding a PDF from rendered pages silently drops everything that is not page
    content: link annotations (internal, URI and cross-file /GoToR), bookmarks, named
    destinations, and document metadata. Re-attaching each of those one at a time is a
    losing game — measured on one 36-page manual, the rebuild kept 5 of 249 links, and a
    Lexus file lost all 248 bookmarks because their destinations point at other files and
    so resolve to no page number.

    So: open the source, swap each page's /Contents and /Resources for the compressed
    ones, and save. Everything else is inherited by construction. qpdf drops the now
    unreferenced original images on write (object streams), so the size stays close to
    the rebuilt file. Requires an exact page-count match; returns False (leaving
    comp_path untouched) on any problem, so the caller just ships the rebuild."""
    try:
        import pikepdf
    except Exception:
        return False
    tmp = comp_path.with_name(comp_path.stem + '_graft.pdf')
    try:
        with pikepdf.open(str(src_pdf)) as s, pikepdf.open(str(comp_path)) as c:
            if len(s.pages) != len(c.pages) or not len(s.pages):
                return False
            for sp, cp in zip(s.pages, c.pages):
                fp = s.copy_foreign(cp.obj)      # bring the compressed page across
                sp.Contents = fp.Contents
                sp.Resources = fp.Resources
                for k in ('/MediaBox', '/Rotate'):
                    if k in fp.keys():
                        sp[k] = fp[k]
            s.remove_unreferenced_resources()
            s.save(str(tmp), recompress_flate=True,
                   object_stream_mode=pikepdf.ObjectStreamMode.generate)
        if tmp.stat().st_size == 0:
            raise RuntimeError('empty graft')
        os.replace(str(tmp), str(comp_path))
        return True
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def has_text(pdf: Path, sample: int = 8, min_chars: int = 40,
             covered: float = 0.8) -> bool:
    """True if the PDF ALREADY has a text layer on (essentially) every page, i.e. OCR
    would add nothing. Judged PER PAGE over pages sampled across the WHOLE file.

    Deliberately not a total-chars-over-the-first-few-pages test: on a MIXED manual
    (a vector TOC/nav page plus scanned content) one text-rich page would blow past a
    global threshold and suppress OCR for every scanned page, leaving the actual content
    unsearchable. Requiring most pages to carry text means a partly-scanned file still
    goes to ocrmypdf, whose --skip-text adds a layer only to the pages missing one."""
    try:
        r = PdfReader(str(pdf))
        n = len(r.pages)
        if n == 0:
            return False
        k = min(sample, n)
        idxs = sorted({round(i * (n - 1) / max(1, k - 1)) for i in range(k)})
        with_text = 0
        for i in idxs:
            try:
                if len((r.pages[i].extract_text() or '').strip()) >= min_chars:
                    with_text += 1
            except Exception:
                pass
        return with_text >= covered * len(idxs)
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


def _page_render_dpi(page, base_dpi: int, cap: int = MAX_RENDER_DPI) -> int:
    """Per-page render DPI: use the page's native scan resolution when it EXCEEDS the
    base (so a high-res scan isn't downsampled), capped to avoid a foldout blow-up; never
    below base (don't upsample a low-res page and bloat it). Returns base if unknown."""
    try:
        native = _largest_image_dpi(page)
    except Exception:
        return base_dpi
    if native > base_dpi:
        return int(min(round(native), cap))
    return base_dpi


def _render_page_gray(src_p: Path, page_no: int, out_png: Path, dpi: int, timeout: int = 0) -> bool:
    """Render a single page to a grayscale PNG at `dpi` (used to re-render one high-res
    bitonal page at its native resolution). Returns True on success."""
    try:
        subprocess.run([GS, '-sDEVICE=pnggray', f'-r{dpi}', f'-dFirstPage={page_no}',
                        f'-dLastPage={page_no}', '-dNOPAUSE', '-dBATCH', '-dQUIET',
                        '-sOutputFile=' + str(out_png), win_long(src_p)],
                       capture_output=True, timeout=timeout or None)
    except Exception:
        return False
    return out_png.exists()


def _run_stalled(cmd, progress, stall: int, poll: float = 2.0, **kw):
    """Run `cmd`, killing it ONLY if it stops making progress — never merely for taking
    a long time. `progress()` returns a number that grows as work is done (pages
    rendered, output bytes written); if it hasn't grown for `stall` seconds the process
    is considered hung and killed (raising subprocess.TimeoutExpired).

    A plain wall-clock budget cannot tell 'slow' from 'stuck': it kills healthy work on
    big files purely for being big (a 6,855-page manual was failed at 2h while working
    correctly). Progress-based detection is size-independent — a 3-page and a 7,000-page
    file are judged identically. stall<=0 disables the watchdog entirely."""
    if stall <= 0:
        return subprocess.run(cmd, **kw)
    kw.pop('timeout', None)
    p = subprocess.Popen(cmd, stdout=kw.pop('stdout', subprocess.PIPE),
                         stderr=kw.pop('stderr', subprocess.PIPE), **kw)
    last, last_t = progress(), time.time()
    while True:
        try:
            out, err = p.communicate(timeout=poll)
            return subprocess.CompletedProcess(cmd, p.returncode, out, err)
        except subprocess.TimeoutExpired:
            pass
        now = progress()
        if now > last:
            last, last_t = now, time.time()
        elif time.time() - last_t >= stall:
            p.kill()
            try:
                p.communicate(timeout=30)
            except Exception:
                pass
            raise subprocess.TimeoutExpired(cmd, stall)


def _run_retry(fn, attempts: int = 3, backoff: float = 2.0):
    """Call fn() and retry a CRASH (an exception or a non-zero return code) a few times —
    transient native-library crashes and file locks usually succeed on a retry. A STALL
    (TimeoutExpired) is never retried: a hung or genuinely slow step behaves the same way
    the second time, so retrying only burns the time again. Returns (result, tries)."""
    last = None
    for i in range(1, attempts + 1):
        try:
            r = fn()
            if getattr(r, 'returncode', 0) == 0:
                return r, i
            last = r
        except subprocess.TimeoutExpired:
            raise                          # stalled: retrying would just stall again
        except Exception:
            if i == attempts:
                raise
        if i < attempts:
            time.sleep(backoff * i)
    return last, attempts


def _repair_pdf(src_p: Path, work: Path, expect_pages: int = 0, timeout: int = 0):
    """Try to repair a malformed/corrupt PDF. qpdf FIRST, Ghostscript second.

    They fail differently and qpdf is far better at this: on a real corrupt manual
    (garbage bytes inside the content streams, page tree intact) Ghostscript's pdfwrite
    salvaged 1 of 21 pages while qpdf recovered all 21 perfectly. Preferring GS therefore
    turned a fully recoverable file into a lost one. A repair that returns FEWER pages
    than the source is rejected outright — a truncated 'repair' is worse than none.
    Returns the repaired Path, else None."""
    for name, fn in (('qpdf', _qpdf_repair), ('gs', _gs_repair)):
        try:
            out = fn(src_p, work, timeout)
        except Exception:
            out = None
        if not out or not out.exists() or out.stat().st_size == 0:
            continue
        if expect_pages:
            try:
                if len(PdfReader(str(out)).pages) < expect_pages:
                    continue                     # partial salvage -> keep looking
            except Exception:
                continue
        return out
    return None


def _renders_ok(pdf: Path, timeout: int = 0) -> bool:
    """Can this PDF actually be rendered? Cheap one-page Ghostscript probe. Used on the
    paths that copy the source through byte-for-byte, so a corrupt file is caught and
    repaired instead of being faithfully reproduced as an unreadable copy."""
    out = pdf.parent / f'_probe_{pdf.stem}.png'
    try:
        ok = _render_page_gray(pdf, 1, out, 36, timeout) and out.stat().st_size > 0
    except Exception:
        ok = False
    finally:
        out.unlink(missing_ok=True)
    return ok


def _qpdf_repair(src_p: Path, work: Path, timeout: int = 0):
    """Rewrite a damaged PDF through qpdf (via pikepdf), which reconstructs broken
    xref/trailer structure while keeping every page it can parse."""
    out = work / 'repaired_qpdf.pdf'
    try:
        import pikepdf
        with pikepdf.open(str(src_p)) as p:
            p.save(str(out))
    except Exception:
        return None
    return out if out.exists() and out.stat().st_size > 0 else None


def _gs_repair(src_p: Path, work: Path, timeout: int = 0):
    """Try to repair a malformed/corrupt PDF by rewriting it through Ghostscript's
    pdfwrite device (which tolerates and reconstructs a lot of broken structure).
    Returns the repaired Path on success, else None. Used as a fallback before a
    file is given up on — one bad download shouldn't just be lost in a big batch."""
    out = work / 'repaired.pdf'
    try:
        # progress = the rewritten PDF growing; killed only if it stalls, not if it's slow
        r = _run_stalled(
            [GS, '-o', str(out), '-sDEVICE=pdfwrite', '-dQUIET', '-dNOPAUSE', '-dBATCH',
             win_long(src_p)],
            lambda: out.stat().st_size if out.exists() else 0, timeout, text=True)
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


def _detect_language(pdf: Path, work: Path, timeout: int = 0) -> str:
    """Guess the OCR language from page CONTENT (not filenames) via Tesseract OSD
    script detection — which reads a rendered image and reports its writing system
    without needing a text layer first, so it works on image-only scans. Renders a
    few evenly-spaced sample pages, tallies the dominant script (confidence-weighted)
    and maps it to a language. Falls back to 'eng' if detection is unavailable/unsure."""
    if not TESS or not GS:
        return 'eng'
    try:
        n = max(len(PdfReader(str(pdf)).pages), 1)
    except Exception:
        n = 1
    k = min(4, n)
    idxs = sorted({1 + round(i * (n - 1) / max(1, k - 1)) for i in range(k)})
    scores: dict = {}
    for pageno in idxs:
        png = work / f'osd_{pageno}.png'
        try:
            subprocess.run([GS, '-sDEVICE=pnggray', '-r200', '-dNOPAUSE', '-dBATCH',
                            '-dQUIET', f'-dFirstPage={pageno}', f'-dLastPage={pageno}',
                            '-sOutputFile=' + str(png), win_long(pdf)],
                           capture_output=True, timeout=timeout or 120)
            if not png.exists():
                continue
            r = subprocess.run([TESS, str(png), 'stdout', '--psm', '0'],
                               capture_output=True, text=True, timeout=timeout or 120)
        except Exception:
            continue
        script, conf = None, 0.0
        for line in (r.stdout or '').splitlines():
            s = line.strip()
            if s.startswith('Script:'):
                script = s.split(':', 1)[1].strip()
            elif s.startswith('Script confidence:'):
                try:
                    conf = float(s.split(':', 1)[1].strip())
                except ValueError:
                    conf = 0.0
        # Only trust a CONFIDENT script vote — sparse pages emit low-confidence noise
        # (often a spurious 'Cyrillic') that otherwise accumulates into a wrong language.
        if script and conf >= MIN_OSD_SCRIPT_CONF:
            scores[script] = scores.get(script, 0.0) + conf
    if not scores:
        return 'eng'                       # no confident page -> safe default
    return _available_ocr_lang(_SCRIPT_LANG.get(max(scores, key=scores.get), 'eng'))


def _ocr_and_place(base: Path, dest_p: Path, src_p: Path, orig: int, work: Path,
                   ocr: bool, language: str, pages: int, kept: bool, note: str,
                   timeout: int = 0, in_place: bool = False) -> dict:
    """Add an OCR text layer to `base` (only if it has none), then atomically place
    it at dest. Shared by the compress path and the --ocr-only path. `timeout` (secs,
    0=off) bounds the OCR step. The OUTPUT is always re-opened and its page count
    checked BEFORE placing it. With `in_place` (dest_p == src_p): if the result is
    identical to the source (kept original + no OCR added) the file is left untouched;
    and a failed verify keeps the original rather than overwriting it with a bad file."""
    final = base
    ocr_added = False
    if ocr:
        if has_text(base):
            note += ' (had text, OCR skipped)'
        else:
            if language == 'auto':
                language = _detect_language(base, work, timeout)
            # filter to installed packs so a missing pack degrades to eng/available
            # rather than erroring OCR out and leaving the file with no text at all.
            language = _available_ocr_lang(language)
            note += f' (lang:{language})'
            ocr_pdf = work / 'ocr.pdf'
            # NO timeout on OCR: ocrmypdf emits no usable progress signal (measured: it is
            # silent for ~90% of a run), so any wall-clock bound would just kill healthy
            # work on big files — which is exactly how a 6,855-page manual "failed" while
            # OCR'ing correctly. A crash is retried; slowness is simply waited out.
            r, tries = _run_retry(lambda: subprocess.run(
                OCRMYPDF + ['--language', language, '--optimize', '0',
                            '--output-type', 'pdf', '--skip-text', '--quiet', '--jobs', '1',
                            str(base), str(ocr_pdf)], capture_output=True, text=True))
            if r is not None and r.returncode == 0 and ocr_pdf.exists() and ocr_pdf.stat().st_size > 0:
                final = ocr_pdf
                ocr_added = True
                if tries > 1:
                    note += f' (OCR retried x{tries - 1})'
            else:
                note += ' (OCR FAILED)'
    # in-place: nothing changed (kept original, no OCR added) -> leave the file untouched
    if in_place and kept and not ocr_added:
        return {'src': src_p.name, 'orig': orig, 'new': orig, 'pages': pages,
                'note': note + ' (unchanged; left in place)', 'kept': True, 'err': None}
    # verify the output BEFORE overwriting anything
    warn = _verify_output(final, pages)
    if warn and in_place:   # never overwrite the original with a bad file
        return {'src': src_p.name, 'orig': orig, 'new': orig, 'pages': pages,
                'note': note, 'kept': True, 'err': 'output failed verify, original kept' + warn}
    note += warn
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = dest_p.with_suffix(dest_p.suffix + '.part')
    try:
        shutil.copyfile(str(final), str(tmp_out))
        os.replace(str(tmp_out), str(dest_p))
    except Exception:
        tmp_out.unlink(missing_ok=True)   # don't leave a stray .part on a failed swap
        raise
    return {'src': src_p.name, 'orig': orig, 'new': dest_p.stat().st_size,
            'pages': pages, 'note': note, 'kept': kept, 'err': None}


def sample_projection(src_p: Path, work: Path, dpi: int, despeckle: bool,
                      min_size: int, photo_thresh: float, photo_dpi: int, jpeg_quality: int,
                      sauvola_k: float = 0.30,
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
            binarize_png(png, min_size, despeckle, dpi, sauvola_k)
            r = subprocess.run([JBIG, '-p', '-a', '-D', str(dpi), png.name], cwd=sub, capture_output=True)
            comp += len(r.stdout)
        elif pc.type in _PT_PASSTHROUGH:
            # colour line art / vector page is passed through losslessly -> charge ~its
            # original page size, so the precheck doesn't expect JBIG2-tiny output.
            clseg = sub / f'cl{p}.pdf'
            _color_line_seg(src_p, p, clseg)
            comp += clseg.stat().st_size if clseg.exists() else 0
        else:
            d = photo_dpi or dpi
            photo_seg_pdf(pc, sub / f'seg{p}.pdf', sub, p, d, jpeg_quality, photo_descreen)
            # photo_seg_pdf writes photo{p}.jpg then a tiny PDF wrapper; size ~ the JPEG
            comp += (sub / f'photo{p}.jpg').stat().st_size
    shutil.rmtree(sub, ignore_errors=True)
    return ((comp / got) * n / orig) if got else 0.0


# ── One file (runs in a worker process) ──────────────────────────────────────

# If a cheap sample projects the compressed size at >= this fraction of the original,
# full compression is skipped and the original is kept (just OCR'd) — not worth the work.
PRECHECK_SKIP_RATIO = 0.75


def compress_one(src: str, dest: str, dpi: int,
                 despeckle: bool = True, min_size: int = 10,
                 ocr: bool = True, language: str = 'eng',
                 photo_thresh: float = 0.02,
                 photo_dpi: int = 150, jpeg_quality: int = 60,
                 min_savings: float = 0.25, ocr_only: bool = False,
                 sauvola_k: float = 0.30, photo_descreen: float = 0.6,
                 timeout: int = 0, in_place: bool = False) -> dict:
    """Render -> classify each page into a PageType -> per-type strategy -> merge -> OCR.

    PAGE-TYPE ROUTER: classify_page() sorts each page into LINE/BLANK (bitonal),
    PHOTO_GRAY or PHOTO_COLOR; consecutive bitonal pages are grouped into tiny generic
    JBIG2 (self-contained, so it renders in Chrome/Edge), photo pages become one JPEG
    each, all merged back in order. Binarization is background-flatten + Sauvola
    adaptive threshold, so faint strokes/leaders on low-contrast yellowed scans survive
    and gray washes resolve cleanly instead of speckling. Grayscale photo/mixed pages
    always get their paper whitened and dark scan edges trimmed (skipped automatically
    on a full-bleed photo with little visible paper). Colour detection is
    cast-robust, so a sepia B&W page is kept as (whitened) grayscale rather than a
    yellow colour JPEG. Add a page kind by extending classify_page() + the router
    branch (see the PageType constants).
    -D <dpi> embeds resolution so pages are sized correctly. With ocr=True,
    ocrmypdf adds an invisible text layer at the end (--optimize 0, images intact).
    """
    src_p, dest_p = Path(src), Path(dest)
    orig = src_p.stat().st_size
    work = Path(tempfile.mkdtemp(prefix='jb_'))
    try:
        # How many pages the SOURCE has. Everything downstream is verified against this,
        # never against the rendered count: on a corrupt PDF, rendering (or the repair
        # fallback) can silently yield fewer pages, and verifying the output against that
        # same reduced count happily passes — which is how a 21-page manual was replaced
        # by a 1-page file. 0 = unknown (unreadable source); then we can't cross-check.
        try:
            src_pages = len(PdfReader(str(src_p)).pages)
        except Exception:
            src_pages = 0
        # SAFETY: never rasterise a born-digital (vector/text) PDF. If the file does
        # not look like a scan, copy it through to dest byte-for-byte, untouched
        # (no render, no binarize, no OCR) — this tool is for scanned/image PDFs only.
        # Always on: a real archive is either scans or born-digital, and the default
        # scan-fraction cleanly separates the two (see looks_born_digital), so this is
        # not something a run should ever need to disable.
        born, bsig = looks_born_digital(src_p)
        if born:
            if not in_place:   # in-place: leave the original vector PDF exactly as-is
                dest_p.parent.mkdir(parents=True, exist_ok=True)
                tmp_out = dest_p.with_suffix(dest_p.suffix + '.part')
                try:
                    shutil.copyfile(str(src_p), str(tmp_out))
                    os.replace(str(tmp_out), str(dest_p))
                except Exception:
                    tmp_out.unlink(missing_ok=True)
                    raise
            where = 'left untouched' if in_place else 'copied untouched'
            return {'src': src_p.name, 'orig': orig,
                    'new': orig if in_place else dest_p.stat().st_size,
                    'pages': bsig.get('sampled'), 'kept': True, 'err': None,
                    'action': 'born_digital', 'signals': bsig,
                    'note': f' (born-digital: {where}; scan_frac={bsig.get("scan_frac")})'}
        note0 = ' (OCR-only, not compressed)' if ocr_only else ''
        # cheap pre-check: sample-compress a few pages; if it won't beat the original,
        # skip full compression and just OCR the original (avoids wasted work + growth).
        if not ocr_only:
            proj = sample_projection(src_p, work, dpi, despeckle, min_size,
                                     photo_thresh, photo_dpi, jpeg_quality,
                                     sauvola_k, photo_descreen)
            if proj >= PRECHECK_SKIP_RATIO:
                ocr_only = True
                note0 = f' (compression skipped: sample projected {proj*100:.0f}% of original)'
        if ocr_only:
            # No (worthwhile) compression: keep the original images, just add the OCR layer.
            base = work / 'orig.pdf'
            shutil.copyfile(str(src_p), str(base))
            # ...but never pass a BROKEN file straight through: this path copies bytes, so
            # a corrupt PDF would be reproduced corrupt (it renders nothing). If it cannot
            # render, repair it first — the repaired copy is what gets OCR'd and placed.
            if not _renders_ok(base, timeout):
                fixed = _repair_pdf(base, work, src_pages, timeout)
                if fixed and _renders_ok(fixed, timeout):
                    base = fixed
                    note0 += ' (repaired malformed PDF)'
                else:
                    return {'src': src_p.name, 'orig': orig, 'new': 0,
                            'err': 'unreadable PDF: renders no pages and repair failed '
                                   '— original kept'}
            res = _ocr_and_place(base, dest_p, src_p, orig, work, ocr, language,
                                 src_pages or len(PdfReader(str(base)).pages), True, note0,
                                 timeout, in_place)
            res['action'] = 'ocr_only'
            return res
        # 1) render pages to grayscale PNG (batched at the base DPI). High-resolution
        #    bitonal pages are RE-rendered per page at their native DPI further below, so
        #    a 300/400-dpi scan is not downsampled to a fixed 200 (a visible quality loss)
        #    while the common low-res pages don't bloat the file by being upsampled.
        render_src = src_p
        did_repair = False
        n_retry = [0]          # transient external-tool crashes that a retry recovered

        def _render():
            # Ghostscript writes p0001.png, p0002.png ... as it goes, so the page count is
            # a true progress signal: the run is killed only if it STALLS (no new page for
            # `timeout` secs), never for merely taking long on a big file.
            cmd = [GS, '-sDEVICE=pnggray', f'-r{dpi}', '-dNOPAUSE', '-dBATCH', '-dQUIET',
                   '-sOutputFile=' + str(work / 'p%04d.png'), win_long(render_src)]
            rr, tries = _run_retry(lambda: _run_stalled(
                cmd, lambda: len(list(work.glob('p*.png'))), timeout, text=True))
            if tries > 1:
                n_retry[0] += tries - 1
            return rr

        r = _render()
        pngs = sorted(p.name for p in work.glob('p*.png'))
        if r.returncode != 0 or not pngs:
            # malformed PDF? try a Ghostscript pdfwrite rewrite, then render the repaired copy
            fixed = _repair_pdf(src_p, work, src_pages, timeout)
            if fixed:
                render_src, did_repair = fixed, True
                for old in work.glob('p*.png'):
                    old.unlink(missing_ok=True)
                r = _render()
                pngs = sorted(p.name for p in work.glob('p*.png'))
        if r.returncode != 0 or not pngs:
            return {'src': src_p.name, 'orig': orig, 'new': 0,
                    'err': f'render failed rc={r.returncode} {r.stderr[:200]}'}
        # A render that produced FEWER pages than the source is page loss, not success —
        # typically a corrupt PDF whose repair salvaged only part of it. Fail the file and
        # keep the original rather than silently shipping a truncated manual.
        if src_pages and len(pngs) != src_pages:
            return {'src': src_p.name, 'orig': orig, 'new': 0,
                    'err': f'page loss: source has {src_pages} page(s) but only '
                           f'{len(pngs)} rendered' + (' (after repair)' if did_repair else '')
                           + ' — original kept'}
        comp = work / 'compressed.pdf'
        n_photo = 0
        n_color = 0

        def _gen_jbig2(name, k):
            """Binarize (adaptive Sauvola + optional despeckle) + generic self-contained
            JBIG2 for one page → .jb2 name. Uses the page's own DPI (native for high-res
            pages, base otherwise) so resolution and MediaBox sizing stay correct."""
            pd = page_dpi.get(k, dpi)
            binarize_png(work / name, min_size, despeckle, pd, sauvola_k)
            jb = f'g{k:05d}.jb2'
            with open(work / jb, 'wb') as jf:
                rr = subprocess.run([JBIG, '-p', '-a', '-D', str(pd), name],
                                    cwd=work, stdout=jf, stderr=subprocess.PIPE, text=True)
            if rr.returncode != 0 or (work / jb).stat().st_size == 0:
                raise RuntimeError(f'jbig2 page {k} rc={rr.returncode} {rr.stderr[:160]}')
            return jb

        # PAGE-TYPE ROUTER: classify every page, then dispatch each to its strategy.
        # Consecutive BITONAL pages (LINE/BLANK) are grouped into one multi-page
        # generic JBIG2 PDF (self-contained, so it renders in Chrome/Edge); PHOTO_*
        # pages each become one JPEG PDF.
        d = photo_dpi or dpi
        try:
            _rdr = PdfReader(str(render_src))   # kept alive for page.images extraction
            _rpages = _rdr.pages
        except Exception:
            _rpages = None

        def _page_at(k):
            try:
                return _rpages[k] if _rpages is not None else None
            except Exception:
                return None
        classes = [classify_page(work / n, k + 1, render_src, work, dpi,
                                 True, photo_thresh, photo_dpi, page=_page_at(k))
                   for k, n in enumerate(pngs)]
        n_color_line = sum(c.type == PT_COLOR_LINE for c in classes)
        n_vector = sum(c.type == PT_VECTOR for c in classes)
        n_photo = sum(c.type not in _PT_BITONAL and c.type not in _PT_PASSTHROUGH for c in classes)
        n_color = sum(c.type == PT_PHOTO_COLOR for c in classes)

        # NATIVE-DPI: for BITONAL pages whose source scan is higher-res than the base
        # render, re-render just that page at its native DPI so it isn't downsampled.
        # Only the (usually few) high-res pages pay the extra render; the rest keep the
        # fast batched render. Photos downsample on purpose; pass-through pages are copied.
        page_dpi = {}
        n_native = 0
        for k, c in enumerate(classes):
            if c.type in _PT_BITONAL:
                pd = _page_render_dpi(_page_at(k), dpi)
                if pd > dpi and _render_page_gray(render_src, k + 1, work / pngs[k], pd, timeout):
                    page_dpi[k] = pd
                    n_native += 1
        seg_pdfs = []
        i = 0
        try:
            while i < len(pngs):
                if classes[i].type in _PT_BITONAL:
                    # a run of consecutive bitonal pages -> one generic multi-page JBIG2.
                    jbs, j = [], i
                    while j < len(pngs) and classes[j].type in _PT_BITONAL:
                        jbs.append(_gen_jbig2(pngs[j], j)); j += 1
                    # Hand the page list to the wrapper over STDIN (one path per line), not
                    # as argv — a multi-thousand-page manual would otherwise overflow the OS
                    # command-line limit (Windows ~32K chars -> WinError 206). One call, any
                    # length. (`-s -` = standalone mode, read page files from stdin.)
                    seg = work / f's{i:05d}.pdf'
                    with open(seg, 'wb') as fout:
                        r = subprocess.run([PY, WRAP, '-s', '-'], input='\n'.join(jbs),
                                           cwd=work, stdout=fout, stderr=subprocess.PIPE, text=True)
                    if r.returncode != 0 or seg.stat().st_size == 0:
                        return {'src': src_p.name, 'orig': orig, 'new': 0,
                                'err': f'wrap failed rc={r.returncode} {r.stderr[:200]}'}
                    seg_pdfs.append(seg)
                    i = j
                elif classes[i].type in _PT_PASSTHROUGH:
                    # colour line art / born-digital vector page -> lossless pass-through of
                    # the original page (no binarize): keeps colour, text, vector and links.
                    seg = work / f's{i:05d}.pdf'
                    _color_line_seg(render_src, i + 1, seg)
                    seg_pdfs.append(seg)
                    i += 1
                else:  # PHOTO_GRAY / PHOTO_COLOR
                    seg = work / f's{i:05d}.pdf'
                    photo_seg_pdf(classes[i], seg, work, i + 1, d, jpeg_quality, photo_descreen)
                    seg_pdfs.append(seg)
                    i += 1
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
        # Put the compressed pages back INTO the original document, so links, bookmarks,
        # named destinations and metadata are inherited rather than rebuilt (and lost).
        # Done BEFORE the size decision so min-savings judges the artefact we will ship.
        # Falls back silently to the plain rebuild.
        grafted = _graft_into_source(render_src, comp)
        kept_original = comp.stat().st_size >= orig * (1 - min_savings)
        if kept_original:
            # Keep the ORIGINAL images — but if the source was malformed, keep the
            # REPAIRED copy instead of copying the broken bytes back out.
            base = work / 'orig.pdf'
            shutil.copyfile(str(render_src if did_repair else src_p), str(base))
            n_photo = n_color = n_color_line = n_vector = n_native = 0
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
        if n_color_line:
            note += f' [{n_color_line} colour line-art pg (lossless)]'
        if n_vector:
            note += f' [{n_vector} vector/born-digital pg (lossless)]'
        if n_native:
            note += f' [{n_native} hi-res pg at native dpi]'
        if not kept_original and not grafted:
            note += ' (rebuilt — links/bookmarks not carried over)'
        if n_retry[0]:
            note += f' (render retried x{n_retry[0]})'
        if did_repair:
            note += ' (repaired malformed PDF)'
        res = _ocr_and_place(base, dest_p, src_p, orig, work, ocr, language,
                             src_pages or len(pngs), kept_original, note, timeout, in_place)
        res['action'] = 'kept_original' if kept_original else 'compressed'
        return res
    except subprocess.TimeoutExpired as ex:
        return {'src': src_p.name, 'orig': orig, 'new': 0,
                'err': f'stalled: no progress for {timeout}s ({getattr(ex, "cmd", ["?"])[0]})'}
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


def _default_workers() -> int:
    """Default worker count = PHYSICAL cores. The heavy per-page binarize is
    memory-bandwidth-bound, so hyperthread siblings (the extra logical cores) add
    little and oversubscribing them just thrashes cache/bandwidth and slows the run.
    Detected per-platform without extra deps; falls back to logical count, then 4."""
    try:
        if sys.platform == 'darwin':
            out = subprocess.run(['sysctl', '-n', 'hw.physicalcpu'],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            if out.isdigit() and int(out) > 0:
                return int(out)
        elif sys.platform.startswith('linux'):
            pairs, phys, core = set(), None, None
            with open('/proc/cpuinfo') as f:
                for line in f:
                    if line.startswith('physical id'):
                        phys = line.split(':')[1].strip()
                    elif line.startswith('core id'):
                        core = line.split(':')[1].strip()
                    elif not line.strip() and phys is not None and core is not None:
                        pairs.add((phys, core)); phys = core = None
            if phys is not None and core is not None:
                pairs.add((phys, core))
            if pairs:
                return len(pairs)
        elif os.name == 'nt':
            out = subprocess.run(
                ['powershell', '-NoProfile', '-NonInteractive', '-Command',
                 '(Get-CIMInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum'],
                capture_output=True, text=True, timeout=15).stdout.strip()
            if out.isdigit() and int(out) > 0:
                return int(out)
    except Exception:
        pass
    return os.cpu_count() or 4


# ── Dry-run preview (runs in a worker process) ────────────────────────────────

def preview_one(src: str, dpi: int, despeckle: bool, min_size: int,
                ocr_only: bool, photo_thresh: float, photo_dpi: int,
                jpeg_quality: int, min_savings: float,
                sauvola_k: float, photo_descreen: float) -> dict:
    """Predict what compress_one WOULD do to a file, WITHOUT writing anything. Used by
    --dry-run so a huge collection can be previewed (born-digital? scanned? projected
    size?) before committing to a full run. Uses the same born-digital check and the
    same sample pre-check the real run uses, so the prediction tracks reality."""
    src_p = Path(src)
    orig = src_p.stat().st_size
    work = Path(tempfile.mkdtemp(prefix='jbprev_'))
    try:
        born, bsig = looks_born_digital(src_p)
        if born:
            return {'src': src_p.name, 'orig': orig, 'new': orig, 'pages': bsig.get('sampled'),
                    'kept': True, 'err': None, 'action': 'born_digital', 'signals': bsig,
                    'note': f' (would copy untouched; scan_frac={bsig.get("scan_frac")})'}
        if ocr_only:
            return {'src': src_p.name, 'orig': orig, 'new': orig, 'pages': None, 'kept': True,
                    'err': None, 'action': 'ocr_only', 'note': ' (OCR-only mode; not compressed)'}
        proj = sample_projection(src_p, work, dpi, despeckle, min_size,
                                 photo_thresh, photo_dpi, jpeg_quality,
                                 sauvola_k, photo_descreen)
        est_new = int(proj * orig)
        if proj >= PRECHECK_SKIP_RATIO:
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
    as underscores (e.g. `dpi = 300`, `no_ocr = true`, `dest = "OUT"`).
    tomllib is stdlib only from Python 3.11+; the import is deferred to here (only
    once a config file is actually in play) so a run with no config file works fine
    on 3.10, and a run that DOES need one gets a clear error instead of crashing on
    startup regardless of whether --config was ever used."""
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
        import tomllib
    except ModuleNotFoundError:
        print('ERROR: reading a TOML config file needs Python 3.11+ (tomllib is stdlib '
              'there); either upgrade Python or drop the --config / ocrmyworkshopmanual.toml '
              'and pass options on the command line instead.', file=sys.stderr)
        sys.exit(1)
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
    ap.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    ap.add_argument('src', type=Path, nargs='?', default=None,
                    help='source: a folder tree of scanned PDFs, OR a single .pdf file '
                         '(omit when using --from-list)')
    ap.add_argument('--dest', type=Path, default=None,
                    help='output root for a folder (default: sibling "<src> (COMPRESSED)"), '
                         'or the output path/folder for a single-file src (default: sibling '
                         '"<name> (COMPRESSED).pdf")')
    ap.add_argument('--in-place', action='store_true',
                    help='OVERWRITE each PDF with its compressed/OCR result IN PLACE (no output '
                         'tree). Non-PDF files, folder structure, born-digital PDFs and '
                         'already-optimal files are left untouched; unchanged files are not '
                         'rewritten. DESTRUCTIVE — back up first. The report is written to the '
                         'tool folder, not among the manuals. Cannot be combined with --dest.')
    ap.add_argument('--dpi', type=int, default=200, help='render dpi (default 200; good speed/quality)')
    ap.add_argument('--workers', type=int, default=_default_workers(),
                    help='parallel worker processes (default: one per PHYSICAL core — the '
                         'CPU-bound binarize is memory-bandwidth-bound, so logical/hyperthread '
                         'cores add little and oversubscribing them thrashes; falls back to '
                         'logical count, then 4, if physical cores cannot be detected)')
    ap.add_argument('--limit', type=int, default=0, help='process only first N files (test)')
    ap.add_argument('--no-despeckle', action='store_true', help='disable background speckle removal')
    ap.add_argument('--sauvola-k', type=float, default=0.30,
                    help='adaptive threshold sensitivity (default 0.30; lower=bolder/thicker ink, '
                         'higher=thinner/cleaner)')
    ap.add_argument('--min-size', type=int, default=10, help='remove black blobs smaller than N px')
    ap.add_argument('--photo-descreen', type=float, default=0.6,
                    help='descreen grayscale photo pages: gaussian sigma (scaled to dpi) that merges '
                         'halftone dot grain into smooth tone — less dithering + smaller (0 = off; default 0.6)')
    ap.add_argument('--no-ocr', action='store_true', help='skip the searchable OCR text layer')
    ap.add_argument('--language', default='eng',
                    help='Tesseract OCR language(s), e.g. eng or eng+fra+spa+deu; '
                         "'auto' detects each file's script (Latin->eng, Cyrillic->rus+eng) via Tesseract OSD")
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
    ap.add_argument('--log', type=Path, default=None,
                    help='path for the run report log (default: a timestamped file in the dest root)')
    ap.add_argument('--no-log', action='store_true', help='do not write a run report log')
    ap.add_argument('--dry-run', action='store_true',
                    help='preview only: classify each pdf (born-digital? scanned?) and project its '
                         'compressed size, report what WOULD happen + projected savings, write NOTHING')
    ap.add_argument('--timeout', type=int, default=600,
                    help='STALL timeout: max seconds a step may make NO progress (no new page '
                         'rendered, no bytes written) before it is treated as hung, killed, and the '
                         'file marked FAILED. This is not a time budget — a slow-but-working file is '
                         'never killed for being big, however long it takes (default 600 = 10 min of '
                         'no progress; 0 disables). Transient crashes are retried automatically.')
    ap.add_argument('--min-free-gb', type=float, default=1.0,
                    help='abort before starting if the destination drive has less than this many GB '
                         'free (default 1.0; 0 disables the check)')
    ap.add_argument('--retry-failed', type=Path, default=None, metavar='REPORT.csv',
                    help='reprocess ONLY the files marked FAILED in a previous run report .csv '
                         '(re-runs them even if a dest exists)')
    ap.add_argument('--from-list', type=Path, default=None, metavar='FILE',
                    help='process the exact list of PDF paths in FILE (one per line; # comments ok), '
                         'each compressed/OCR IN PLACE. For working a specific SUBSET of a huge tree '
                         'without walking the whole thing — plain folder mode already runs one global '
                         'worker pool across every PDF under the tree, so it is NOT folder-limited; '
                         'reach for --from-list only when you want to hand-pick which files to do. '
                         'Ignores the src argument.')
    ap.add_argument('--config', type=Path, default=None,
                    help='TOML config file of default option values (CLI flags override it); if omitted, '
                         './ocrmyworkshopmanual.toml is loaded when present. Keys match long option '
                         'names with dashes as underscores, e.g. dpi = 300, no_ocr = true')
    _apply_config_defaults(ap)
    args = ap.parse_args()

    err = _validate_numeric_args(args)
    if err:
        print(f'ERROR: {err}', file=sys.stderr); sys.exit(1)

    err = check_tools(want_ocr=not args.no_ocr)
    if err:
        print(f'ERROR: {err}', file=sys.stderr); sys.exit(1)

    if args.in_place and args.dest:
        print('ERROR: --in-place cannot be combined with --dest', file=sys.stderr); sys.exit(1)
    if args.no_ocr and args.ocr_only:
        print('ERROR: --no-ocr and --ocr-only cannot be combined (one skips OCR entirely, '
              'the other skips compression to ONLY add OCR)', file=sys.stderr); sys.exit(1)
    if args.from_list and args.src:
        print('ERROR: pass EITHER a src OR --from-list, not both', file=sys.stderr); sys.exit(1)
    if not args.from_list and not args.src:
        print('ERROR: give a source folder/.pdf, or --from-list FILE', file=sys.stderr); sys.exit(1)

    single_dest = None
    if args.from_list:
        # GLOBAL-POOL mode: an explicit list of PDFs, each processed IN PLACE. Everything
        # downstream is the normal in-place path, but the job list spans all folders at once,
        # so the single worker pool parallelises across the whole set (not per-folder).
        if not args.from_list.exists():
            print(f'ERROR: --from-list file not found: {args.from_list}', file=sys.stderr); sys.exit(1)
        listed = [Path(x.strip()) for x in args.from_list.read_text(encoding='utf-8').splitlines()
                  if x.strip() and not x.strip().startswith('#')]
        pdfs = [p for p in listed if p.suffix.lower() == '.pdf' and p.is_file()]
        if not pdfs:
            print(f'ERROR: no existing .pdf paths in {args.from_list}', file=sys.stderr); sys.exit(1)
        args.in_place = True   # listed files are compressed/OCR'd where they sit
        try:
            rel_base = Path(os.path.commonpath([str(p) for p in pdfs]))
        except ValueError:      # paths on different drives -> no common base
            rel_base = pdfs[0].parent
        dest_root = rel_base
        src_root = args.from_list
        miss = len(listed) - len(pdfs)
        print(f'From-list: {len(pdfs)} PDF(s) from {args.from_list.name}'
              + (f'  ({miss} skipped: missing or non-.pdf)' if miss else ''))
    else:
        src_root = args.src
        # SINGLE-FILE mode: src is one .pdf. rel_base is its folder so the report shows just
        # the filename; default output is a sibling "<name> (COMPRESSED).pdf".
        if src_root.is_file():
            if src_root.suffix.lower() != '.pdf':
                print(f'ERROR: not a PDF: {src_root}', file=sys.stderr); sys.exit(1)
            rel_base = src_root.parent
            pdfs = [src_root]
            if args.in_place:
                dest_root = rel_base
            elif args.dest:
                single_dest = args.dest if args.dest.suffix.lower() == '.pdf' \
                    else args.dest / src_root.name
                dest_root = single_dest.parent
            else:
                single_dest = src_root.with_name(f'{src_root.stem} (COMPRESSED){src_root.suffix}')
                dest_root = single_dest.parent
        elif src_root.is_dir():
            rel_base = src_root
            pdfs = sorted(p for p in src_root.rglob('*.pdf'))
            dest_root = rel_base if args.in_place else (args.dest or src_root.parent / (src_root.name + ' (COMPRESSED)'))
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

    if args.in_place:
        # overwrite each PDF where it sits; no skip-if-exists (dest == src). Already-optimal
        # files are detected per-file and left untouched by the pipeline, so re-runs are safe.
        jobs = [(str(p), str(p)) for p in pdfs]
        skipped = 0
    elif single_dest is not None:
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
    print(f'Dest        : {"IN-PLACE (overwrites source PDFs)" if args.in_place else dest_root}')
    ocr_desc = f'OCR({args.language})' if not args.no_ocr else 'no OCR'
    bd_desc = 'born-digital-safe'
    if args.ocr_only:
        print(f'{len(pdfs)} PDFs found, {skipped} already done, {len(jobs)} to process '
              f'@ {args.workers} workers, OCR-ONLY (no compression), {ocr_desc}, {bd_desc}\n')
    else:
        photo_desc = f'photo>{args.photo_threshold:g}@{args.photo_dpi}dpi'
        bin_desc = f'adaptive(sauvola k={args.sauvola_k:g})'
        print(f'{len(pdfs)} PDFs found, {skipped} already done, {len(jobs)} to process '
              f'@ {args.dpi} dpi, {args.workers} workers, generic mode, {bin_desc}, '
              f'{"despeckle" if not args.no_despeckle else "no despeckle"}, '
              f'{photo_desc}, {ocr_desc}, {bd_desc}\n')
    if not jobs:
        print('Nothing to do.'); return
    if args.dry_run:
        print('*** DRY-RUN: previewing only — nothing will be written. ***\n')
    elif args.in_place:
        print('*** IN-PLACE: source PDFs will be OVERWRITTEN with their compressed/OCR '
              'result. Non-PDFs, structure, born-digital & already-optimal files untouched. ***\n')

    set_below_normal_priority()
    if not args.dry_run:
        _sweep_stale_scratch()   # reclaim scratch orphaned by earlier killed runs
    t0 = time.time()
    done = fail = kept = 0
    tot_orig = tot_new = 0
    results = []  # accumulated for the run log

    # open the report CSV up front and flush a row per file, so progress is visible
    # live (the full .log + a final complete .csv are (re)written at the end).
    report_dir = (SCRIPT_DIR / 'reports') if args.in_place else \
        (src_root.parent if args.dry_run else None)
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

    N = len(jobs)
    # duplicate check is skipped in dry-run (a preview shouldn't hash every byte)
    dup_check = not args.dry_run
    seen_hash = {}   # content-hash -> first rel seen (for a live console marker)
    interrupted = False
    try:
        with cf.ProcessPoolExecutor(max_workers=args.workers,
                                    initializer=set_below_normal_priority) as ex:
            if args.dry_run:
                futs = {ex.submit(preview_one, s, args.dpi,
                                  not args.no_despeckle, args.min_size, args.ocr_only,
                                  args.photo_threshold, args.photo_dpi, args.jpeg_quality,
                                  args.min_savings, args.sauvola_k, args.photo_descreen): (s, d)
                        for s, d in jobs}
            else:
                futs = {ex.submit(compress_one, s, d, args.dpi,
                                  not args.no_despeckle, args.min_size,
                                  not args.no_ocr, args.language,
                                  args.photo_threshold, args.photo_dpi, args.jpeg_quality,
                                  args.min_savings, args.ocr_only, args.sauvola_k,
                                  args.photo_descreen,
                                  args.timeout, args.in_place): (s, d)
                        for s, d in jobs}
            for i, fut in enumerate(cf.as_completed(futs), 1):
                s, d = futs[fut]
                try:
                    res = fut.result()
                except Exception as ex:
                    # a worker DIED (BrokenProcessPool from OOM / OS-kill / native
                    # segfault) or raised — don't let one dead worker abort the whole
                    # run; mark this file FAILED (original untouched, in-place never
                    # wrote) and carry on. Every still-pending file will land here too,
                    # so the run ends with a complete report you can --retry-failed.
                    try:
                        orig = Path(s).stat().st_size
                    except Exception:
                        orig = 0
                    res = {'src': Path(s).name, 'orig': orig, 'new': 0,
                           'err': f'worker died ({type(ex).__name__}): {str(ex)[:140]}'}
                res['rel'] = os.path.relpath(s, str(rel_base))
                results.append(res)
                dmark = ''
                if dup_check and not res.get('err'):
                    try:
                        res['hash'] = _file_hash(Path(s))
                    except Exception:
                        res['hash'] = None
                    if res.get('hash'):
                        if res['hash'] in seen_hash:
                            res['duplicate_of'] = seen_hash[res['hash']]
                            dmark = f'  [dup of {seen_hash[res["hash"]]}]'
                        else:
                            seen_hash[res['hash']] = res['rel']
                elapsed = time.time() - t0
                eta = (N - i) * (elapsed / i) if i < N and elapsed > 0 else 0
                eta_str = f'  [ETA {eta/60:.0f}m]' if eta >= 30 else ''
                if res['err']:
                    fail += 1
                    _say(f'  [{i}/{N}] FAIL {res["src"]}: {res["err"]}{dmark}{eta_str}')
                else:
                    done += 1
                    if res.get('kept'):
                        kept += 1
                    tot_orig += res['orig']; tot_new += res['new']
                    pct = res['new'] * 100 // res['orig'] if res['orig'] else 0
                    arrow = f'~{mb(res["new"]):.0f}' if args.dry_run else f'{mb(res["new"]):.0f}'
                    _say(f'  [{i}/{N}] {res["src"][:60]}  '
                         f'{mb(res["orig"]):.0f}->{arrow} MB ({pct}%){res.get("note", "")}{dmark}{eta_str}')
                if csv_live:   # flush a row per file so the report survives a hard stop
                    try:
                        csv_live.write(_csv_row(_report_row(res))); csv_live.flush()
                    except Exception:
                        pass
    except KeyboardInterrupt:
        interrupted = True
        _say('\n*** interrupted (Ctrl-C) — finishing up and writing the report for '
             'work done so far; in-place files are each intact (original or complete) ***')
    finally:
        if csv_live:
            try:
                csv_live.close()
            except Exception:
                pass

    dup_sets = _flag_duplicates(results) if dup_check else 0
    dt = time.time() - t0
    n_born = sum(1 for r in results if r.get('action') == 'born_digital')
    verb = 'Previewed' if args.dry_run else 'processed'
    head = 'Interrupted after' if interrupted else 'Done in'
    _say(f'\n{head} {dt/60:.1f} min. {verb} {done} ({done - kept} '
         f'{"would compress" if args.dry_run else "compressed"}, '
         f'{kept} kept-original/OCR-only incl. {n_born} born-digital), failed {fail}')
    if tot_orig:
        word = 'Projected' if args.dry_run else 'Total'
        saved = 'would save' if args.dry_run else 'saved'
        _say(f'{word}: {mb(tot_orig):.0f} MB -> {mb(tot_new):.0f} MB '
             f'({tot_new*100//tot_orig}% ; {saved} {mb(tot_orig-tot_new):.0f} MB)')
    if fail:
        _say(f'{fail} file(s) FAILED — see the report .csv (filter the `error` column); '
             f're-run them with --retry-failed <report>.csv once the cause is cleared.')
    if dup_sets:
        n_dup_files = sum(1 for r in results if r.get('duplicate_of'))
        _say(f'Duplicates: {n_dup_files} file(s) in {dup_sets} set(s) flagged '
             f'(byte-identical; all still processed — see report)')
    _say('Output: (dry-run — nothing written)' if args.dry_run else
         ('Output: IN-PLACE (source PDFs overwritten)' if args.in_place else f'Output: {dest_root}'))

    if not args.no_log:
        settings = {
            'in_place': args.in_place,
            'dpi': args.dpi, 'workers': args.workers, 'mode': 'generic',
            'binarization': f'adaptive sauvola-k={args.sauvola_k:g}',
            'despeckle': not args.no_despeckle, 'min_size': args.min_size,
            'photo_threshold': args.photo_threshold, 'photo_dpi': args.photo_dpi,
            'jpeg_quality': args.jpeg_quality,
            'photo_descreen': args.photo_descreen, 'ocr': ocr_desc, 'ocr_only': args.ocr_only,
            'min_savings': args.min_savings,
            'timeout': args.timeout,
            'retry_failed': str(args.retry_failed) if args.retry_failed else False,
            'dry_run': args.dry_run,
        }
        try:
            # reuse the same path the live CSV was written to (report_dir/report_log_path
            # were computed before the run); write_run_log (re)writes the full .log + .csv
            log_path = write_run_log(report_log_path, dest_root, src_root, results, settings,
                                     t0, dt, len(pdfs), skipped, args.limit, fail, done, kept,
                                     dry_run=args.dry_run, report_dir=report_dir)
            _say(f'Log: {log_path}  (+ .csv)')
        except Exception as ex:
            _say(f'(could not write run log: {ex})')
    if interrupted:
        sys.exit(130)   # conventional exit code for Ctrl-C


if __name__ == '__main__':
    main()
