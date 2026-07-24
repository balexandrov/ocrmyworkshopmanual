#!/usr/bin/env python3
r"""Combine a folder of loose page images (and/or small PDFs) into ONE PDF.

For a folder whose files are the scattered pages/sections of a single manual —
e.g. `1-1.jpg 1-2.jpg ... 2a-1.jpg 2b-1.jpg ...` — this merges them, in natural
page order, into a single PDF named after the folder, written **next to** the
folder (a sibling in its parent directory). By default it then hands that PDF to
ocrmyworkshopmanual.py to compress (JBIG2/photo-aware) and add a searchable OCR
text layer.

Run it per folder, manually:

  python combine_manual.py "M:\Auto\Backup\Auto\Japan\Honda\--Engines--\Haynes_ZC_Manual"
      -> writes  ...\--Engines--\Haynes_ZC_Manual.pdf  (combined, then compressed+OCR'd)

  python combine_manual.py FOLDER --dry-run      # just show the page order, write nothing
  python combine_manual.py FOLDER --no-compress  # raw combined PDF only, skip compress/OCR
  python combine_manual.py FOLDER --language eng+rus --tessdata C:\path\to\tessdata

ONLY files directly in the folder are used (subfolders like a `*_files` HTML
asset dir are ignored), and only image/PDF types — a stray .htm/.txt is skipped.
Ordering is a NATURAL sort (so `1-2` comes before `1-11`, and `2a` before `2b`);
always eyeball the printed order before trusting it, or use --dry-run first.
"""
import argparse
import io
import os
import re
import subprocess
import sys
from pathlib import Path

import img2pdf
from PIL import Image
from pypdf import PdfWriter

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tif', '.tiff', '.webp'}
SCRIPT_DIR = Path(__file__).resolve().parent
TOOL = SCRIPT_DIR / 'ocrmyworkshopmanual.py'


def win_long(p) -> str:
    """Windows extended-length path so long paths open (no-op elsewhere)."""
    if os.name == 'nt':
        ap = os.path.abspath(str(p))
        return ap if ap.startswith('\\\\?\\') else '\\\\?\\' + ap
    return str(p)


def natkey(name: str):
    """Natural sort key: split into digit / non-digit runs, digits compared as
    ints. `re.split` always alternates (non-digit, digit, ...) starting with a
    non-digit, so every name yields the same str/int position pattern — no
    cross-type comparisons."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', name)]


def collect(folder: Path):
    """Direct image + PDF files in `folder` (non-recursive), natural-sorted."""
    files = [p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() in (IMAGE_EXTS | {'.pdf'})]
    files.sort(key=lambda p: natkey(p.name))
    return files


def _image_to_pdf_bytes(path: Path) -> bytes:
    """A one-page PDF wrapping the image. Fast path is img2pdf (embeds the JPEG
    losslessly, no re-encode); fall back to a Pillow re-wrap for images img2pdf
    rejects (alpha, palette, CMYK oddities)."""
    try:
        return img2pdf.convert(win_long(path))
    except Exception:
        im = Image.open(win_long(path))
        if im.mode in ('RGBA', 'LA', 'P'):
            im = im.convert('RGB')
        buf = io.BytesIO()
        im.save(buf, 'PNG')
        return img2pdf.convert(buf.getvalue())


def combine(files, out_pdf: Path):
    """Merge `files` (images and/or PDFs), in the given order, into out_pdf."""
    w = PdfWriter()
    for p in files:
        if p.suffix.lower() == '.pdf':
            w.append(win_long(p))
        else:
            w.append(io.BytesIO(_image_to_pdf_bytes(p)))
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with open(win_long(out_pdf), 'wb') as f:
        w.write(f)


def main():
    ap = argparse.ArgumentParser(
        description='Combine a folder of page images/PDFs into one PDF named after the folder.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('folder', type=Path, help='folder of loose page images / small PDFs')
    ap.add_argument('--dry-run', action='store_true',
                    help='print the page order and output path, then stop (write nothing)')
    ap.add_argument('--no-compress', action='store_true',
                    help='produce the raw combined PDF only; skip the compress + OCR step')
    ap.add_argument('--language', default='auto',
                    help="OCR language for the compress step (default 'auto' — per-page "
                         'script detection); passed straight to ocrmyworkshopmanual.py')
    ap.add_argument('--tessdata', default=None,
                    help='TESSDATA_PREFIX dir for OCR language packs (e.g. for rus); '
                         'else the current environment / system default is used')
    args = ap.parse_args()

    folder = args.folder
    if not folder.is_dir():
        sys.exit(f'ERROR: not a folder: {folder}')

    files = collect(folder)
    if not files:
        sys.exit(f'ERROR: no image or PDF files directly in {folder}')

    out_pdf = folder.parent / (folder.name + '.pdf')
    n_img = sum(1 for p in files if p.suffix.lower() != '.pdf')
    n_pdf = len(files) - n_img
    print(f'{len(files)} files ({n_img} image, {n_pdf} pdf) -> {out_pdf}\n')
    print('Page order:')
    for i, p in enumerate(files, 1):
        print(f'  {i:>4}. {p.name}')

    if args.dry_run:
        print('\n(--dry-run: nothing written)')
        return

    if out_pdf.exists():
        print(f'\nNOTE: overwriting existing {out_pdf.name}')
    print(f'\nCombining -> {out_pdf} ...', flush=True)
    combine(files, out_pdf)
    size_mb = out_pdf.stat().st_size / 1048576
    print(f'Combined: {len(files)} pages, {size_mb:.1f} MB')

    if args.no_compress:
        print('(--no-compress: raw combined PDF left as-is)')
        return

    env = dict(os.environ)
    if args.tessdata:
        env['TESSDATA_PREFIX'] = args.tessdata
    print(f'\nCompressing + OCR ({args.language}) in place via {TOOL.name} ...', flush=True)
    r = subprocess.run([sys.executable, str(TOOL), str(out_pdf),
                        '--in-place', '--no-log', '--language', args.language], env=env)
    if r.returncode != 0:
        sys.exit(f'ERROR: compress/OCR step failed (exit {r.returncode}); '
                 f'the raw combined PDF is still at {out_pdf}')
    print(f'\nDone -> {out_pdf}')


if __name__ == '__main__':
    main()
