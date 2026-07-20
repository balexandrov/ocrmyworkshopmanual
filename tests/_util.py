"""Shared helpers for the fixture extractor and the test suite.

Everything here imports the real pipeline module (`ocrmyworkshopmanual`) so the
tests exercise the SAME code paths as production, not a re-implementation.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

# Make the repo-root module importable no matter where pytest is invoked from.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ocrmyworkshopmanual as owm  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_DIR / 'fixtures'
REPORTS_DIR = TESTS_DIR / 'reports'
MANIFEST = FIXTURES_DIR / 'manifest.json'

# Folder name  ->  the PageType constant classify_page() is expected to return.
TYPE_DIRS = {
    'line': owm.PT_LINE,
    'blank': owm.PT_BLANK,
    'photo_gray': owm.PT_PHOTO_GRAY,
    'photo_color': owm.PT_PHOTO_COLOR,
}


def tools_missing() -> str | None:
    """Return a human message if a binary the tests need is absent, else None."""
    if not owm.GS:
        return 'Ghostscript not found (owm.GS is None)'
    if not owm.JBIG:
        return 'jbig2enc not found (owm.JBIG is None)'
    return None


def render_gray(pdf: Path, page_no: int, dpi: int, out_png: Path) -> bool:
    """Render one page of `pdf` to a grayscale PNG via the same Ghostscript call
    the pipeline uses. Returns True on success."""
    subprocess.run(
        [owm.GS, '-sDEVICE=pnggray', f'-r{dpi}',
         f'-dFirstPage={page_no}', f'-dLastPage={page_no}',
         '-dNOPAUSE', '-dBATCH', '-dQUIET',
         '-sOutputFile=' + str(out_png), owm.win_long(pdf)],
        capture_output=True)
    return out_png.exists()


def classify(pdf: Path, page_no: int, dpi: int, work: Path,
             photo_thresh: float = 0.02, photo_dpi: int = 150):
    """Render page `page_no` gray, then run the production classify_page() on it.
    Returns (PageType, signals_dict). signals are the cheap measurements the
    router keys off, recorded so a human can sanity-check a fixture's label."""
    png = work / f'g{page_no}.png'
    if not render_gray(pdf, page_no, dpi, png):
        return None, {}
    g = np.asarray(Image.open(png).convert('L'))
    signals = {
        'ink_frac': round(float((g < 100).mean()), 6),
        'photo_cov': round(owm.photo_coverage(png, dpi), 4),
    }
    pc = owm.classify_page(png, page_no, pdf, work, dpi, True, photo_thresh, photo_dpi)
    signals['type'] = pc.type
    if pc.color_png:
        pc.color_png.unlink(missing_ok=True)
    return pc.type, signals


def load_manifest() -> list[dict]:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding='utf-8'))
    return []


def save_manifest(entries: list[dict]) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(entries, indent=2), encoding='utf-8')


def fixture_pdfs(page_type: str) -> list[Path]:
    """All extracted single-page fixture PDFs for one type, sorted."""
    d = FIXTURES_DIR / page_type
    return sorted(d.glob('*.pdf')) if d.exists() else []


def workdir(prefix: str = 'owmtest_') -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def make_color_pdf(path: Path, size=(1000, 1400)) -> Path:
    """Make a 1-page BRIGHT-colour PDF (orange bars on white) with NO dark pixels —
    its grayscale luminance is all >= 100. Reproduces the regression where such a
    page was mis-classified BLANK by the dark-pixel-only ink test and destroyed as
    bitonal. Should classify as PT_PHOTO_COLOR."""
    import numpy as np
    import img2pdf
    from PIL import Image
    w, h = size
    a = np.full((h, w, 3), 255, np.uint8)
    a[80:280, 80:920] = (230, 110, 60)      # luminance ~140, saturated orange
    a[360:660, 80:520] = (240, 150, 90)
    a[360:520, 560:900] = (235, 130, 80)
    jpg = path.with_suffix('.jpg')
    Image.fromarray(a).save(jpg, 'JPEG', quality=90, dpi=(200, 200))
    with open(path, 'wb') as f:
        f.write(img2pdf.convert(str(jpg), dpi=200))
    jpg.unlink(missing_ok=True)
    return path


def make_born_digital_pdf(path: Path, npages: int = 3, lines_per_page: int = 25) -> Path:
    """Hand-build a valid born-digital PDF: vector Helvetica text, NO raster images.
    Used to test the born-digital safety check (looks_born_digital / copy-through).
    Kept dependency-free (no reportlab) by writing objects + a correct xref by hand."""
    objs, font_id, nid = {}, 3, 4
    content_ids, page_ids = [], []
    for _ in range(npages):
        content_ids.append(nid); nid += 1
        page_ids.append(nid); nid += 1

    objs[1] = b'<< /Type /Catalog /Pages 2 0 R >>'
    kids = ' '.join(f'{p} 0 R' for p in page_ids)
    objs[2] = f'<< /Type /Pages /Kids [{kids}] /Count {npages} >>'.encode()
    objs[font_id] = b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'
    for i in range(npages):
        parts = [f'BT /F1 18 Tf 72 740 Td (Born-digital vector page {i + 1}) Tj ET']
        y = 700
        for j in range(lines_per_page):
            parts.append(f'BT /F1 11 Tf 72 {y} Td (Line {j + 1}: selectable vector text, '
                         f'no raster images at all.) Tj ET')
            y -= 20
        c = '\n'.join(parts).encode('latin-1')
        objs[content_ids[i]] = b'<< /Length %d >>\nstream\n' % len(c) + c + b'\nendstream'
        objs[page_ids[i]] = (
            f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
            f'/Resources << /Font << /F1 {font_id} 0 R >> >> '
            f'/Contents {content_ids[i]} 0 R >>').encode()

    buf = b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n'
    offsets = {}
    for oid in sorted(objs):
        offsets[oid] = len(buf)
        buf += f'{oid} 0 obj\n'.encode() + objs[oid] + b'\nendobj\n'
    xref_pos = len(buf)
    maxid = max(objs)
    buf += f'xref\n0 {maxid + 1}\n'.encode() + b'0000000000 65535 f \n'
    for oid in range(1, maxid + 1):
        buf += f'{offsets[oid]:010d} 00000 n \n'.encode()
    buf += f'trailer\n<< /Size {maxid + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n'.encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf)
    return path
