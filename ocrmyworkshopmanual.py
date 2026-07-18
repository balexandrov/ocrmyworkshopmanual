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
NOTE: for SCANNED/image PDFs only. Born-digital/vector PDFs would be rasterised.

Usage:
  python ocrmyworkshopmanual.py "M:\\path\\to\\folder"           # compress + OCR a tree
  python ocrmyworkshopmanual.py SRC --dest OUT --workers 10
  python ocrmyworkshopmanual.py SRC --limit 3                    # test first N files
  python ocrmyworkshopmanual.py SRC --no-ocr                     # compress only
  python ocrmyworkshopmanual.py SRC --ocr-only                   # add text layer only, no compression
  python ocrmyworkshopmanual.py SRC --language eng+fra+spa+deu   # multilingual OCR
  python ocrmyworkshopmanual.py SRC --symbol                     # smaller, GS/Acrobat only

Tuning notes (learned on Toyota FSM scans):
  OCR (default on) adds a searchable text layer via ocrmypdf; --no-ocr to skip.
                   Needs Tesseract on PATH and ocrmypdf installed.
  GENERIC (default) vs --symbol: symbol mode shares one glyph dictionary across
                   all pages → ~30% smaller, BUT PDFium (Chrome/Edge) renders a
                   large shared dictionary as BLANK pages. Generic works everywhere.
  --dpi 200        good speed/quality balance for on-screen viewing (~native ~220).
  --threshold 125  gray<T => ink. Keep LOW (~120-130): pages with a gray shaded
                   background wash (e.g. foldout wiring diagrams) turn to
                   salt-and-pepper NOISE at high thresholds like 190.
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
import os
import shutil
import subprocess
import sys
import tempfile
import time
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

def despeckle_png(path: Path, thresh: int, min_size: int):
    """In place: threshold a grayscale page to bitonal and drop black connected
    components smaller than min_size px (scan speckle). Saved as 1-bit PNG."""
    g = np.asarray(Image.open(path).convert('L'))
    ink = g < thresh
    lbl, _ = ndimage.label(ink, structure=_STRUCT8)
    counts = np.bincount(lbl.ravel())
    small = np.where(counts < min_size)[0]
    small = small[small != 0]
    if small.size:
        ink &= ~np.isin(lbl, small)
    arr = np.where(ink, 0, 255).astype('uint8')
    Image.fromarray(arr).convert('1').save(path)


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


def photo_page_pdf(src_p: Path, page_no: int, out_pdf: Path, work: Path,
                   dpi: int, photo_dpi: int, quality: int) -> bool:
    """A continuous-tone page: re-render it in COLOR at photo_dpi, keep colour if the
    page actually has colour (else grayscale to save space), JPEG, wrap to a 1-page
    PDF sized (via embedded dpi) to match the bitonal pages. Returns True if colour."""
    d = photo_dpi or dpi
    cpng = work / f'color{page_no}.png'
    subprocess.run([GS, '-sDEVICE=png16m', f'-r{d}', f'-dFirstPage={page_no}', f'-dLastPage={page_no}',
                    '-dNOPAUSE', '-dBATCH', '-dQUIET', '-sOutputFile=' + str(cpng), win_long(src_p)],
                   capture_output=True)
    im = Image.open(cpng).convert('RGB')
    a = np.asarray(im).astype(np.int16)
    is_color = float(((a.max(2) - a.min(2)) > 30).mean()) > 0.02
    jpg = work / f'photo{page_no}.jpg'
    (im if is_color else im.convert('L')).save(jpg, 'JPEG', quality=quality, dpi=(d, d))
    cpng.unlink(missing_ok=True)
    with open(out_pdf, 'wb') as f:
        f.write(img2pdf.convert(str(jpg)))
    return is_color


def has_text(pdf: Path, sample: int = 6, min_chars: int = 40) -> bool:
    """True if the PDF already has a real text layer (sampled first pages)."""
    try:
        r = PdfReader(str(pdf))
        n = min(sample, len(r.pages))
        return sum(len((r.pages[i].extract_text() or '').strip()) for i in range(n)) >= min_chars
    except Exception:
        return False


