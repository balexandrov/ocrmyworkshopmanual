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
    'color_line': owm.PT_COLOR_LINE,
}


def tools_missing() -> str | None:
    """Return a human message if a binary the tests need is absent, else None."""
    if not owm.GS:
        return 'Ghostscript not found (owm.GS is None)'
    if not owm.JBIG:
        return 'jbig2enc not found (owm.JBIG is None)'
    return None


def ocr_missing() -> str | None:
    """Return a message if the OCR toolchain is unavailable, else None. Kept separate
    from tools_missing() so tests that actually RUN OCR skip cleanly on a machine
    without Tesseract, instead of failing as if the code were broken."""
    if not owm.TESS:
        return 'Tesseract not found (owm.TESS is None)'
    if not owm.OCRMYPDF:
        return 'ocrmypdf not found'
    return None


def render_page(pdf: Path, page_no: int, dpi: int, out_png: Path) -> bool:
    """Render one page in COLOUR at `dpi`, exactly as the pipeline does: one render at
    source resolution feeds every check, so the tests must classify the same input."""
    subprocess.run(
        [owm.GS, '-sDEVICE=png16m', f'-r{dpi}',
         f'-dFirstPage={page_no}', f'-dLastPage={page_no}',
         '-dNOPAUSE', '-dBATCH', '-dQUIET',
         '-sOutputFile=' + str(out_png), owm.win_long(pdf)],
        capture_output=True)
    return out_png.exists()


def render_gray(pdf: Path, page_no: int, dpi: int, out_png: Path) -> bool:
    """Render one page of `pdf` to a grayscale PNG (used by the binarisation tests)."""
    subprocess.run(
        [owm.GS, '-sDEVICE=pnggray', f'-r{dpi}',
         f'-dFirstPage={page_no}', f'-dLastPage={page_no}',
         '-dNOPAUSE', '-dBATCH', '-dQUIET',
         '-sOutputFile=' + str(out_png), owm.win_long(pdf)],
        capture_output=True)
    return out_png.exists()


def classify(pdf: Path, page_no: int, dpi: int, work: Path,
             photo_thresh: float = 0.02, photo_dpi: int = 150):
    """Render page `page_no` the way the pipeline does — colour, at the page's own source
    resolution — then run the production classify_page() on it. Returns (PageType,
    signals_dict); signals are the cheap measurements the router keys off, recorded so a
    human can sanity-check a fixture's label."""
    pd = owm._page_render_dpi(owm._reader_page(pdf, page_no), dpi)
    png = work / f'g{page_no}.png'
    if not render_page(pdf, page_no, pd, png):
        return None, {}
    g = np.asarray(Image.open(png).convert('L'))
    signals = {
        'ink_frac': round(float((g < 100).mean()), 6),
        'photo_cov': round(owm.photo_coverage(g, pd), 4),
        'render_dpi': pd,
    }
    pc = owm.classify_page(png, page_no, pdf, work, pd, True, photo_thresh, photo_dpi)
    signals['type'] = pc.type
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


def make_color_line_pdf(path: Path, size=(1000, 1400)) -> Path:
    """Make a flat-colour LINE-ART page (mimics a colour wiring diagram): thin RED /
    GREEN / BLUE 'wires' + black text-like marks on white. Sharp edges, NO continuous
    tone -> photo_coverage stays ~0 (the bitonal gate), yet ~half the ink is genuine
    colour. This is exactly the case that must route to PT_COLOR_LINE (lossless pass-
    through) instead of being binarized to b&w. Saved lossless (PNG -> DeviceRGB)."""
    import numpy as np
    import img2pdf
    from PIL import Image
    w, h = size
    a = np.full((h, w, 3), 255, np.uint8)
    cols = [(220, 20, 20), (20, 150, 20), (30, 30, 220)]
    y, ci = 60, 0
    while y < h - 60:
        a[y:y + 3, 60:w - 60] = cols[ci % 3]          # a coloured horizontal 'wire'
        for x in range(80, w - 80, 40):               # black text-like dashes beneath it
            a[y + 12:y + 20, x:x + 24] = (0, 0, 0)
        y += 48; ci += 1
    for i, x in enumerate(range(120, w - 120, 160)):  # a few vertical coloured wires
        a[80:h - 80, x:x + 3] = cols[i % 3]
    png = path.with_suffix('.png')                     # PNG = lossless, exact colours + sharp
    Image.fromarray(a).save(png, dpi=(200, 200))
    with open(path, 'wb') as f:
        f.write(img2pdf.convert(str(png), dpi=200))
    png.unlink(missing_ok=True)
    return path


