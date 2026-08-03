#!/usr/bin/env python3
"""Build the --from-list input for a lossless-rewrite pass over born-digital PDFs.

The raster pipeline never touches a born-digital (vector/text) PDF, so every one of them
came out of past runs reported as `born digital` and copied byte-for-byte. Those rows are
already the exact inventory a lossless pass wants — no re-scan of the archive needed.

Reads any number of run-report CSVs (the `_ocrmyworkshopmanual_report_*.csv` a run writes),
keeps the born-digital rows at or above a size floor, de-duplicates by real path (an archive
gets re-run, so the same file appears in several reports), drops paths that no longer exist,
and writes one path per line — biggest first, so the long tail cannot delay the big wins.

  python helpers/lossless_candidates.py [--min-mb 50] [--out reports/lossless_list.txt]
                                        [REPORT.csv ...]

With no CSVs given it uses every _ocrmyworkshopmanual_report_*.csv in the repo root.
Writing the list changes nothing on disk; run it, read the summary, then pass the list to
the tool with --from-list ... --dest (see the README).
"""
import argparse
import csv
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

ap = argparse.ArgumentParser()
ap.add_argument('reports', nargs='*', type=Path,
                help='run-report CSVs (default: _ocrmyworkshopmanual_report_*.csv in repo root)')
ap.add_argument('--min-mb', type=float, default=50.0,
                help='size floor in MB (default 50). A rewrite saves a PERCENTAGE, so the '
                     'floor is what keeps 280,000 small files out of a pass whose whole '
                     'value sits in a few hundred big ones')
ap.add_argument('--out', type=Path, default=REPO / 'reports' / 'lossless_list.txt')
ap.add_argument('--reason', default='born digital',
                help='report `reason` value to select (default "born digital")')
ap.add_argument('--sample', type=int, default=0, metavar='N',
                help='also write a <out>.sample.txt of N files spread across FOLDERS and size '
                     'bands, for a check run before committing to the whole list. A spread, '
                     'not the N biggest: what a rewrite achieves depends on which authoring '
                     'chain produced the file, so ten manuals from one publisher would '
                     'measure one case N times')
args = ap.parse_args()

reports = args.reports or sorted(REPO.glob('_ocrmyworkshopmanual_report_*.csv'))
if not reports:
    print('ERROR: no report CSVs given and none found in the repo root', file=sys.stderr)
    raise SystemExit(1)

# path -> MB. Keyed on the resolved path so the same file listed by two runs counts once.
best, rows, skipped_small, missing = {}, 0, 0, 0
for rep in reports:
    with open(rep, newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            if (r.get('reason') or '').strip() != args.reason:
                continue
            rows += 1
            try:
                size = float(r.get('orig size (MB)') or 0)
            except ValueError:
                continue
            if size < args.min_mb:
                skipped_small += 1
                continue
            p = Path((r.get('file') or '').strip())
            if not p.name:
                continue
            # A report is read days later; the file may have moved, been deleted, or the
            # drive may not be mounted. Listing a path that is not there just burns a row
            # in the next run's report.
            if not p.is_file():
                missing += 1
                continue
            key = str(p.resolve()).lower()
            if key not in best or size > best[key][0]:
                best[key] = (size, p)

if not best:
    print(f'No {args.reason!r} files at or above {args.min_mb:.0f} MB in '
          f'{len(reports)} report(s).')
    raise SystemExit(0)

ordered = sorted(best.values(), key=lambda x: -x[0])
total = sum(s for s, _ in ordered)
args.out.parent.mkdir(parents=True, exist_ok=True)
with open(args.out, 'w', encoding='utf-8') as f:
    f.write(f'# {len(ordered)} born-digital PDFs >= {args.min_mb:.0f} MB, '
            f'{total / 1024:.1f} GB total, biggest first\n')
    f.write(f'# from: {", ".join(r.name for r in reports)}\n')
    for _s, p in ordered:
        f.write(str(p) + '\n')

sample_path = None
if args.sample > 0:
    # Group one level ABOVE the file's own folder, then cycle groups taking the biggest from
    # each. What a rewrite achieves is decided by the authoring chain that produced the file,
    # and in a vehicle archive the model folder tracks that far better than the leaf does:
    # grouping by leaf folder puts each model YEAR in its own group, so a marque with ten
    # years of manuals swamps the sample with ten measurements of one authoring chain.
    by_folder = {}
    for s, p in ordered:
        parent = p.parent
        key = parent.parent if parent.parent != parent else parent
        by_folder.setdefault(str(key).lower(), []).append((s, p))
    picked, round_no = [], 0
    while len(picked) < min(args.sample, len(ordered)):
        added = False
        for files in by_folder.values():
            if round_no < len(files) and len(picked) < args.sample:
                picked.append(files[round_no])
                added = True
        if not added:
            break
        round_no += 1
    sample_path = args.out.with_suffix('.sample.txt')
    with open(sample_path, 'w', encoding='utf-8') as f:
        f.write(f'# CHECK RUN: {len(picked)} of {len(ordered)} candidates, '
                f'{sum(s for s, _ in picked) / 1024:.1f} GB, spread across folders\n')
        for _s, p in sorted(picked, key=lambda x: -x[0]):
            f.write(str(p) + '\n')

print(f'Reports read      : {len(reports)}')
print(f'{args.reason!r} rows : {rows}  ({skipped_small} under {args.min_mb:.0f} MB, '
      f'{missing} no longer on disk)')
print(f'Candidates        : {len(ordered)} unique files, {total / 1024:.1f} GB')
print(f'List written      : {args.out}')
if sample_path:
    print(f'Check-run list    : {sample_path}')
print()
print('Largest:')
for s, p in ordered[:10]:
    print(f'  {s:8.1f} MB  {p}')
print()
print('Next:  python ocrmyworkshopmanual.py --from-list '
      f'"{args.out}" --dest <OUTPUT_ROOT> --no-ocr')