def _ocr_and_place(base: Path, dest_p: Path, src_p: Path, orig: int, work: Path,
                   ocr: bool, language: str, pages: int, kept: bool, note: str) -> dict:
    """Add an OCR text layer to `base` (only if it has none), then atomically place
    it at dest. Shared by the compress path and the --ocr-only path."""
    final = base
    if ocr:
        if has_text(base):
            note += ' (had text, OCR skipped)'
        else:
            ocr_pdf = work / 'ocr.pdf'
            r = subprocess.run(
                [PY, '-m', 'ocrmypdf', '--language', language, '--optimize', '0',
                 '--output-type', 'pdf', '--skip-text', '--quiet', '--jobs', '1',
                 str(base), str(ocr_pdf)], capture_output=True, text=True)
            if r.returncode == 0 and ocr_pdf.exists() and ocr_pdf.stat().st_size > 0:
                final = ocr_pdf
            else:
                note += ' (OCR FAILED)'
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = dest_p.with_suffix(dest_p.suffix + '.part')
    shutil.copyfile(str(final), str(tmp_out))
    os.replace(str(tmp_out), str(dest_p))
    return {'src': src_p.name, 'orig': orig, 'new': dest_p.stat().st_size,
            'pages': pages, 'note': note, 'kept': kept, 'err': None}


def sample_projection(src_p: Path, work: Path, dpi: int, despeckle: bool, thresh: int,
                      min_size: int, photo_thresh: float, photo_dpi: int, jpeg_quality: int,
                      k: int = 10) -> float:
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
        if photo_coverage(png, dpi) > photo_thresh:
            d = photo_dpi or dpi
            cpng = sub / f'c{p}.png'
            subprocess.run([GS, '-sDEVICE=png16m', f'-r{d}', f'-dFirstPage={p}', f'-dLastPage={p}',
                            '-dNOPAUSE', '-dBATCH', '-dQUIET', '-sOutputFile=' + str(cpng), win_long(src_p)],
                           capture_output=True)
            im = Image.open(cpng).convert('RGB'); a = np.asarray(im).astype(np.int16)
            col = ((a.max(2) - a.min(2)) > 30).mean() > 0.02
            jpg = sub / f'j{p}.jpg'
            (im if col else im.convert('L')).save(jpg, 'JPEG', quality=jpeg_quality, dpi=(d, d))
            comp += jpg.stat().st_size
        else:
            if despeckle:
                despeckle_png(png, thresh, min_size)
            r = subprocess.run([JBIG, '-p', '-a', '-D', str(dpi), png.name], cwd=sub, capture_output=True)
            comp += len(r.stdout)
    shutil.rmtree(sub, ignore_errors=True)
    return ((comp / got) * n / orig) if got else 0.0


# ── One file (runs in a worker process) ──────────────────────────────────────

