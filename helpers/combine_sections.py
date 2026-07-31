#!/usr/bin/env python3
r"""Combine each SECTION folder of a split manual into one PDF named after the folder,
verify it, and (with --delete) remove the folder it came from.

A "split manual" here is a root whose immediate subfolders are the manual's sections,
each holding the section's pages as many small PDFs — flat or one level deeper again:

    USDM Impreza FSM 1995\BODY SECTION\*.pdf                    (flat)
    USDM Forester FSM 2006\BODY SECTION\AIRBAG SYSTEM AB\*.pdf   (nested)

  ->  USDM Forester FSM 2006\BODY SECTION.pdf     (sibling, inside the root)

The ROOT is never deleted — only the section folders that were successfully combined.
So a root ends up holding one PDF per section, and any loose files already sitting at
the root (standalone guides, index.htm) are left exactly where they are.

  python helpers/combine_sections.py ROOTS.txt --dry-run
  python helpers/combine_sections.py ROOTS.txt --delete
  python helpers/combine_sections.py ROOTS.txt --delete --skip-unrecoverable

ROOTS.txt is one root folder per line (blank lines and #comments ignored).

A damaged part is REPAIRED first (qpdf, then Ghostscript — the same path the main tool
uses), and the repair must recover the page count the file's raw bytes say it had, so a
partial salvage cannot pass itself off as complete. If a part stays broken the section is
refused, unless --skip-unrecoverable is given: then it is combined without that part, the
part is moved to "<SECTION> (UNRECOVERABLE)\" so it is never destroyed, and the row says
which files and how many pages were left out. Every unrecoverable file is named by its
path RELATIVE TO THE SECTION, because one real section holds two different
`General Description.pdf` files and a bare filename cannot tell them apart.

DELETION IS GATED on all of these passing, because a merge is where pages vanish
silently and a short PDF looks no different from a complete one:
  * every input PDF is readable, and the combined file reopens
  * combined page count == the exact sum of the inputs' page counts
  * combined bytes >= MIN_SIZE_RATIO x the inputs' bytes — a cheap PROXY only. If it
    trips, WORD RECALL against the inputs decides: a manual printed from the web merges
    to ~0.83 of its input bytes with every word intact, so size alone condemned a
    perfect merge. Recall below MIN_WORD_RECALL is what actually fails the section.
  * of SAMPLE_PAGES pages spread through the result, none is blank (neither text nor
    an image) — a page can merge as an empty page without changing the count
Any failure leaves the section folder and its PDF untouched and is reported.

Resumable: a section whose output PDF already exists is skipped, so a re-run continues
where it stopped. Progress goes to reports/combine_sections.csv after every section.
"""
import argparse
import csv
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import combine_manual as CM                                    # noqa: E402
from pypdf import PdfReader                                     # noqa: E402

# Combined bytes as a fraction of the inputs' bytes. Merging copies page streams through,
# so on a scanned manual the ratio sits at ~1.00 (measured 0.996-1.007 across flat, nested
# and 714-file sections). It is a cheap PROXY for "did we lose content", not the property
# itself — see `word_recall` below for what happens when it trips.
MIN_SIZE_RATIO = 0.90

# Fraction of the inputs' words that must survive into the combined PDF when the size proxy
# trips. A lossless concatenation scores 1.0; this is the real check, so it is strict.
MIN_WORD_RECALL = 0.98
SAMPLE_PAGES = 8


def _page_has_content(pg) -> bool:
    """Text or an image on this page. A merge can produce a structurally valid but EMPTY
    page, which the page-count check cannot see — this is what catches that."""
    try:
        if len((pg.extract_text() or '').strip()) > 0:
            return True
    except Exception:
        pass
    try:
        res = pg.get_inherited('/Resources') or pg.get('/Resources')
        xo = (res.get_object().get('/XObject') or {})
        return any(o.get_object().get('/Subtype') == '/Image'
                   for _n, o in xo.get_object().items())
    except Exception:
        return False


def blank_sampled(pdf: Path, k: int = SAMPLE_PAGES) -> tuple:
    """(pages checked, how many were blank) over pages spread across the whole file."""
    try:
        r = PdfReader(str(pdf))
    except Exception:
        return 0, 0
    n = len(r.pages)
    if not n:
        return 0, 0
    idxs = sorted({round(i * (n - 1) / max(1, k - 1)) for i in range(min(k, n))})
    bad = 0
    for i in idxs:
        try:
            if not _page_has_content(r.pages[i]):
                bad += 1
        except Exception:
            bad += 1
    return len(idxs), bad


