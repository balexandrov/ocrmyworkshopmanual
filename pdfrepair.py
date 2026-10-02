#!/usr/bin/env python3
"""
pdfrepair.py

Give pages back the named resources their content uses and their /Resources lack.

    python pdfrepair.py <file>                        # report only
    python pdfrepair.py <file> --dest DIR --apply     # write the repaired copy into DIR

WHAT IT FIXES. A manual assembled from many prints by a re-distributor can come out with pages
whose content stream draws with a font, image or graphics state its own /Resources do not hold
-- `/F4 12 Tf` on a page whose /Font has only /F5. A viewer then substitutes: measured on the
2022-2024 Honda Civic FE/FL download, page 1501 rendered as letter-shifted gibberish ("+RZ WR XVH
WKLV PDQXDO" for "How to use this manual") and lost two images, and four more pages lost their
graphics states. The pages beside such a page come from the same print run and usually define the
same name for the same thing.

WHAT IS DONE. For each name a page uses and lacks, the same name in the same category is looked
for on the nearest pages (up to `--reach` either side), and each distinct object found is TRIED on
the page. A candidate is kept only if it makes the page better by measurement, never by guess:

  * the page lacks fewer of the names its content uses than before, and
  * for a font, the page's extracted text carries more real words than before (a wrong font gives
    gibberish again, so it cannot win).

Among candidates that pass, the most words wins, then the nearest page. Nothing else on the page or
in the file changes: only the page's resource dictionary gains entries pointing at objects that are
already in the file.

WHAT IT CANNOT FIX. A page whose /Contents points at an object that is not its content (the same
Civic download: 19 pages whose content reference lands on a link annotation, a font descriptor or
a graphics state) has lost its content; nothing in the file holds it, and those pages are listed
as such. Get another copy of the manual for them.
"""
import argparse
import collections
import os
import re
import sys
import tempfile

import pikepdf
from pypdf import PdfReader

CATS = {'Tf': '/Font', 'Do': '/XObject', 'gs': '/ExtGState'}
USE = re.compile(rb'/([A-Za-z][\w.#-]*)\s+(?:[-\d.]+\s+)?(Tf|Do|gs)\b')
WORD = re.compile(r'[A-Za-z]{3,}')


def _contents(page):
    c = page.obj.get('/Contents')
    parts = c if isinstance(c, pikepdf.Array) else [c]
    if not all(isinstance(x, pikepdf.Stream) for x in parts if x is not None):
        return None                                      # content lost (points at something else)
    return b'\n'.join(x.read_bytes() for x in parts if x is not None)


def used_names(page):
    """{category: {name}} the page's content stream uses, or None if its content is not a stream."""
    data = _contents(page)
    if data is None:
        return None
    out = collections.defaultdict(set)
    for name, op in USE.findall(data):
        out[CATS[op.decode()]].add(name.decode())
    return out


def missing(page):
    used = used_names(page)
    if used is None:
        return None
    res = page.obj.get('/Resources') or pikepdf.Dictionary()
    out = {}
    for cat, names in used.items():
        have = res.get(cat)
        have = {str(k)[1:] for k in have.keys()} if isinstance(have, pikepdf.Dictionary) else set()
        lack = names - have
        if lack:
            out[cat] = lack
    return out


def _score(path, index):
    """(names the page uses and lacks, readable words it extracts) for one page of a saved file.
    Words are counted on the text pypdf extracts, which is what a reader's search finds."""
    with pikepdf.open(path) as p:
        m = missing(p.pages[index]) or {}
    try:
        text = PdfReader(path).pages[index].extract_text() or ''
    except Exception:
        text = ''
    return sum(len(v) for v in m.values()), len(WORD.findall(text))


