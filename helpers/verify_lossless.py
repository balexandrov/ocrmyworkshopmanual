#!/usr/bin/env python3
"""Audit a LOSSLESS rewrite pass: prove that nothing a page can draw left the files.

    python helpers/verify_lossless.py --before <SRC_ROOT> --after <OUT_ROOT>
                                      [--out reports/lossless_audit.csv] [--workers N]

Pairs are matched by path relative to each root, walking the AFTER tree (the source root may be
a whole archive). One row per pair; a non-zero exit means at least one file FAILED.

WHAT THIS ASKS THAT THE RUN'S OWN GUARD DOES NOT. The guard checks that everything which should
still be there IS there — counts, page fingerprints, decoded content bytes. It never asks the
opposite question: *of everything that disappeared, what was it?* Those are different questions,
and the second is the one that caught the bug that mattered: an early version of the rewrite
dropped 9,898 streams nobody had accounted for while page count, annotation count, bookmark
count and every sampled page fingerprint matched.

TWO THINGS MAKE THE NAIVE VERSION OF THIS TEST WRONG, both learned by being wrong:

1. Streams must be paired by the SHA-1 of their DECODED bytes, because qpdf renumbers objects —
   but that COLLAPSES DUPLICATES. A file may hold the same font program twice, once live and
   once orphaned; qpdf drops the orphan, and a naive "this hash lost a copy" reads that as
   losing a font. The only sound question per hash is: *were there more REACHABLE copies in the
   source than there are copies at all in the output?* Anything unreachable from the trailer
   cannot be drawn by any page, annotation or resource, so omitting it is correct — real
   archive PDFs are full of such dead objects from incremental updates.

2. The document-level XMP packet legitimately changes. pikepdf renormalises it on save, and
   when the source's packet is unparseable (measured: 2014_Forester.pdf carries 3,360 bytes of
   binary with no xpacket marker at all) pikepdf replaces it with a valid empty one. Reported
   as `xmp_replaced`, not as loss — nothing readable was there to lose.

It deliberately does NOT import ocrmyworkshopmanual. An auditor that reuses the code it audits
cannot catch that code being wrong — the same reason verify_run.py judges colour from rendered
pixels rather than calling the pipeline's own colour test.
"""
import argparse
import collections
import concurrent.futures as cf
import csv
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pikepdf

GS = (os.environ.get('JBIG2_GS') or shutil.which('gswin64c') or shutil.which('gs')
      or r'C:\Program Files\gs\gs10.07.1\bin\gswin64c.exe')

# qpdf regenerates these on every save, so they are never "the same stream" across a rewrite.
GENERATED = {'/ObjStm', '/XRef'}
# A linearization hint stream carries no page content: /S is required, the rest are offsets into
# the first-page / shared-object tables. Identified by shape, since it has no /Type.
HINT_KEYS = {'/S', '/O', '/E', '/L', '/I', '/T', '/A', '/V', '/C', '/R', '/B', '/Length',
             '/Filter', '/DecodeParms'}


def _is_xmp(head: bytes) -> bool:
    return b'<?xpacket' in head or b'<x:xmpmeta' in head or b'<rdf:RDF' in head


def _reachable(pdf) -> set:
    """Every indirect object reachable from the trailer. Same definition qpdf prunes by."""
    seen, stack = set(), [pdf.trailer]
    while stack:
        obj = stack.pop()
        try:
            if isinstance(obj, (pikepdf.Dictionary, pikepdf.Stream)):
                items = [v for _k, v in obj.items()]
            elif isinstance(obj, pikepdf.Array):
                items = list(obj)
            else:
                continue
        except Exception:
            continue
        for v in items:
            if isinstance(v, (pikepdf.Dictionary, pikepdf.Stream, pikepdf.Array)):
                og = v.objgen
                if og != (0, 0):
                    if og in seen:
                        continue
                    seen.add(og)
                stack.append(v)
    return seen