def word_recall(inputs: list, out: Path) -> float:
    """Fraction of the inputs' words present in the combined PDF (1.0 = all of them).

    This is what the size ratio was only ever standing in for. That proxy is calibrated on
    scanned manuals, where merging copies page streams through and the ratio lands at ~1.00;
    but a manual captured by PRINTING an online one arrives as many small browser PDFs that
    each carry their own catalogue, metadata and xref, and pypdf writes the merge more
    compactly. Measured on a real Outlander section: 0.829 of the input bytes with word
    recall 1.0000, all 60 images and all 13 embedded font programs intact — nothing missing
    at all. Rejecting that on size alone threw away a perfect merge, so when the proxy trips
    the actual content is compared instead.

    Order-independent (multiset of words), so it does not care how the pages were sequenced.
    Reuses the main tool's `_words` so 'a word' means the same thing here as in its audit."""
    import ocrmyworkshopmanual as owm
    from collections import Counter

    def count(pdf: Path) -> Counter:
        c = Counter()
        try:
            rd = PdfReader(str(pdf))
        except Exception:
            return c
        for pg in rd.pages:
            try:
                c.update(owm._words(pg.extract_text()))
            except Exception:
                pass
        return c

    want = Counter()
    for p in inputs:
        want.update(count(p))
    if not want:
        return 1.0                    # nothing extractable to compare (pure image scans)
    got = count(out)
    return sum(min(n, got[w]) for w, n in want.items()) / sum(want.values())


def drop_self_combined(sec: Path, files: list) -> tuple:
    """(files without any already-combined copy of the folder itself, [names dropped]).

    One section in this archive ships both its parts and a merge of them:
    `WIRING DIAGRAM SECTION\\` holds nine part PDFs in subfolders totalling 146 pages AND
    a top-level `Wiring_diagram.pdf` that is those same 146 pages already merged. Combining
    everything under the folder would emit every page twice.

    The test is EXACT page-count identity against the subfolders' total, not a ratio: a
    40%-of-total heuristic flagged 31 genuine chapters (a 19-page `Pre-delivery
    Inspection.pdf` in a section whose subfolders hold 43 pages) and only one real
    duplicate, which sat at exactly 1.00."""
    subs = [p for p in sec.iterdir() if p.is_dir()]
    if not subs:
        return files, []
    sub_pages = 0
    for s in subs:
        w, _bad = CM.expected_pages(CM.collect(s, recursive=True))
        sub_pages += w
    if not sub_pages:
        return files, []
    keep, dropped = [], []
    for p in files:
        if (p.parent == sec and CM.is_pdf(p)
                and CM.page_count(p) == sub_pages):
            dropped.append(p.name)
        else:
            keep.append(p)
    return keep, dropped


def sections_of(root: Path) -> list:
    """The root's immediate subfolders, natural-sorted — the manual's sections."""
    return sorted([p for p in root.iterdir() if p.is_dir()],
                  key=lambda p: CM.natkey(p.name))