def compress_one(src: str, dest: str, dpi: int,
                 despeckle: bool = True, thresh: int = 125, min_size: int = 10,
                 symbol: bool = False, ocr: bool = True, language: str = 'eng',
                 detect_photos: bool = True, photo_thresh: float = 0.02,
                 photo_dpi: int = 150, jpeg_quality: int = 50,
                 min_savings: float = 0.10, ocr_only: bool = False,
                 precheck: bool = True, precheck_skip: float = 0.90) -> dict:
    """Render -> classify pages -> (bitonal JBIG2 | grayscale JPEG) -> merge -> OCR.

    MIXED CONTENT (detect_photos=True): line-art/text pages are binarized to tiny
    generic JBIG2; continuous-tone PHOTO pages (mid-tone fraction > photo_thresh)
    are kept grayscale JPEG so they don't get wrecked by thresholding. Pages are
    merged back in order. --no-photo / detect_photos=False forces all-bitonal.
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
        note0 = ' (OCR-only, not compressed)' if ocr_only else ''
        # cheap pre-check: sample-compress a few pages; if it won't beat the original,
        # skip full compression and just OCR the original (avoids wasted work + growth).
        if not ocr_only and not symbol and precheck:
            proj = sample_projection(src_p, work, dpi, despeckle, thresh, min_size,
                                     photo_thresh, photo_dpi, jpeg_quality)
            if proj >= precheck_skip:
                ocr_only = True
                note0 = f' (compression skipped: sample projected {proj*100:.0f}% of original)'
        if ocr_only:
            # No (worthwhile) compression: keep the original images, just add the OCR layer.
            base = work / 'orig.pdf'
            shutil.copyfile(str(src_p), str(base))
            return _ocr_and_place(base, dest_p, src_p, orig, work, ocr, language,
                                  len(PdfReader(str(base)).pages), True, note0)
        # 1) render pages to grayscale PNG
        r = subprocess.run(
            [GS, '-sDEVICE=pnggray', f'-r{dpi}', '-dNOPAUSE', '-dBATCH', '-dQUIET',
             '-sOutputFile=' + str(work / 'p%04d.png'), win_long(src_p)],
            capture_output=True, text=True)
        pngs = sorted(p.name for p in work.glob('p*.png'))
        if r.returncode != 0 or not pngs:
            return {'src': src_p.name, 'orig': orig, 'new': 0,
                    'err': f'render failed rc={r.returncode} {r.stderr[:200]}'}
        comp = work / 'compressed.pdf'
        n_photo = 0
        n_color = 0

        def _gen_jbig2(name, k):
            """Despeckle (if line) + generic self-contained JBIG2 for one page → .jb2 name."""
            if despeckle:
                despeckle_png(work / name, thresh, min_size)
            jb = f'g{k:05d}.jb2'
            with open(work / jb, 'wb') as jf:
                rr = subprocess.run([JBIG, '-p', '-a', '-D', str(dpi), name],
                                    cwd=work, stdout=jf, stderr=subprocess.PIPE, text=True)
            if rr.returncode != 0 or (work / jb).stat().st_size == 0:
                raise RuntimeError(f'jbig2 page {k} rc={rr.returncode} {rr.stderr[:160]}')
            return jb

        if symbol:
            # SYMBOL mode: pure bitonal, one shared dictionary (GS/Acrobat only, no photo detect)
            if despeckle:
                for name in pngs:
                    despeckle_png(work / name, thresh, min_size)
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
            # GENERIC + MIXED CONTENT: photo pages -> (colour/gray) JPEG, line pages -> JBIG2.
            is_photo = [detect_photos and photo_coverage(work / n, dpi) > photo_thresh for n in pngs]
            n_photo = sum(is_photo)
            seg_pdfs = []
            i = 0
            try:
                while i < len(pngs):
                    seg = work / f's{i:05d}.pdf'
                    if is_photo[i]:
                        if photo_page_pdf(src_p, i + 1, seg, work, dpi, photo_dpi, jpeg_quality):
                            n_color += 1
                        i += 1
                    else:
                        # a run of consecutive line pages -> one multi-page JBIG2 PDF
                        jbs, j = [], i
                        while j < len(pngs) and not is_photo[j]:
                            jbs.append(_gen_jbig2(pngs[j], j)); j += 1
                        with open(seg, 'wb') as fout:
                            r = subprocess.run([PY, WRAP, '-s', *jbs], cwd=work, stdout=fout,
                                               stderr=subprocess.PIPE, text=True)
                        if r.returncode != 0 or seg.stat().st_size == 0:
                            return {'src': src_p.name, 'orig': orig, 'new': 0,
                                    'err': f'wrap failed rc={r.returncode} {r.stderr[:200]}'}
                        i = j
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
        return _ocr_and_place(base, dest_p, src_p, orig, work, ocr, language,
                              len(pngs), kept_original, note)
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


# ── Batch driver ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description='Compress scanned PDFs to small generic-JBIG2 and add a searchable OCR text layer.')
    ap.add_argument('src', type=Path, help='source folder tree of scanned PDFs')
    ap.add_argument('--dest', type=Path, default=None,
                    help='output root (default: sibling "<src> (COMPRESSED)")')
    ap.add_argument('--dpi', type=int, default=200, help='render dpi (default 200; good speed/quality)')
    ap.add_argument('--workers', type=int, default=min(10, (os.cpu_count() or 4)))
    ap.add_argument('--limit', type=int, default=0, help='process only first N files (test)')
    ap.add_argument('--no-despeckle', action='store_true', help='disable background speckle removal')
    ap.add_argument('--threshold', type=int, default=125, help='gray<T => ink (keep low, ~125)')
    ap.add_argument('--min-size', type=int, default=10, help='remove black blobs smaller than N px')
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
    ap.add_argument('--jpeg-quality', type=int, default=50, help='JPEG quality for photo pages (default 50)')
    ap.add_argument('--min-savings', type=float, default=0.10,
                    help='keep the compressed file only if it is at least this fraction smaller than '
                         'the original; else keep the original and OCR only (default 0.10)')
    ap.add_argument('--ocr-only', action='store_true',
                    help='do not compress at all: copy each original and just add the OCR text layer '
                         '(skips files that already have text)')
    ap.add_argument('--no-precheck', action='store_true',
                    help='disable the sample pre-check that skips compression for files it would not shrink')
    ap.add_argument('--precheck-threshold', type=float, default=0.90,
                    help='skip full compression if a sample projects the result >= this fraction of the '
                         'original (default 0.90); the --min-savings fallback still guards the rest')
    args = ap.parse_args()

    err = check_tools(want_ocr=not args.no_ocr)
    if err:
        print(f'ERROR: {err}', file=sys.stderr); sys.exit(1)

    src_root = args.src
    if not src_root.is_dir():
        print(f'ERROR: source folder not found: {src_root}', file=sys.stderr); sys.exit(1)
    dest_root = args.dest or src_root.parent / (src_root.name + ' (COMPRESSED)')

    pdfs = sorted(p for p in src_root.rglob('*.pdf'))
    jobs = []
    for src in pdfs:
        dest = dest_root / src.relative_to(src_root)
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
    if args.ocr_only:
        print(f'{len(pdfs)} PDFs found, {skipped} already done, {len(jobs)} to process '
              f'@ {args.workers} workers, OCR-ONLY (no compression), {ocr_desc}\n')
    else:
        photo_desc = 'no photo-detect' if (args.no_photo or args.symbol) else f'photo>{args.photo_threshold:g}@{args.photo_dpi}dpi'
        print(f'{len(pdfs)} PDFs found, {skipped} already done, {len(jobs)} to process '
              f'@ {args.dpi} dpi, {args.workers} workers, '
              f'{"symbol" if args.symbol else "generic"} mode, '
              f'{"despeckle T=" + str(args.threshold) if not args.no_despeckle else "no despeckle"}, '
              f'{photo_desc}, {ocr_desc}\n')
    if not jobs:
        print('Nothing to do.'); return

    set_below_normal_priority()
    t0 = time.time()
    done = fail = kept = 0
    tot_orig = tot_new = 0
    with cf.ProcessPoolExecutor(max_workers=args.workers,
                                initializer=set_below_normal_priority) as ex:
        futs = {ex.submit(compress_one, s, d, args.dpi,
                          not args.no_despeckle, args.threshold, args.min_size, args.symbol,
                          not args.no_ocr, args.language,
                          not args.no_photo, args.photo_threshold, args.photo_dpi, args.jpeg_quality,
                          args.min_savings, args.ocr_only,
                          not args.no_precheck, args.precheck_threshold): s
                for s, d in jobs}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            res = fut.result()
            if res['err']:
                fail += 1
                print(f'  [{i}/{len(jobs)}] FAIL {res["src"]}: {res["err"]}')
            else:
                done += 1
                if res.get('kept'):
                    kept += 1
                tot_orig += res['orig']; tot_new += res['new']
                pct = res['new'] * 100 // res['orig'] if res['orig'] else 0
                print(f'  [{i}/{len(jobs)}] {res["src"][:66]}  '
                      f'{mb(res["orig"]):.0f}->{mb(res["new"]):.0f} MB ({pct}%){res.get("note", "")}')

    dt = time.time() - t0
    print(f'\nDone in {dt/60:.1f} min. processed {done} ({done - kept} compressed, '
          f'{kept} kept-original/OCR-only), failed {fail}')
    if tot_orig:
        print(f'Total: {mb(tot_orig):.0f} MB -> {mb(tot_new):.0f} MB '
              f'({tot_new*100//tot_orig}% ; saved {mb(tot_orig-tot_new):.0f} MB)')
    print(f'Output: {dest_root}')


if __name__ == '__main__':
    main()
