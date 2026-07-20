#!/usr/bin/env python3
"""
extract_fixtures.py — build the page-type test corpus from REAL scanned PDFs.

Point it at one or more real scanned PDFs (or folders of them). It samples pages,
runs the production classify_page() on each, and picks up to N pages per PageType,
preferring pages from DIFFERENT source files so a type isn't overfit to one
scanner/manual. Each chosen page is copied out as a self-contained single-page PDF
into tests/fixtures/<type>/ and recorded in tests/fixtures/manifest.json with its
provenance and the measured signals.

    python tests/extract_fixtures.py "M:\\scans"                     # auto-pick from a tree
    python tests/extract_fixtures.py a.pdf b.pdf --per-type 5        # from specific files
    python tests/extract_fixtures.py --add "a.pdf:37:photo_color"    # force a known page in
    python tests/extract_fixtures.py --list                         # show current corpus

IMPORTANT: auto-picked labels come from the classifier that these fixtures are
meant to TEST — so they are CANDIDATES, not ground truth. Open each extracted PDF,
confirm the label is right, and set "verified": true for it in manifest.json (or
just delete/re-file any wrong one). test_classify.py flags unverified fixtures.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from pypdf import PdfReader, PdfWriter

import _util as U
from _util import TYPE_DIRS, FIXTURES_DIR


def iter_sources(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            out += sorted(pp.rglob('*.pdf'))
        elif pp.suffix.lower() == '.pdf' and pp.exists():
            out.append(pp)
        else:
            print(f'  ! skipping (not a PDF/folder): {p}')
    return out


def sample_pages(n: int, k: int) -> list[int]:
    """Up to k page numbers (1-based) spread evenly across an n-page file."""
    if n <= k:
        return list(range(1, n + 1))
    return sorted({round(i * (n - 1) / (k - 1)) + 1 for i in range(k)})


def extract_page(src: Path, page_no: int, dest_dir: Path) -> Path:
    """Copy one page of `src` out to a self-contained single-page PDF."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    r = PdfReader(str(src))
    w = PdfWriter()
    w.add_page(r.pages[page_no - 1])
    dest = dest_dir / f'{src.stem[:40]}_p{page_no:04d}.pdf'
    with open(dest, 'wb') as f:
        w.write(f)
    return dest