def make_scan_pdf(path: Path, npages: int = 3, dpi: int = 300) -> Path:
    """Make a multi-page 'scanned' PDF: each page is a grayscale raster of black text on
    white at `dpi` (DeviceGray image, no vector text) — i.e. a high-resolution bitonal
    scan that JBIG2 compresses hugely and whose native DPI can exceed the render floor."""
    import img2pdf
    from PIL import Image, ImageDraw, ImageFont
    from pypdf import PdfReader, PdfWriter
    wpx, hpx = round(8.5 * dpi), round(11 * dpi)
    try:
        font = ImageFont.truetype('arial.ttf', round(dpi * 0.13))
    except Exception:
        font = ImageFont.load_default()
    wr = PdfWriter()
    for i in range(npages):
        im = Image.new('L', (wpx, hpx), 255)
        d = ImageDraw.Draw(im)
        for ln in range(40):
            d.text((round(dpi * 0.5), round(dpi * 0.5) + ln * round(dpi * 0.21)),
                   f'Scan page {i + 1} line {ln}: high-resolution service manual text.',
                   fill=0, font=font)
        pg_png = path.with_name(f'{path.stem}_s{i}.png')
        im.save(pg_png, dpi=(dpi, dpi))
        pg_pdf = path.with_name(f'{path.stem}_s{i}.pdf')
        with open(pg_pdf, 'wb') as f:
            f.write(img2pdf.convert(str(pg_png), dpi=dpi))
        wr.append(str(pg_pdf))
        pg_png.unlink(missing_ok=True)
        pg_pdf.unlink(missing_ok=True)
    with open(path, 'wb') as f:
        wr.write(f)
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


def make_form_wrapped_pdf(path: Path, with_text: bool = True, img_px: int = 1200) -> Path:
    """A page whose content stream ONLY invokes a Form XObject — everything real (a big
    raster, and optionally VISIBLE vector text) lives inside the form.

    Real producers do this: measured on iText-produced Subaru manuals whose page stream is
    27 bytes, `q /Xf1 Do Q`, with all 42 text ops and every image nested one level down.
    Inspecting only the page stream saw no text and let publisher type be rasterised.
    `with_text=False` gives the opposite case — a genuine image-only wrapped scan, which
    must still be recognised as compressible."""
    import zlib

    import pikepdf
    from PIL import Image
    pdf = pikepdf.Pdf.new()
    im = Image.new('L', (img_px, round(img_px * 11 / 8.5)), 255)
    img = pikepdf.Stream(pdf, zlib.compress(im.tobytes()))
    img.Type, img.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
    img.Width, img.Height = im.width, im.height
    img.ColorSpace, img.BitsPerComponent = pikepdf.Name.DeviceGray, 8
    img.Filter = pikepdf.Name.FlateDecode
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
        BaseFont=pikepdf.Name.Helvetica, Encoding=pikepdf.Name.WinAnsiEncoding))
    body = b'q 612 0 0 792 0 0 cm /Im0 Do Q\n'
    if with_text:
        for k, y in enumerate((700, 680, 660)):
            body += (b'BT /F1 12 Tf 40 ' + str(y).encode() + b' Td ('
                     + f'Publisher vector text line {k}, real visible type not an OCR layer. '
                       .encode() + b')Tj ET\n')
    form = pikepdf.Stream(pdf, body)
    form.Type, form.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
    form.BBox = pikepdf.Array([0, 0, 612, 792])
    form.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=img),
                                        Font=pikepdf.Dictionary(F1=font))
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Contents = pikepdf.Stream(pdf, b'q /Xf1 Do Q\n')
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Xf1=form))
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(str(path))
    return path
