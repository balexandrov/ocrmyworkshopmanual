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

By default ONLY files directly in the folder are used (subfolders like a `*_files`
HTML asset dir are ignored), and only image/PDF types — a stray .htm/.txt is
skipped. With `--recursive`, subfolders are walked too, which is what a manual
split into per-topic subfolders needs:

  BODY SECTION\AIRBAG SYSTEM AB\1. General Description.pdf
  BODY SECTION\AIRBAG SYSTEM AB\2. Airbag Connector.pdf
  BODY SECTION\BODY STRUCTURE BS\1. General Description.pdf   -> one BODY SECTION.pdf

Ordering is a NATURAL sort (so `1-2` comes before `1-11`, and `2a` before `2b`);
always eyeball the printed order before trusting it, or use --dry-run first.

The combined PDF is VERIFIED before this exits: it must open and carry exactly the
sum of its inputs' page counts. That check is what makes it safe to delete the
source folder afterwards — merging is where pages go missing silently, since a
short PDF looks no different from a complete one.
"""
import argparse
import io
import os
import re
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import img2pdf
from PIL import Image
from pypdf import PdfReader, PdfWriter

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


def looks_like_pdf(p: Path) -> bool:
    """True if the file BEGINS with a PDF header, whatever its name says.

    Not paranoia: one page in this archive is a genuine 1-page PDF stored as a file
    literally named `null` (five stray newlines, then `%PDF-1.3`), inside a Wiring Diagram
    Section. Keying off the extension alone dropped it from its section — and this tool
    then deletes the folder, so that page would have been gone. Leading whitespace is
    tolerated because that is exactly how these files are malformed; the header must still
    be the first real bytes, so a text file that merely mentions %PDF does not match."""
    try:
        with open(win_long(p), 'rb') as f:
            head = f.read(1024)
    except OSError:
        return False
    return head.lstrip(b'\r\n \t\x00').startswith(b'%PDF')


def is_pdf(p: Path) -> bool:
    """A page-bearing PDF: by extension, or by content for a misnamed one."""
    return p.suffix.lower() == '.pdf' or looks_like_pdf(p)


def collect(folder: Path, recursive: bool = False):
    """Image + PDF files in `folder`, in natural page order.

    Non-recursive (default): direct files only. With `recursive`: subfolders are walked
    depth-first, and at EVERY level files and subfolders are ordered together by the same
    natural key — so a subfolder takes its pages' place in the sequence rather than being
    appended at the end. That is what a mixed section needs, where numbered intro pages
    sit beside a subfolder that continues them:

        1. Foreword.pdf  …  8. Pre-delivery Inspection.pdf
        PERIODIC MAINTENANCE SERVICES PM\\1. …          <- comes after 8., as printed
    """
    out = []

    def walk(d: Path):
        try:
            entries = sorted(d.iterdir(), key=lambda p: natkey(p.name))
        except OSError as ex:
            print(f'  WARNING: cannot list {d}: {ex}', file=sys.stderr)
            return
        for p in entries:
            if p.is_dir():
                if recursive:
                    walk(p)
            elif p.suffix.lower() in (IMAGE_EXTS | {'.pdf'}) or looks_like_pdf(p):
                out.append(p)

    walk(folder)
    return out


def page_count(pdf: Path) -> int:
    """Pages in a PDF, or -1 if it cannot be read."""
    try:
        return len(PdfReader(win_long(pdf)).pages)
    except Exception:
        return -1


def expected_pages(files) -> tuple:
    """(total pages the combined PDF must have, list of unreadable inputs).

    An image is one page; a PDF contributes its own count. Unreadable inputs are
    returned rather than skipped: silently dropping one produces a short PDF that looks
    perfectly valid, and the whole point of the count is to catch exactly that."""
    total, bad = 0, []
    for p in files:
        if not is_pdf(p):
            total += 1                     # an image is one page
            continue
        n = page_count(p)
        if n < 0:
            bad.append(p)
        else:
            total += n
    return total, bad


_PAGE_OBJ = re.compile(rb'/Type\s*/Page(?![s/\w])')


def pages_in_raw_bytes(pdf: Path) -> int:
    """Lower bound on a PDF's page count, read from its RAW BYTES — usable when the file
    is too broken for a parser to open at all.

    Counts `/Type /Page` object dictionaries (never `/Type /Pages`, the tree node). This
    UNDER-counts a PDF that keeps its page dicts in compressed object streams, which is
    the safe direction: it can only ever make the partial-salvage check below more
    lenient, never make it reject a sound repair."""
    try:
        return len(_PAGE_OBJ.findall(pdf.read_bytes()))
    except OSError:
        return 0


Repair = namedtuple('Repair', 'src rel status recovered expected')
"""One input's repair outcome. `status` is:
     'repaired'   - fully recovered, safe to merge
     'incomplete' - a repair worked but salvaged FEWER pages than the original held
     'failed'     - nothing could be read out of it at all
`rel` is the path relative to the section, because a bare filename is ambiguous: one real
section holds `Clutch System\\General Description.pdf` AND
`Control Systems\\General Description.pdf`, and a report naming only 'General
Description.pdf' twice cannot tell you which one is broken."""