def process_section(sec: Path, root: Path, delete: bool, dry: bool,
                    skip_broken: bool = False, order: str = 'natural') -> dict:
    """Combine one section folder -> <root>\\<section>.pdf, verify, optionally delete
    the folder. Returns a row for the progress CSV; never raises."""
    row = {'root': str(root), 'section': sec.name, 'status': '', 'files': 0, 'pages': 0,
           'src_MB': 0.0, 'out_MB': 0.0, 'ratio': '', 'blank_sampled': '',
           'unrecoverable': '', 'pages_dropped': '', 'order': order,
           'docid_missing': '', 'word_recall': '',
           'lost_other_files': '', 'deleted': 'no', 'seconds': 0, 'detail': ''}
    t0 = time.time()
    out = root / (sec.name + '.pdf')
    # `detail` accumulates: a note made early (an ignored self-copy, a repaired part) must
    # survive whatever verdict is recorded later, or the reason the numbers look the way
    # they do is lost from the very row that reports them.
    notes = []

    def detail(*more):
        row['detail'] = '; '.join(notes + [m for m in more if m])
        return row

    try:
        # NOT CM.dedupe_pages, on purpose. combine_manual drops a redundant low-resolution
        # rescan from its PDF and leaves the file on disk, so a wrong call there costs a
        # reprint. Here --delete rmtree's the section afterwards, so the same wrong call
        # DESTROYS the file. A ratio gate calibrated on one measured folder (26 pairs at
        # 4.0-16.9x) is evidence enough to leave a page out of a PDF, and not evidence enough
        # to delete it. Wiring it in later also means fixing the src_MB/merge_b denominators
        # (the dropped bytes would be charged to the merge and trip MIN_SIZE_RATIO), giving
        # the drops their own CSV column rather than letting `lost_other_files` swallow them,
        # and making an ambiguous group block --delete. That is its own change.
        files = CM.collect(sec, recursive=True)
        files, self_dup = drop_self_combined(sec, files)
        if self_dup:
            notes.append('ignored already-combined copy inside the folder: '
                         + ', '.join(self_dup))
        # ordering comes AFTER the self-copy check, which compares page counts and does not
        # care about sequence, and after `collect`, so discovery and ordering stay separate
        if order == 'docid':
            files, missing = CM.order_by_docid(files)
            row['docid_missing'] = missing
            if missing:
                row['order'] = 'natural'
                notes.append(f'{missing} part(s) carry no publisher doc id, so doc-id '
                             f'order was NOT applied to this section')
        row['files'] = len(files)
        # anything in the folder that is NOT a page we are carrying over would be lost
        # with the folder; name it in the report rather than deleting it silently
        collected = set(files)
        others = [p for p in sec.rglob('*') if p.is_file() and p not in collected]
        row['lost_other_files'] = '; '.join(p.name for p in others[:5]) + \
                                  (f' (+{len(others) - 5} more)' if len(others) > 5 else '')
        if not files:
            row['status'] = 'SKIP'
            return detail('no image/PDF files under it')
        src_b = sum(p.stat().st_size for p in files)
        row['src_MB'] = round(src_b / 1048576, 2)

        # An output that is already there is not automatically trustworthy, and not
        # automatically wrong either: several of these roots were combined by another tool
        # in 2019, leaving <SECTION>.pdf beside <SECTION>/. VERIFY it against the folder
        # instead of blindly skipping — if it is a faithful merge the folder is redundant
        # and can go, which is the whole point of the run.
        if out.exists():
            want, bad = CM.expected_pages(files)
            got = CM.page_count(out)
            row['pages'] = got
            row['out_MB'] = round(out.stat().st_size / 1048576, 2)
            row['ratio'] = round(out.stat().st_size / src_b, 3) if src_b else ''
            if bad:
                row['status'] = 'CONFLICT'
                return detail(f'{out.name} exists; cannot verify it — '
                              f'{len(bad)} unreadable input(s)')
            if got != want:
                row['status'] = 'CONFLICT'
                return detail(f'{out.name} exists but has {got} pages vs {want} in the '
                              f'folder — not a complete merge, left alone')
            # size ratio is deliberately NOT checked here: another tool made this file and
            # may have optimised it (measured 0.877 on one real section), so only the page
            # count and a blank-page sample can be trusted as evidence of completeness.
            k, blanks = blank_sampled(out)
            row['blank_sampled'] = f'{blanks}/{k}'
            if blanks:
                row['status'] = 'CONFLICT'
                return detail(f'{out.name} exists but {blanks}/{k} sampled pages are blank')
            row['status'] = 'ALREADY'
            detail(f'{out.name} already matches the folder ({got} pages)')
            if dry:
                return row
            if delete:
                shutil.rmtree(sec)
                row['deleted'] = 'yes'
            return row

        if dry:
            want, bad = CM.expected_pages(files)
            row['pages'] = want
            row['status'] = 'WOULD-COMBINE' if not bad else 'WOULD-REPAIR'
            row['unrecoverable'] = '; '.join(str(b.relative_to(sec)) for b in bad)
            return detail('' if not bad else
                          f'{len(bad)} unreadable input(s) — would try qpdf/Ghostscript '
                          f'repair' + (', then skip any that stay broken'
                                       if skip_broken else ''))

        # repair any unreadable part first, in a scratch dir, so one malformed page does
        # not strand a whole section as un-combinable
        work = Path(tempfile.mkdtemp(prefix='combsec_'))
        try:
            files, reps = CM.repair_inputs(files, work, base=sec)
            for r in reps:
                notes.append(f'{r.rel}: ' + (
                    f'repaired ({r.recovered} pages)' if r.status == 'repaired'
                    else f'UNRECOVERABLE — only {r.recovered} of ~{r.expected} pages '
                         f'can be salvaged' if r.status == 'incomplete'
                    else 'UNRECOVERABLE — nothing could be read out of it'))
            broken = [r for r in reps if r.status != 'repaired']
            row['unrecoverable'] = '; '.join(str(r.rel) for r in broken)
            row['pages_dropped'] = sum(r.expected or 0 for r in broken) if broken else ''
            if broken and skip_broken:
                # Combine what IS readable rather than leaving the manual split forever —
                # but the damaged originals are moved out BEFORE the folder is deleted, so
                # skipping a page never destroys the only copy of it. A better tool, or a
                # cleaner download, can still be tried on them later.
                bad_set = {r.src for r in broken}
                files = [p for p in files if p not in bad_set]
                if not files:
                    row['status'] = 'FAILED'
                    return detail('every input is unrecoverable — nothing to combine')
                keep_dir = root / (sec.name + ' (UNRECOVERABLE)')
                for r in broken:
                    dest = keep_dir / r.rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(r.src), str(dest))
                notes.append(f'{len(broken)} unrecoverable part(s) moved to '
                             f'{keep_dir.name}\\ and left out of the PDF')
            # The size ratio must be measured against what was ACTUALLY merged. qpdf and
            # Ghostscript rewrite a repaired part smaller, so charging the merge for the
            # original bytes made a sound section look like content loss (measured: a Baja
            # transmission section with 3 repaired parts came out at 0.852 against the
            # 0.90 floor, while its page count verified exactly).
            merge_b = sum(p.stat().st_size for p in files)
            # A section split into per-topic subfolders needs an outline as much as a combined
            # manual does; `section_bookmarks` returns {} for a flat one, so those outputs are
            # byte-identical to before. Outline items are not page text, so `word_recall` is
            # unaffected, and the extra bytes can only push `ratio` up, never below the floor.
            pages = CM.combine(files, out,          # raises CombineFailed on page loss
                               bookmarks=CM.section_bookmarks(files, sec))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        row['pages'] = pages
        out_b = out.stat().st_size
        row['out_MB'] = round(out_b / 1048576, 2)
        ratio = out_b / merge_b if merge_b else 0
        row['ratio'] = round(ratio, 3)
        # A rejected result must not be left on disk. Otherwise a re-run finds it, takes the
        # `out.exists()` path — which deliberately does NOT re-check the ratio, since that
        # path is for files another tool made — and quietly accepts what this run refused.
        if ratio < MIN_SIZE_RATIO:
            # The proxy tripped; check the thing it stands in for before condemning a file.
            recall = word_recall(files, out)
            row['word_recall'] = round(recall, 4)
            if recall < MIN_WORD_RECALL:
                row['status'] = 'FAILED'
                out.unlink(missing_ok=True)
                return detail(f'combined is only {ratio:.3f} of the merged inputs\' bytes '
                              f'AND word recall is {recall:.4f} '
                              f'(floor {MIN_WORD_RECALL}) — content is missing')
            notes.append(f'smaller than expected ({ratio:.3f} of input bytes) but word '
                         f'recall {recall:.4f} — a more compact rewrite, nothing lost')
        k, blanks = blank_sampled(out)
        row['blank_sampled'] = f'{blanks}/{k}'
        if blanks:
            row['status'] = 'FAILED'
            out.unlink(missing_ok=True)
            return detail(f'{blanks} of {k} sampled pages are blank')
        row['status'] = 'OK-PARTIAL' if row['unrecoverable'] else 'OK'
        detail()
        if delete:
            shutil.rmtree(sec)
            row['deleted'] = 'yes'
        return row
    except CM.CombineFailed as ex:
        out.unlink(missing_ok=True)
        row['status'] = 'FAILED'
        return detail(str(ex))
    except Exception as ex:
        row['status'] = 'FAILED'
        return detail(f'{type(ex).__name__}: {ex}')
    finally:
        row['seconds'] = round(time.time() - t0)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('roots', type=Path, help='file listing one root folder per line')
    ap.add_argument('--delete', action='store_true',
                    help='DELETE each section folder after its combined PDF passes every '
                         'check (default: keep everything)')
    ap.add_argument('--order', choices=('natural', 'docid'), default='natural',
                    help="page order within each section. 'natural' (default) sorts by "
                         "filename, right when the parts are numbered. 'docid' reads the "
                         "publisher document id from each PDF's page-1 browser print header "
                         '— for a manual captured by printing an online one, whose parts are '
                         'named by topic so a filename sort is arbitrary. Per section it is '
                         'all-or-nothing: unless EVERY part has a doc id that section keeps '
                         'natural order, and the `order` / `docid_missing` columns say which '
                         'was used')
    ap.add_argument('--skip-unrecoverable', action='store_true',
                    help='combine a section even if some parts are damaged beyond repair, '
                         'leaving those parts OUT of the PDF instead of refusing the whole '
                         'section. The damaged originals are MOVED to '
                         '"<SECTION> (UNRECOVERABLE)\\" beside the PDF first, so nothing is '
                         'destroyed and a better tool can be tried on them later; the row '
                         'is reported as OK-PARTIAL with the files and page count dropped')
    ap.add_argument('--dry-run', action='store_true',
                    help='report what would happen; write and delete nothing')
    ap.add_argument('--progress', type=Path,
                    default=REPO / 'reports' / 'combine_sections.csv')
    ap.add_argument('--pdf-list', type=Path,
                    default=REPO / 'reports' / 'combined_pdfs.txt',
                    help='where to write the list of produced PDFs, for the '
                         'compress/OCR step (ocrmyworkshopmanual --from-list)')
    args = ap.parse_args()

    roots = [Path(l.strip()) for l in args.roots.read_text(encoding='utf-8').splitlines()
             if l.strip() and not l.lstrip().startswith('#')]
    missing = [r for r in roots if not r.is_dir()]
    if missing:
        for m in missing:
            print(f'ERROR: not a folder: {m}', file=sys.stderr)
        sys.exit(1)
    if args.delete and args.dry_run:
        sys.exit('ERROR: --delete and --dry-run are contradictory; pick one')

    args.progress.parent.mkdir(parents=True, exist_ok=True)
    new = not args.progress.exists()
    fh = args.progress.open('a', newline='', encoding='utf-8')
    cols = ['root', 'section', 'status', 'files', 'pages', 'src_MB', 'out_MB', 'ratio',
            'blank_sampled', 'word_recall', 'order', 'docid_missing', 'unrecoverable',
            'pages_dropped', 'lost_other_files', 'deleted', 'seconds', 'detail']
    w = csv.DictWriter(fh, fieldnames=cols)
    if new:
        w.writeheader()
        fh.flush()

    made, tally = [], {}
    t_all = time.time()
    print(f'{len(roots)} root(s)'
          + ('  [DRY RUN — nothing written]' if args.dry_run else
             '  [WILL DELETE combined section folders]' if args.delete else
             '  [combine only, nothing deleted]'), flush=True)
    for i, root in enumerate(roots, 1):
        secs = sections_of(root)
        print(f'\n[{i}/{len(roots)}] {root}   ({len(secs)} sections)', flush=True)
        for sec in secs:
            row = process_section(sec, root, args.delete, args.dry_run,
                                  skip_broken=args.skip_unrecoverable, order=args.order)
            w.writerow(row)
            fh.flush()
            tally[row['status']] = tally.get(row['status'], 0) + 1
            if row['status'] in ('OK', 'OK-PARTIAL', 'ALREADY'):
                made.append(root / (sec.name + '.pdf'))
            mark = {'OK': 'ok', 'OK-PARTIAL': 'part', 'ALREADY': 'have', 'SKIP': '--',
                    'FAILED': 'FAIL', 'CONFLICT': 'CONF'}.get(row['status'], '??')
            extra = f"  del={row['deleted']}" if not args.dry_run else ''
            print(f"   {mark:4} {row['files']:>4}f -> {row['pages']:>5}p "
                  f"{row['src_MB']:>7.1f}->{row['out_MB']:>7.1f}MB "
                  f"r={row['ratio']} blank={row['blank_sampled']}{extra}  {sec.name[:44]}"
                  + (f"  | {row['detail']}" if row['detail'] else ''), flush=True)
            if row['lost_other_files']:
                print(f"        non-page files inside it: {row['lost_other_files']}",
                      flush=True)
    fh.close()

    if made:
        args.pdf_list.parent.mkdir(parents=True, exist_ok=True)
        args.pdf_list.write_text('\n'.join(str(p) for p in made) + '\n', encoding='utf-8')

    print(f'\nDONE in {(time.time() - t_all) / 60:.1f} min: '
          + ', '.join(f'{v} {k}' for k, v in sorted(tally.items())))
    print(f'Progress: {args.progress}')
    if made:
        print(f'Produced {len(made)} PDF(s); list -> {args.pdf_list}')
        print(f'Next:  python ocrmyworkshopmanual.py --from-list "{args.pdf_list}" '
              f'--language eng')
    if tally.get('CONFLICT'):
        print(f'{tally["CONFLICT"]} section(s) have an existing PDF that does NOT match the '
              f'folder — nothing was written or deleted for those; filter status=CONFLICT')
    if tally.get('FAILED'):
        print(f'\n{tally["FAILED"]} section(s) FAILED — their folders were NOT deleted; '
              f'filter status=FAILED in {args.progress.name}')
        sys.exit(1)


if __name__ == '__main__':
    main()