def _kind_ok(cat, obj):
    """Is `obj` the kind of object category `cat` holds?

    A page, page-tree node or catalog is never a resource. The Civic FE download has fonts and
    graphics states that ARE pages (/F1, /G3); a page carries /Parent, so copying such a "font"
    copies the page tree and with it the whole book -- three whole copies of its 11,008 pages ended
    up in a 744-page chapter, 1.3 GB of it."""
    try:
        if isinstance(obj, pikepdf.Dictionary) and obj.get('/Type') in (pikepdf.Name.Page, pikepdf.Name.Pages,
                                                                         pikepdf.Name.Catalog):
            return False
        if cat == '/Font':
            return isinstance(obj, pikepdf.Dictionary) and not isinstance(obj, pikepdf.Stream) and \
                obj.get('/Type') in (None, pikepdf.Name.Font) and '/Subtype' in obj and '/Width' not in obj
        if cat == '/XObject':
            return isinstance(obj, pikepdf.Stream) and obj.get('/Subtype') in (pikepdf.Name.Image, pikepdf.Name.Form)
        if cat == '/ExtGState':
            return isinstance(obj, pikepdf.Dictionary) and not isinstance(obj, pikepdf.Stream)
    except Exception:
        return False
    return True


def drop_misfiled(page):
    """Remove resource entries that are the wrong kind of object for their category.

    Unused, they are simply gone (the Civic's page 1501 lists an image as font /F6: nothing draws
    with it, but text extraction reads every /Font entry and gives up on the page at an image).
    Used, the entry was never what the content meant -- a font that is a page draws nothing -- so it
    is removed and the name comes back from `missing` to be borrowed and measured like any other.
    Returns the removed (category, name, was_used) triples."""
    used = used_names(page)
    if used is None:
        return []
    res = page.obj.get('/Resources')
    out = []
    for cat in CATS.values():
        d = res.get(cat) if res is not None else None
        if not isinstance(d, pikepdf.Dictionary):
            continue
        for k in list(d.keys()):
            name = str(k)[1:]
            if not _kind_ok(cat, d[k]):
                del d[k]
                out.append((cat, name, name in used.get(cat, ())))
    return out


def _trial(page, path):
    """Score one page on a one-page copy: saving the whole book per candidate (446 MB for the
    Civic) would cost minutes each, and the page and the objects it draws with are all that matter."""
    one = pikepdf.new()
    one.pages.append(page)
    one.save(path)
    one.close()
    return _score(path, 0)