def _survey(path: Path, want_reach: bool):
    """(total counts, reachable counts, {hash: (head, keys, size)}, catalog-XMP hash)."""
    total, reach, info = collections.Counter(), collections.Counter(), {}
    cat = None
    with pikepdf.open(str(path)) as p:
        live = _reachable(p) if want_reach else set()
        m = p.Root.get('/Metadata')
        if isinstance(m, pikepdf.Stream):
            try:
                cat = hashlib.sha1(m.read_bytes()).hexdigest()
            except Exception:
                cat = None
        for o in p.objects:
            if not isinstance(o, pikepdf.Stream):
                continue
            if str(o.get('/Type') or '') in GENERATED:
                continue
            try:
                d = o.read_bytes()
            except Exception:
                d = b'<UNDECODABLE>'
            h = hashlib.sha1(d).hexdigest()
            total[h] += 1
            if h not in info:
                info[h] = (d[:64], {str(k) for k in o.keys()}, len(d))
            if want_reach and o.objgen in live:
                reach[h] += 1
    return total, reach, info, cat


def _render_hash(pdf: Path, page: int, dpi: int) -> str:
    """md5 of one page's RAW pixels. ppmraw, not PNG: no encoder, no metadata, nothing that
    could differ for a reason other than the pixels themselves."""
    out = subprocess.run([GS, '-sDEVICE=ppmraw', f'-r{dpi}',
                          f'-dFirstPage={page}', f'-dLastPage={page}',
                          '-dNOPAUSE', '-dBATCH', '-dQUIET', '-sOutputFile=-', str(pdf)],
                         capture_output=True, timeout=900)
    if out.returncode != 0 or not out.stdout:
        return f'ERR:{out.returncode}'
    return hashlib.md5(out.stdout).hexdigest()


def _render_pages(n: int, want: int) -> list:
    """Spread the sampled pages across the document, including both ends."""
    if n <= want:
        return list(range(1, n + 1))
    return sorted({1, n, *(1 + round(i * (n - 1) / (want - 1)) for i in range(want))})


def _text_hash(pdf: Path, page: int) -> str:
    """md5 of one page's extracted text, via poppler's pdftotext.

    A SECOND, independent library matters for two reasons. It is not the library the rewrite
    used (so it cannot share a blind spot with it), and it reaches pages Ghostscript cannot:
    gs 10.07.1 dies with exit 255 on -dFirstPage >= 1022 in a PDF with a FLAT page tree
    (upstream bug 709436). That is a property of the FILE, not of the rewrite — measured on
    Toyota.Camry.1991.RM.pdf, 2,389 kids directly under /Root/Pages, where the SOURCE fails
    at page 1022 exactly as the output does. Without this check, every deep page in such a
    file would be unjudgeable."""
    try:
        out = subprocess.run(['pdftotext', '-f', str(page), '-l', str(page), str(pdf), '-'],
                             capture_output=True, timeout=900)
    except Exception:
        return 'ERR'
    if out.returncode != 0:
        return 'ERR'
    return hashlib.md5(out.stdout).hexdigest()


def compare_renders(pair, want: int, dpi: int):
    """Do the sampled pages still come out the same? For a LOSSLESS pass that means
    pixel-IDENTICAL, not merely similar — unlike a lossy re-encode, where only 'did colour
    survive' can be asked.

    Returns (rel, pixels_same, pixels_judged, text_same, text_judged, problems). A page whose
    render fails in BOTH versions is INCONCLUSIVE, never a failure: identical failure is
    evidence about the renderer, not about the rewrite, and reporting it as damage is the kind
    of false alarm that gets an auditor ignored. Only a real DIFFERENCE is a problem."""
    b, a, rel = pair
    try:
        with pikepdf.open(str(a)) as p:
            n = len(p.pages)
    except Exception as ex:
        return rel, 0, 0, 0, 0, [f'could not open output: {repr(ex)[:80]}']
    pages = _render_pages(n, want)
    px_same = px_judged = tx_same = tx_judged = 0
    problems = []
    for pg in pages:
        hb, ha = _render_hash(b, pg, dpi), _render_hash(a, pg, dpi)
        if hb.startswith('ERR') and ha.startswith('ERR'):
            pass                                   # inconclusive — neither side renders
        elif hb.startswith('ERR') or ha.startswith('ERR'):
            px_judged += 1
            problems.append(f'p{pg}: renders in only ONE version ({hb} / {ha})')
        else:
            px_judged += 1
            if hb == ha:
                px_same += 1
            else:
                problems.append(f'p{pg}: pixels differ')
        tb, ta = _text_hash(b, pg), _text_hash(a, pg)
        if tb == 'ERR' and ta == 'ERR':
            pass
        elif tb == 'ERR' or ta == 'ERR':
            tx_judged += 1
            problems.append(f'p{pg}: text extracts from only ONE version')
        else:
            tx_judged += 1
            if tb == ta:
                tx_same += 1
            else:
                problems.append(f'p{pg}: text differs')
    return rel, px_same, px_judged, tx_same, tx_judged, problems


