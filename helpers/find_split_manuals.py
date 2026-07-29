#!/usr/bin/env python3
r"""Find manuals that were published as many small PDFs instead of one document, and say
which are safe to consolidate with `combine_sections.py`.

READ-ONLY. Nothing in the scanned tree is written, moved or deleted.

  python helpers/find_split_manuals.py "D:\archive"
  python helpers/find_split_manuals.py <tree> --csv reports/split_manuals.csv
  python helpers/find_split_manuals.py <tree> --sections     # list each root's sections

TWO detection signals, because either alone misses real manuals:
  A) the folder's NAME contains a marker (default `usdm`, see --name-contains)
  B) it has >= 2 immediate subfolders whose names contain `SECTION`

Signal B found every Subaru manual, whose sections are literally named `BODY SECTION`,
`DIAGNOSTICS SECTION`; but that convention is Subaru's alone — a Mitsubishi Outlander
manual of the identical shape names its sections `ABS`, `AC`, `AT`, `CAN`, and is only
found by signal A. Keep both.

The VERDICT is the point. Consolidating deletes the source folders, so telling these apart
before touching anything is what makes the run safe:

  SPLIT              many small parts under section-like subfolders — combinable
  CONTAINER          the subfolders are MODEL YEARS or variants, not sections. Combining
                     is still possible but means one PDF per year, a different decision
                     (real case: `1998-2009 D22 Frontier USDM` -> 1998, 1999 KA, 1999 VG…)
  ALREADY-SECTIONED  parts are already big (>= --big-part-mb) — probably one PDF per
                     section already; nothing to combine
  HTML-DUMP          more non-PDF files than PDFs: a browsable HTML manual whose pages and
                     figures live outside the PDFs. Combining and deleting DESTROYS it
  THIN               too few PDFs to be a split manual

Sizes are MB. `median_part_MB` is the discriminator that matters: a manual split into
pages sits well under 1 MB per file, one already consolidated sits in the tens.
"""
import argparse
import csv
import os
import re
import statistics
import sys
from pathlib import Path

# a child folder that looks like a model year or year range, not a section:
# '1998', '2010', '1999 KA', '2013-2014', '11_GS45X'
_YEARISH = re.compile(r'^\s*(19|20)\d{2}\b')


def tree_stats(d: Path) -> tuple:
    """(pdf count, other-file count, total bytes, pdf sizes, max depth below d)."""
    npdf = nother = nbytes = 0
    sizes = []
    depth = 0
    for cur, _dirs, files in os.walk(d):
        try:
            depth = max(depth, len(Path(cur).relative_to(d).parts))
        except ValueError:
            pass
        for f in files:
            try:
                sz = (Path(cur) / f).stat().st_size
            except OSError:
                sz = 0
            nbytes += sz
            if f.lower().endswith('.pdf'):
                npdf += 1
                sizes.append(sz)
            else:
                nother += 1
    return npdf, nother, nbytes, sizes, depth