def repair(src, dest_dir=None, reach=10, apply=False, tmp_dir=None):
    pdf = pikepdf.open(src)
    n = len(pdf.pages)
    lost, todo = [], {}
    misfiled = {}
    for i, page in enumerate(pdf.pages):
        if used_names(page) is None:
            # Its /Contents is not a stream: the content is lost, and every viewer already shows the
            # page blank. An empty content stream shows exactly that and makes the page VALID, which
            # other passes need -- on the Civic, these 21 pages stopped the lossless re-store and the
            # box-space scan for the whole file ("operation for stream attempted on object of type
            # dictionary"). The page and its annotations stay; the loss is reported. Done FIRST, so
            # the wrong-kind entries below are removed from these pages too: the Civic's lost pages
            # are where its fonts-that-are-pages sat.
            page.obj.Contents = pdf.make_stream(b'')
            lost.append(i)
        dropped = drop_misfiled(page)         # every page: a wrong-kind entry is wrong wherever it is
        if dropped:
            misfiled[i] = dropped
        m = missing(page)                     # a used entry just dropped is missing now, and borrowed
        if m:
            todo[i] = m
    report = {'lost_content': lost, 'pages': {}, 'fixed': 0, 'unfixed': 0,
              'misfiled': sum(len(v) for v in misfiled.values())}
    for i, dropped in misfiled.items():
        report['pages'].setdefault(i, []).extend(
            f'{cat[1:]} {name}: removed, the wrong kind of object'
            + (' (used: borrowed below if a neighbour has it)' if was_used else ' and never used')
            for cat, name, was_used in dropped)
    tmp_dir = tmp_dir or tempfile.gettempdir()           # one-page trials; --dest may not exist yet
    trial = os.path.join(tmp_dir, '.pdfrepair-trial.pdf')
    for i, lacks in todo.items():
        page = pdf.pages[i]
        res = page.obj.get('/Resources')
        if res is None:
            page.obj.Resources = res = pikepdf.Dictionary()
        notes = []
        for cat, names in lacks.items():
            for name in sorted(names):
                key = pikepdf.Name('/' + name)
                cands, seen = [], set()
                for dist in range(1, reach + 1):
                    for j in (i - dist, i + dist):
                        if not 0 <= j < n:
                            continue
                        r2 = pdf.pages[j].obj.get('/Resources')
                        d2 = r2.get(cat) if r2 is not None else None
                        if isinstance(d2, pikepdf.Dictionary) and key in d2:
                            obj = d2[key]
                            og = obj.objgen if obj.is_indirect else None
                            # a neighbour's entry can be scrambled too: the Civic's /F1 was a page on
                            # several pages in a row, and one was borrowed as the "fix" for another
                            if og is not None and og not in seen and _kind_ok(cat, obj):
                                seen.add(og)
                                cands.append((dist, j, obj))
                if not cands:
                    notes.append(f'{cat[1:]} {name}: no page within {reach} defines it')
                    report['unfixed'] += 1
                    continue
                base = _trial(page, trial)
                best = None
                for dist, j, obj in cands:
                    sub = res.get(cat)
                    if not isinstance(sub, pikepdf.Dictionary):
                        res[cat] = sub = pikepdf.Dictionary()
                    sub[key] = obj
                    score = _trial(page, trial)
                    del sub[key]
                    better = score[0] < base[0] and (cat != '/Font' or score[1] > base[1])
                    if better and (best is None or (score[1], -dist) > (best[0][1], -best[1])):
                        best = (score, dist, j, obj)
                if best is None:
                    notes.append(f'{cat[1:]} {name}: {len(cands)} candidate(s), none made the page better')
                    report['unfixed'] += 1
                    continue
                score, dist, j, obj = best
                sub = res.get(cat)
                if not isinstance(sub, pikepdf.Dictionary):
                    res[cat] = sub = pikepdf.Dictionary()
                sub[key] = obj
                notes.append(f'{cat[1:]} {name}: taken from page {j + 1}; '
                             f'missing {base[0]} -> {score[0]}, words {base[1]} -> {score[1]}')
                report['fixed'] += 1
        report['pages'].setdefault(i, []).extend(notes)
    if os.path.exists(trial):
        os.remove(trial)
    if apply and (report['fixed'] or report['misfiled'] or lost):
        out = os.path.join(dest_dir, os.path.basename(src)) if dest_dir else src
        if dest_dir:
            os.makedirs(dest_dir, exist_ok=True)
        pdf.save(out + '.part')
        pdf.close()
        with pikepdf.open(out + '.part') as chk:                # audit: same page count
            assert len(chk.pages) == n, 'page count changed'
        os.replace(out + '.part', out)
        report['written'] = out
    return report


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:                                   # pragma: no cover
        pass
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[3].strip(),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('path')
    ap.add_argument('--apply', action='store_true', help='write the repaired copy (into --dest)')
    ap.add_argument('--dest', help='directory for the repaired copy (in place if omitted)')
    ap.add_argument('--reach', type=int, default=10, help='pages either side to borrow from (default 10)')
    a = ap.parse_args(argv)
    r = repair(a.path, a.dest, a.reach, a.apply)
    name = os.path.basename(a.path)
    for i, notes in sorted(r['pages'].items()):
        for note in notes:
            print(f'{name} p{i + 1}: {note}')
    if r['lost_content']:
        print(f'{name}: content lost on {len(r["lost_content"])} page(s), given an empty content stream '
              f'(its /Contents was not a stream; '
              f'nothing in the file holds it): {", ".join(str(i + 1) for i in r["lost_content"])}')
    print(f'{name}: {r["fixed"]} resource(s) restored, {r["unfixed"]} not'
          + (f' -> {r["written"]}' if r.get('written') else ''))


if __name__ == '__main__':
    main()
