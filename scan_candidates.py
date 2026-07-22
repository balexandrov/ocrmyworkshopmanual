#!/usr/bin/env python3
"""Scan a tree of PDFs and list FOLDERS that are good candidates for in-place
compression by ocrmyworkshopmanual.py.

A candidate folder is one that holds *scanned* PDFs (image-only, the kind this
tool shrinks) that would actually benefit — i.e. they are big and/or lack an OCR
text layer. Born-digital (vector/text) PDFs are detected and excluded, because
the tool copies those through untouched.

Read-only. It never renders pages (no Ghostscript); it only samples pages with
pypdf, exactly the same cheap heuristics the tool itself uses to decide
scanned-vs-born-digital (`looks_born_digital`) and has-OCR (`has_text`). So the
verdicts here match what the real run would do.

Outputs (into ./reports next to this script):
  scan_candidates.csv   one row per CANDIDATE folder, biggest first  (the ranked list)
  scan_candidates.txt   just the candidate folder paths, biggest first (feed to the tool)
  scan_all_folders.csv  one row per folder that holds >=1 PDF (full picture)
  scan_files.csv        one row per PDF (raw per-file verdicts)

Usage:
  python scan_candidates.py "M:\\Auto\\Backup\\Auto"
  python scan_candidates.py "M:\\Auto\\Backup\\Auto" --big-mb 50 --workers 8
"""
import argparse
import concurrent.futures as cf
import csv
import logging
import os
import sys
import time
from pathlib import Path

from pypdf import PdfReader

# This tree is a site-download: hundreds of thousands of files, many of them HTML
# error pages saved with a .pdf extension. pypdf screams about those on stderr.
logging.getLogger('pypdf').setLevel(logging.CRITICAL)


def _is_real_pdf(path_str) -> bool:
    """Cheap magic-byte gate: real PDFs start with '%PDF-' within the first few
    hundred bytes. HTML/JSON/broken downloads (very common in this tree) don't, and
    we skip the expensive pypdf sampling for them entirely."""
    try:
        with open(win_long(path_str), 'rb') as fh:
            head = fh.read(1024)
        return b'%PDF-' in head
    except Exception:
        return False


# ---- detection heuristics (copied verbatim from ocrmyworkshopmanual.py so the
#      verdicts match the real tool exactly) -------------------------------------

def win_long(p) -> str:
    if os.name == 'nt':
        ap = os.path.abspath(str(p))
        return ap if ap.startswith('\\\\?\\') else '\\\\?\\' + ap
    return str(p)


def has_text(pdf, sample: int = 6, min_chars: int = 40) -> bool:
    """True if the PDF already has a real text layer (sampled first pages)."""
    try:
        r = PdfReader(win_long(pdf))
        n = min(sample, len(r.pages))
        return sum(len((r.pages[i].extract_text() or '').strip()) for i in range(n)) >= min_chars
    except Exception:
        return False


def _largest_image_dpi(page) -> float:
    """Effective DPI of the LARGEST single raster image on a page. A full-page
    scan yields the scan resolution; a small logo yields a tiny number."""
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


def looks_born_digital(src_p, scan_fraction: float = 0.5,
                       sample: int = 8, min_chars: int = 100, dpi_floor: int = 50):
    """Is this a born-digital (vector/text) PDF rather than a scan? Returns
    (is_born_digital, signals_dict)."""
    sig = {'sampled': 0, 'scan_pages': 0, 'text_pages': 0, 'scan_frac': 0.0, 'chars': 0, 'pages': 0}
    try:
        r = PdfReader(win_long(src_p))
        n = len(r.pages)
    except Exception as ex:
        sig['error'] = f'unreadable ({ex})'
        return False, sig
    sig['pages'] = n
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


# ---- per-file classification (runs in a worker process) ------------------------