def repair_inputs(files, work: Path, base: Path = None) -> tuple:
    """Repair the unreadable PDFs among `files`, reusing ocrmyworkshopmanual's qpdf-then-
    Ghostscript repair. Returns (files with repaired copies substituted, [Repair, ...]).

    Refusing a whole section over one malformed part would leave the manual split forever,
    so a recoverable part is recovered. But a repair is NOT taken at face value: the page
    count that guards the merge is computed from the files handed to it, so a repair that
    salvaged 1 page of 9 would agree with itself and pass, and the section's source folder
    would then be deleted. Measured on a real Baja transmission section — three truncated
    parts holding 3, 9 and 6 page objects, from each of which qpdf salvaged exactly one
    page: ~15 pages would have been lost silently.

    So a repair must recover at least as many pages as the raw bytes say the original had.
    When it cannot, the part is left unreadable on purpose — `expected_pages` still reports
    it and `combine` still refuses — but HOW SHORT the salvage was is measured and returned,
    because 'recovered 1 of ~9 pages' is what tells you whether the file is worth chasing,
    and it is what --skip-unrecoverable needs in order to say what a section is missing."""
    import ocrmyworkshopmanual as owm
    out, results = [], []
    for i, p in enumerate(files):
        if not is_pdf(p) or page_count(p) >= 0:
            out.append(p)
            continue
        rel = p.relative_to(base) if base else Path(p.name)
        want = pages_in_raw_bytes(p)
        sub = work / f'rep{i:05d}'
        sub.mkdir(parents=True, exist_ok=True)
        # STRICT first — exactly what the main tool does, but told how many pages to expect
        # so it rejects a partial salvage instead of quietly returning a shortened file.
        rep = owm._repair_pdf(p, sub, expect_pages=want)
        got = page_count(Path(rep)) if rep else -1
        if rep and got >= 0 and (not want or got >= want):
            out.append(Path(rep))
            results.append(Repair(p, rel, 'repaired', got, want))
            continue
        # It failed the strict bar. Repeat WITHOUT the expectation — the main tool's own
        # leniency — purely to measure what is actually recoverable, so the report can say
        # how much of the file is left rather than just 'failed'.
        sub2 = work / f'sal{i:05d}'
        sub2.mkdir(parents=True, exist_ok=True)
        sal = owm._repair_pdf(p, sub2)
        sgot = page_count(Path(sal)) if sal else -1
        out.append(p)                           # unusable — let the count refuse it
        results.append(Repair(p, rel, 'incomplete' if sgot > 0 else 'failed',
                             max(sgot, 0), want))
    return out, results


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


class CombineFailed(Exception):
    """The combined PDF is not a faithful merge of its inputs — do not trust it, and
    above all do not delete the sources on the strength of it."""


def combine(files, out_pdf: Path, verify: bool = True) -> int:
    """Merge `files` (images and/or PDFs), in the given order, into out_pdf. Returns the
    page count. Raises CombineFailed if an input cannot be read or merged, or if the
    result does not carry exactly the sum of the inputs' pages.

    Written to a .part first and moved into place only once verified, so a failure never
    leaves a half-written PDF that looks like a finished one."""
    want, bad = (0, []) if not verify else expected_pages(files)
    if bad:
        raise CombineFailed(f'{len(bad)} unreadable input(s), first: {bad[0].name}')
    w = PdfWriter()
    for p in files:
        try:
            if is_pdf(p):
                w.append(win_long(p))
            else:
                w.append(io.BytesIO(_image_to_pdf_bytes(p)))
        except Exception as ex:
            raise CombineFailed(f'{p.name}: {type(ex).__name__}: {ex}') from ex
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_pdf.with_suffix(out_pdf.suffix + '.part')
    try:
        with open(win_long(tmp), 'wb') as f:
            w.write(f)
        got = page_count(tmp)
        if got < 0:
            raise CombineFailed('the combined PDF cannot be reopened')
        if verify and got != want:
            raise CombineFailed(f'page loss: inputs total {want} pages, combined has {got}')
        os.replace(win_long(tmp), win_long(out_pdf))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return got


def main():
    ap = argparse.ArgumentParser(
        description='Combine a folder of page images/PDFs into one PDF named after the folder.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('folder', type=Path, help='folder of loose page images / small PDFs')
    ap.add_argument('--recursive', action='store_true',
                    help='also walk subfolders (for a manual split into per-topic '
                         'subfolders); files and subfolders are ordered together by the '
                         'same natural key at every level')
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

    files = collect(folder, recursive=args.recursive)
    if not files:
        sys.exit(f'ERROR: no image or PDF files {"under" if args.recursive else "directly in"} '
                 f'{folder}' + ('' if args.recursive else
                                ' (its files may be in subfolders — try --recursive)'))

    out_pdf = folder.parent / (folder.name + '.pdf')
    n_img = sum(1 for p in files if not is_pdf(p))
    n_pdf = len(files) - n_img
    print(f'{len(files)} files ({n_img} image, {n_pdf} pdf) -> {out_pdf}\n')
    print('Page order:')
    for i, p in enumerate(files, 1):
        # under --recursive the bare name is ambiguous (every subfolder has a
        # `1. General Description.pdf`), so show the path relative to the folder
        label = p.relative_to(folder) if args.recursive else p.name
        print(f'  {i:>4}. {label}')

    if args.dry_run:
        want, bad = expected_pages(files)
        print(f'\nWould produce {want} pages'
              + (f' — WARNING: {len(bad)} unreadable input(s)' if bad else ''))
        for p in bad:
            print(f'    unreadable: {p}')
        print('(--dry-run: nothing written)')
        return

    if out_pdf.exists():
        print(f'\nNOTE: overwriting existing {out_pdf.name}')
    print(f'\nCombining -> {out_pdf} ...', flush=True)
    try:
        pages = combine(files, out_pdf)
    except CombineFailed as ex:
        sys.exit(f'ERROR: combine refused: {ex}\n'
                 f'       {folder} is untouched — do NOT delete it.')
    size_mb = out_pdf.stat().st_size / 1048576
    print(f'Combined: {len(files)} files -> {pages} pages, {size_mb:.1f} MB (page count verified)')

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