def classify(root: Path, sections: list, loose_pdfs: int, npdf: int, nother: int,
             sizes: list, depth: int, min_pdfs: int, big_part_mb: float) -> tuple:
    """(verdict, reason) for one candidate root."""
    med = statistics.median(sizes) / 1048576 if sizes else 0.0
    if npdf < min_pdfs:
        return 'THIN', f'only {npdf} PDFs beneath it'
    if nother > npdf:
        return 'HTML-DUMP', (f'{nother} non-PDF files vs {npdf} PDFs — a browsable HTML '
                             f'manual; combining and deleting would destroy it')
    if not sections:
        return ('ALREADY-SECTIONED', f'no subfolders; {npdf} PDFs sit directly in it')
    if med >= big_part_mb:
        return 'ALREADY-SECTIONED', (f'median part {med:.2f} MB — parts are already '
                                     f'section-sized, nothing to combine')
    yearish = sum(1 for s in sections if _YEARISH.match(s.name))
    if yearish >= max(2, len(sections) // 2):
        return 'CONTAINER', (f'{yearish} of {len(sections)} subfolders look like model '
                             f'years — combining would give one PDF per year, not per '
                             f'section')
    return 'SPLIT', f'{len(sections)} sections, median part {med:.2f} MB'


def find(tree: Path, marker: str, min_pdfs: int, big_part_mb: float) -> list:
    out = []
    for cur, dirs, files in os.walk(tree):
        cur_p = Path(cur)
        by_name = marker in cur_p.name.lower()
        by_shape = sum(1 for d in dirs if 'section' in d.lower()) >= 2
        if not (by_name or by_shape):
            continue
        npdf, nother, nbytes, sizes, depth = tree_stats(cur_p)
        if not npdf:
            continue
        sections = sorted((cur_p / d for d in dirs), key=lambda p: p.name.lower())
        loose = sum(1 for f in files if f.lower().endswith('.pdf'))
        verdict, reason = classify(cur_p, sections, loose, npdf, nother, sizes, depth,
                                   min_pdfs, big_part_mb)
        out.append({
            'verdict': verdict, 'root': str(cur_p), 'sections': len(sections),
            'pdfs': npdf, 'loose_pdfs_at_root': loose, 'other_files': nother,
            'total_MB': round(nbytes / 1048576, 1),
            'median_part_MB': round(statistics.median(sizes) / 1048576, 3) if sizes else 0,
            'depth': depth,
            'matched': ('name+shape' if (by_name and by_shape)
                        else 'name' if by_name else 'shape'),
            'reason': reason,
        })
        # do not descend: this root's sections are not separate manuals
        dirs[:] = []
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('tree', type=Path, help='folder tree to scan (read-only)')
    ap.add_argument('--csv', type=Path, default=None, help='also write the table here')
    ap.add_argument('--name-contains', default='usdm', metavar='STR',
                    help="signal A: folder-name marker, case-insensitive (default 'usdm'; "
                         "use '' to rely on the SECTION shape alone)")
    ap.add_argument('--min-pdfs', type=int, default=10,
                    help='fewer PDFs than this is reported THIN (default 10)')
    ap.add_argument('--big-part-mb', type=float, default=3.0,
                    help='median part size at or above which a root is judged already '
                         'one-PDF-per-section (default 3)')
    ap.add_argument('--sections', action='store_true',
                    help="also list every root's subfolders with their own counts")
    args = ap.parse_args()

    if not args.tree.is_dir():
        sys.exit(f'ERROR: not a folder: {args.tree}')

    rows = find(args.tree, args.name_contains.lower(), args.min_pdfs, args.big_part_mb)
    order = ['SPLIT', 'CONTAINER', 'ALREADY-SECTIONED', 'HTML-DUMP', 'THIN']
    rows.sort(key=lambda r: (order.index(r['verdict']) if r['verdict'] in order else 9,
                             -r['total_MB']))

    if not rows:
        print(f'no candidate split manuals under {args.tree}')
        return

    for v in order:
        sel = [r for r in rows if r['verdict'] == v]
        if not sel:
            continue
        print(f'\n### {v}: {len(sel)} root(s), {sum(r["pdfs"] for r in sel):,} PDFs, '
              f'{sum(r["total_MB"] for r in sel)/1024:.2f} GB')
        for r in sel:
            rel = str(Path(r['root'])).replace(str(args.tree) + os.sep, '')
            print(f'  {r["pdfs"]:>5}p {r["total_MB"]:>8.1f}MB sec={r["sections"]:>3} '
                  f'med={r["median_part_MB"]:>6.2f}MB d={r["depth"]} '
                  f'[{r["matched"]:10}] {rel}')
            print(f'        {r["reason"]}')
            if args.sections:
                for s in sorted((Path(r['root']) / d)
                                for d in os.listdir(r['root'])
                                if (Path(r['root']) / d).is_dir()):
                    n, _no, nb, szs, dep = tree_stats(s)
                    m = statistics.median(szs) / 1048576 if szs else 0
                    print(f'          {n:>4}p {nb/1048576:>7.1f}MB med={m:>5.2f}MB '
                          f'd={dep}  {s.name}')

    combinable = [r for r in rows if r['verdict'] == 'SPLIT']
    print(f'\n{len(rows)} candidate root(s); {len(combinable)} look SPLIT and combinable '
          f'({sum(r["pdfs"] for r in combinable):,} PDFs, '
          f'{sum(r["total_MB"] for r in combinable)/1024:.2f} GB)')
    if combinable:
        print('Feed the SPLIT roots to helpers/combine_sections.py (one path per line); '
              'run it with --dry-run first.')

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f'-> {args.csv}')


if __name__ == '__main__':
    main()