def add_one(src: Path, page_no: int, page_type: str, dpi: int, entries: list[dict]) -> None:
    """Extract a single explicitly-named page and record it (label forced by user)."""
    if page_type not in TYPE_DIRS:
        print(f'  ! unknown type {page_type!r} (use one of {list(TYPE_DIRS)})')
        return
    work = U.workdir()
    try:
        _, signals = U.classify(src, page_no, dpi, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    dest = extract_page(src, page_no, FIXTURES_DIR / page_type)
    entries.append({
        'file': str(dest.relative_to(FIXTURES_DIR)).replace('\\', '/'),
        'type': page_type, 'source': str(src), 'source_page': page_no,
        'signals': signals, 'classifier_said': signals.get('type'),
        'verified': True, 'note': 'manually added',
    })
    flag = '' if signals.get('type') == TYPE_DIRS[page_type] else \
        f'  <-- WARNING: classifier said {signals.get("type")!r}, you forced {page_type!r}'
    print(f'  + {dest.name}  [{page_type}]{flag}')


def auto_pick(sources: list[Path], per_type: int, dpi: int, sample_k: int,
              entries: list[dict]) -> None:
    """Sample+classify across all sources, then choose per_type pages per type,
    round-robin across distinct source files for diversity."""
    already = {(e['source'], e['source_page']) for e in entries}
    # candidates[type] = list of (src, page_no, signals)
    candidates: dict[str, list] = {t: [] for t in TYPE_DIRS}
    for src in sources:
        try:
            n = len(PdfReader(str(src)).pages)
        except Exception as ex:
            print(f'  ! unreadable, skipping {src.name}: {ex}')
            continue
        work = U.workdir()
        try:
            for pg in sample_pages(n, sample_k):
                if (str(src), pg) in already:
                    continue
                t, signals = U.classify(src, pg, dpi, work)
                if t in candidates:
                    candidates[t].append((src, pg, signals))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        print(f'  scanned {src.name}: '
              + ', '.join(f'{t}={len(candidates[t])}' for t in TYPE_DIRS))

    for t, cands in candidates.items():
        picked = _round_robin_distinct(cands, per_type)
        if not picked:
            print(f'  [{t}] no candidates found in the given sources')
            continue
        for src, pg, signals in picked:
            dest = extract_page(src, pg, FIXTURES_DIR / t)
            entries.append({
                'file': str(dest.relative_to(FIXTURES_DIR)).replace('\\', '/'),
                'type': t, 'source': str(src), 'source_page': pg,
                'signals': signals, 'classifier_said': t,
                'verified': False, 'note': 'auto-picked — CONFIRM label then set verified:true',
            })
        srcs = len({s for s, _, _ in picked})
        print(f'  [{t}] picked {len(picked)} page(s) from {srcs} distinct source file(s)')


def _round_robin_distinct(cands: list, want: int) -> list:
    """Pick up to `want` items, cycling through distinct source files first so we
    don't take 5 pages from the same manual when others are available."""
    by_src: dict[Path, list] = {}
    for c in cands:
        by_src.setdefault(c[0], []).append(c)
    picked: list = []
    while len(picked) < want and any(by_src.values()):
        for src in list(by_src):
            if by_src[src]:
                picked.append(by_src[src].pop(0))
                if len(picked) >= want:
                    break
    return picked


def list_corpus() -> None:
    entries = U.load_manifest()
    if not entries:
        print('No fixtures yet. Run: python tests/extract_fixtures.py <scanned.pdf ...>')
        return
    by_type: dict[str, list] = {}
    for e in entries:
        by_type.setdefault(e['type'], []).append(e)
    for t in TYPE_DIRS:
        rows = by_type.get(t, [])
        v = sum(1 for e in rows if e.get('verified'))
        print(f'{t:12s} {len(rows)} fixture(s), {v} verified')
        for e in rows:
            mark = 'OK ' if e.get('verified') else '?? '
            s = e.get('signals', {})
            print(f'   {mark}{e["file"]:34s} ink={s.get("ink_frac")!s:8s} '
                  f'cov={s.get("photo_cov")!s:7s} said={e.get("classifier_said")}')


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('sources', nargs='*', help='scanned PDF files and/or folders')
    ap.add_argument('--per-type', type=int, default=5, help='pages per type (default 5)')
    ap.add_argument('--dpi', type=int, default=200, help='classify/extract dpi (default 200)')
    ap.add_argument('--sample-per-file', type=int, default=25,
                    help='pages sampled per source file when auto-picking (default 25)')
    ap.add_argument('--add', action='append', default=[], metavar='PDF:PAGE:TYPE',
                    help='force one known page into the corpus (repeatable)')
    ap.add_argument('--list', action='store_true', help='print the current corpus and exit')
    ap.add_argument('--reset', action='store_true',
                    help='delete all existing fixtures + manifest before extracting')
    args = ap.parse_args()

    if args.list:
        list_corpus()
        return

    err = U.tools_missing()
    if err:
        print(f'ERROR: {err}. classify/extract need Ghostscript.', file=sys.stderr)
        sys.exit(1)

    if args.reset and FIXTURES_DIR.exists():
        for t in TYPE_DIRS:
            shutil.rmtree(FIXTURES_DIR / t, ignore_errors=True)
        (FIXTURES_DIR / 'manifest.json').unlink(missing_ok=True)
        print('reset: cleared existing fixtures')

    entries = U.load_manifest()

    for spec in args.add:
        try:
            pdf_s, pg_s, t = spec.rsplit(':', 2)
        except ValueError:
            print(f'  ! bad --add {spec!r}, expected PDF:PAGE:TYPE'); continue
        add_one(Path(pdf_s), int(pg_s), t, args.dpi, entries)

    if args.sources:
        srcs = iter_sources(args.sources)
        print(f'{len(srcs)} source PDF(s) to scan @ {args.dpi} dpi, '
              f'{args.sample_per_file} pages sampled each, {args.per_type} per type\n')
        auto_pick(srcs, args.per_type, args.dpi, args.sample_per_file, entries)

    if not args.add and not args.sources:
        ap.print_help()
        return

    U.save_manifest(entries)
    print(f'\nmanifest -> {U.MANIFEST}')
    print('NEXT: open each fixture PDF, confirm its label, set "verified": true in the manifest.')


if __name__ == '__main__':
    main()