def audit(pair):
    b, a, rel = pair
    row = {'file': rel, 'verdict': '', 'detail': '',
           'xmp': 0, 'hint': 0, 'orphans': 0, 'lost': 0, 'added': 0,
           'MB before': 0.0, 'MB after': 0.0, '%': '', 'xmp_replaced': ''}
    try:
        row['MB before'] = round(b.stat().st_size / 1048576, 2)
        row['MB after'] = round(a.stat().st_size / 1048576, 2)
        row['%'] = round(100 * row['MB after'] / row['MB before']) if row['MB before'] else ''
        s_tot, s_reach, s_info, s_cat = _survey(b, True)
        o_tot, _o_reach, _o_info, o_cat = _survey(a, False)
    except Exception as ex:
        row['verdict'] = 'FAILED'
        row['detail'] = f'could not read: {repr(ex)[:110]}'
        return row

    lost = []
    for h, r in s_reach.items():
        deficit = r - o_tot.get(h, 0)
        if deficit <= 0:
            continue
        head, keys, _size = s_info[h]
        if _is_xmp(head):
            row['xmp'] += deficit
        elif h == s_cat:
            # The source's own document packet, gone because pikepdf rewrote it. Unparseable
            # in the source if it does not even carry an xpacket marker.
            row['xmp_replaced'] = 'unparseable in source' if not _is_xmp(head) else 'renormalised'
        elif '/S' in keys and keys <= HINT_KEYS:
            row['hint'] += deficit
        else:
            lost.append((deficit, _size, repr(head[:40])))
            row['lost'] += deficit
    # Copies that vanished but were NOT reachable in the source: dead objects, correctly pruned.
    # A linearization hint stream is always one of these — it hangs off the linearization
    # parameter dictionary, not off the trailer — so it has to be counted here rather than in
    # the reachable pass above, or the `hint` column reads 0 on every linearized file.
    accounted = row['xmp'] + row['lost'] + (1 if row['xmp_replaced'] else 0)
    for h in s_tot:
        deficit = s_tot[h] - o_tot.get(h, 0) - max(0, s_reach.get(h, 0) - o_tot.get(h, 0))
        if deficit <= 0:
            continue
        head, keys, _size = s_info[h]
        if '/S' in keys and keys <= HINT_KEYS and not _is_xmp(head):
            row['hint'] += deficit
    row['orphans'] = (sum(max(0, s_tot[h] - o_tot.get(h, 0)) for h in s_tot)
                      - accounted - row['hint'])
    row['added'] = sum(n for h, n in (o_tot - s_tot).items() if h != o_cat)

    if lost:
        row['verdict'] = 'FAILED'
        row['detail'] = ('reachable content lost: '
                         + '; '.join(f'{n}x {sz}B {hd}' for n, sz, hd in lost[:3]))
    elif row['added']:
        row['verdict'] = 'FAILED'
        row['detail'] = f'{row["added"]} unexplained stream(s) added'
    else:
        row['verdict'] = 'ok'
        bits = []
        if row['xmp']:
            bits.append(f'{row["xmp"]} XMP packets')
        if row['hint']:
            bits.append('linearization hint')
        if row['orphans']:
            bits.append(f'{row["orphans"]} dead objects pruned')
        if row['xmp_replaced']:
            bits.append(f'doc XMP {row["xmp_replaced"]}')
        row['detail'] = ', '.join(bits) or 'compression only, nothing removed'
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--before', required=True, type=Path)
    ap.add_argument('--after', required=True, type=Path)
    ap.add_argument('--out', type=Path, default=Path('reports/lossless_audit.csv'))
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--render', type=int, default=0, metavar='N',
                    help='also render N pages per file with Ghostscript in BOTH versions and '
                         'compare raw pixels. The structural pass proves no object was lost; '
                         'this proves the pages still look identical, judged by a renderer that '
                         're-interprets the file from scratch. 0 = skip')
    ap.add_argument('--render-dpi', type=int, default=50)
    args = ap.parse_args()

    pairs = []
    for a in sorted(p for p in args.after.rglob('*')
                    if p.is_file() and p.suffix.lower() == '.pdf'):
        rel = a.relative_to(args.after)
        b = args.before / rel
        if b.is_file():
            pairs.append((b, a, str(rel)))
    if not pairs:
        sys.exit('no matching pairs found')
    print(f'Auditing {len(pairs)} pair(s) with {args.workers} worker(s)...', flush=True)

    rows = []
    with cf.ProcessPoolExecutor(max_workers=args.workers) as ex:
        for i, row in enumerate(ex.map(audit, pairs), 1):
            rows.append(row)
            print(f'  [{i}/{len(pairs)}] {row["verdict"]:6} {row["file"][:66]}  '
                  f'{row["detail"][:64]}', flush=True)

    pix_bad = []
    if args.render:
        print(f'\nRendering {args.render} page(s) per file at {args.render_dpi} dpi in both '
              f'versions...', flush=True)
        by_rel = {r['file']: r for r in rows}
        with cf.ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = [ex.submit(compare_renders, p, args.render, args.render_dpi) for p in pairs]
            for i, fut in enumerate(cf.as_completed(futs), 1):
                rel, pxs, pxj, txs, txj, problems = fut.result()
                r = by_rel.get(rel, {})
                r['pixels'] = f'{pxs}/{pxj}'
                r['text'] = f'{txs}/{txj}'
                if problems:
                    pix_bad.append((rel, problems))
                    r['verdict'] = 'FAILED'
                    r['detail'] = (r.get('detail', '') + ' | RENDER: ' + '; '.join(problems[:2]))
                print(f'  [{i}/{len(futs)}] {"ok    " if not problems else "FAILED"} '
                      f'pixels {pxs}/{pxj} text {txs}/{txj}  {rel[:56]}', flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    cols = ['file', 'MB before', 'MB after', '%', 'xmp', 'hint', 'orphans', 'lost', 'added',
            'xmp_replaced', 'pixels', 'text', 'verdict', 'detail']
    with open(args.out, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in sorted(rows, key=lambda x: x['file'].lower()):
            w.writerow({k: r.get(k, '') for k in cols})

    tb = sum(r['MB before'] for r in rows)
    ta = sum(r['MB after'] for r in rows)
    bad = [r for r in rows if r['verdict'] != 'ok']
    print(f'\n{len(rows)} pairs: {tb / 1024:.1f} GB -> {ta / 1024:.1f} GB '
          f'(saved {(tb - ta) / 1024:.1f} GB)')
    print(f'XMP packets removed : {sum(r["xmp"] for r in rows)}')
    print(f'hint streams        : {sum(r["hint"] for r in rows)}')
    print(f'dead objects pruned : {sum(r["orphans"] for r in rows)}')
    print(f'doc XMP replaced    : {sum(1 for r in rows if r["xmp_replaced"])} file(s)')
    print(f'REACHABLE CONTENT LOST: {sum(r["lost"] for r in rows)}')
    if args.render:
        def _tot(col, idx):
            return sum(int((r.get(col) or '0/0').split('/')[idx]) for r in rows)
        print(f'PAGES PIXEL-IDENTICAL : {_tot("pixels", 0)}/{_tot("pixels", 1)} judged')
        print(f'PAGES TEXT-IDENTICAL  : {_tot("text", 0)}/{_tot("text", 1)} judged  (poppler)')
        print(f'files with a real difference: {len(pix_bad)}')
    print(f'Report: {args.out}')
    if bad:
        print(f'\nFAILED {len(bad)} file(s):')
        for r in bad:
            print(f'  {r["file"]}: {r["detail"]}')
        sys.exit(1)
    print('\nALL PAIRS OK — nothing reachable from any page was removed.')


if __name__ == '__main__':
    main()