def classify_file(path_str):
    """Classify one PDF. Returns a flat dict (picklable across processes)."""
    p = Path(path_str)
    try:
        size = p.stat().st_size
    except Exception:
        size = 0
    if not _is_real_pdf(path_str):
        return {'path': path_str, 'folder': str(p.parent), 'size': size,
                'not_pdf': True, 'born_digital': False, 'scanned': False,
                'has_ocr': False, 'pages': 0, 'scan_frac': 0.0,
                'error': 'not a PDF (bad header)'}
    born, sig = looks_born_digital(p)
    ocr = False if born else has_text(p)   # OCR only meaningful for scans
    return {
        'path': path_str,
        'folder': str(p.parent),
        'size': size,
        'not_pdf': False,
        'born_digital': born,
        'scanned': (not born) and not sig.get('error'),
        'has_ocr': ocr,
        'pages': sig.get('pages', 0),
        'scan_frac': sig.get('scan_frac', 0.0),
        'error': sig.get('error', ''),
    }


def mb(n):
    return n / 1048576.0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('root', help='root folder to scan (recursively)')
    ap.add_argument('--big-mb', type=float, default=50.0,
                    help='a scanned PDF this many MB or larger is flagged BIG (default 50)')
    ap.add_argument('--workers', type=int, default=0,
                    help='parallel classifier processes (default: cpu count)')
    ap.add_argument('--out', default=None,
                    help='output dir for reports (default: ./reports next to this script)')
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        sys.exit(f'not a directory: {root}')
    out_dir = Path(args.out) if args.out else (Path(__file__).resolve().parent / 'reports')
    out_dir.mkdir(parents=True, exist_ok=True)
    workers = args.workers or (os.cpu_count() or 4)

    # 1) Enumerate: walk once, collect PDFs and count non-PDF files per folder.
    print(f'Enumerating {root} ...', flush=True)
    pdfs = []
    other_count = {}      # folder -> count of non-pdf files
    pdf_folders = set()
    t0 = time.time()
    for dirpath, dirnames, filenames in os.walk(root):
        n_other = 0
        for fn in filenames:
            if fn.lower().endswith('.pdf'):
                pdfs.append(os.path.join(dirpath, fn))
                pdf_folders.add(dirpath)
            else:
                n_other += 1
        other_count[dirpath] = n_other
    print(f'Found {len(pdfs)} PDFs in {len(pdf_folders)} folders '
          f'({time.time() - t0:.0f}s to enumerate).', flush=True)
    if not pdfs:
        sys.exit('No PDFs found.')

    # 2) Classify every PDF in parallel.
    files = []
    done = 0
    n = len(pdfs)
    t1 = time.time()
    with cf.ProcessPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(classify_file, pdfs, chunksize=4):
            files.append(r)
            done += 1
            if done % 50 == 0 or done == n:
                rate = done / max(1e-6, time.time() - t1)
                eta = (n - done) / max(1e-6, rate)
                print(f'  classified {done}/{n}  ({rate:.0f}/s, ETA {eta/60:.1f} min)', flush=True)

    # 3) Aggregate per folder.
    folders = {}
    for f in files:
        d = folders.setdefault(f['folder'], {
            'folder': f['folder'], 'n_pdf': 0, 'n_scanned': 0, 'n_born': 0,
            'n_err': 0, 'n_junk': 0, 'n_big': 0, 'n_no_ocr': 0, 'total_bytes': 0,
            'scanned_bytes': 0, 'compressible_bytes': 0, 'biggest_mb': 0.0,
        })
        if f.get('not_pdf'):
            d['n_junk'] += 1          # .pdf-named file that isn't a PDF (HTML etc.)
            continue
        d['n_pdf'] += 1
        d['total_bytes'] += f['size']
        d['biggest_mb'] = max(d['biggest_mb'], mb(f['size']))
        if f['error']:
            d['n_err'] += 1
        if f['born_digital']:
            d['n_born'] += 1
        if f['scanned']:
            d['n_scanned'] += 1
            d['scanned_bytes'] += f['size']
            is_big = mb(f['size']) >= args.big_mb
            no_ocr = not f['has_ocr']
            if is_big:
                d['n_big'] += 1
            if no_ocr:
                d['n_no_ocr'] += 1
            if is_big or no_ocr:
                d['compressible_bytes'] += f['size']

    for d in folders.values():
        d['n_other'] = other_count.get(d['folder'], 0)
        d['only_pdfs'] = (d['n_other'] == 0)
        reasons = []
        if d['n_big']:
            reasons.append(f"BIG({d['n_big']})")
        if d['n_no_ocr']:
            reasons.append(f"NO_OCR({d['n_no_ocr']})")
        if d['only_pdfs'] and d['n_scanned']:
            reasons.append('ONLY_PDFS')
        d['reasons'] = ' '.join(reasons)
        # Candidate = holds scanned PDFs that are big and/or missing OCR.
        d['candidate'] = d['n_scanned'] > 0 and (d['n_big'] > 0 or d['n_no_ocr'] > 0)

    # 4) Write outputs.
    all_rows = sorted((d for d in folders.values() if d['n_pdf'] > 0),
                      key=lambda d: d['scanned_bytes'], reverse=True)
    cands = [d for d in all_rows if d['candidate']]
    # rank candidates by the bytes that will actually be re-encoded, biggest first
    cands.sort(key=lambda d: (d['compressible_bytes'], d['scanned_bytes']), reverse=True)

    def folder_row(d):
        return [d['folder'], d['n_pdf'], d['n_scanned'], d['n_born'], d['n_err'],
                d['n_junk'], d['n_big'], d['n_no_ocr'], d['n_other'], int(d['only_pdfs']),
                f"{mb(d['total_bytes']):.1f}", f"{mb(d['scanned_bytes']):.1f}",
                f"{mb(d['compressible_bytes']):.1f}", f"{d['biggest_mb']:.1f}", d['reasons']]

    hdr = ['folder', 'pdfs', 'scanned', 'born_digital', 'errors', 'junk_pdf', 'big',
           'no_ocr', 'other_files', 'only_pdfs', 'total_MB', 'scanned_MB',
           'compressible_MB', 'biggest_MB', 'reasons']

    cand_csv = out_dir / 'scan_candidates.csv'
    with cand_csv.open('w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(hdr)
        for d in cands:
            w.writerow(folder_row(d))

    all_csv = out_dir / 'scan_all_folders.csv'
    with all_csv.open('w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(hdr + ['candidate'])
        for d in all_rows:
            w.writerow(folder_row(d) + [int(d['candidate'])])

    files_csv = out_dir / 'scan_files.csv'
    with files_csv.open('w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(['path', 'size_MB', 'scanned', 'born_digital', 'has_ocr',
                    'pages', 'scan_frac', 'error'])
        for f in sorted((x for x in files if not x.get('not_pdf')),
                        key=lambda x: x['size'], reverse=True):
            w.writerow([f['path'], f"{mb(f['size']):.1f}", int(f['scanned']),
                        int(f['born_digital']), int(f['has_ocr']), f['pages'],
                        f['scan_frac'], f['error']])

    feed_txt = out_dir / 'scan_candidates.txt'
    with feed_txt.open('w', encoding='utf-8') as fh:
        for d in cands:
            fh.write(d['folder'] + '\n')

    # 5) Summary to console.
    tot_scanned = sum(d['scanned_bytes'] for d in cands)
    tot_comp = sum(d['compressible_bytes'] for d in cands)
    n_junk = sum(1 for f in files if f.get('not_pdf'))
    n_real = len(files) - n_junk
    print('\n' + '=' * 70)
    print(f'Real PDFs: {n_real}   |   .pdf-named junk skipped (HTML etc.): {n_junk}')
    print(f'CANDIDATE FOLDERS: {len(cands)} (of {len(all_rows)} folders with real PDFs)')
    print(f'  scanned data in candidates:      {mb(tot_scanned)/1024:.2f} GB')
    print(f'  of that, big and/or missing OCR: {mb(tot_comp)/1024:.2f} GB')
    print('=' * 70)
    print('\nTop 25 candidates (biggest first):\n')
    print(f'{"compress_MB":>11}  {"scanned_MB":>10}  {"big":>3} {"no_ocr":>6}  reasons / folder')
    for d in cands[:25]:
        print(f'{mb(d["compressible_bytes"]):>11.0f}  {mb(d["scanned_bytes"]):>10.0f}  '
              f'{d["n_big"]:>3} {d["n_no_ocr"]:>6}  {d["reasons"]:<20} {d["folder"]}')
    print('\nWrote:')
    for p in (cand_csv, feed_txt, all_csv, files_csv):
        print(f'  {p}')
    if cands:
        print('\nBiggest candidate (start here):')
        print(f'  {cands[0]["folder"]}')


if __name__ == '__main__':
    main()
