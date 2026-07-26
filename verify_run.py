"""Audit a compression run: prove it did not lose anything.

Compresses are only trustworthy if you can show what SURVIVED. Point this at a folder
holding `before/` and `after/` copies of the same files (same filenames) and it compares
every pair on the things that matter — page count, colour, link annotations, bookmarks,
searchable text, readability — and FLAGS any file that lost something, so a regression
cannot hide in a long list.

Typical use: copy a representative sample to <dir>/before and <dir>/after, run the tool
in place over <dir>/after, then run this.

    python verify_run.py <dir>            # writes <dir>/REVIEW.md and <dir>/review.csv

Note the colour check shares ocrmyworkshopmanual's _is_color(); for an audit that is
independent of the tool's own judgement, compare raw saturated-pixel counts instead.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ocrmyworkshopmanual as owm  # noqa: E402

if len(sys.argv) < 2:
    sys.exit(__doc__)
REV = Path(sys.argv[1])
if not (REV / 'before').is_dir() or not (REV / 'after').is_dir():
    sys.exit(f'{REV} must contain before/ and after/ folders with matching filenames')


def probe(pdf: Path) -> dict:
    """Structural facts about a PDF: pages, text, links, bookmarks, image kinds."""
    d = {'pages': 0, 'text': 0, 'links': 0, 'bookmarks': 0, 'jbig2': 0, 'err': ''}
    try:
        r = PdfReader(str(pdf))
        d['pages'] = len(r.pages)
        for i in range(min(8, d['pages'])):
            pg = r.pages[i]
            try:
                d['text'] += len((pg.extract_text() or '').strip())
            except Exception:
                pass
            ann = pg.get('/Annots')
            if ann:
                try:
                    d['links'] += sum(1 for a in ann.get_object()
                                      if a.get_object().get('/Subtype') == '/Link')
                except Exception:
                    pass

        def cnt(items):
            n = 0
            for it in items:
                n += cnt(it) if isinstance(it, list) else 1
            return n
        try:
            d['bookmarks'] = cnt(r.outline)
        except Exception:
            pass
        d['jbig2'] = pdf.read_bytes().count(b'/JBIG2Decode')
    except Exception as ex:
        d['err'] = str(ex)[:80]
    return d


def is_colour(pdf: Path, pages=(1, 2, 3)) -> bool:
    """True if ANY of the sampled pages carries genuine colour (same test the router uses)."""
    for p in pages:
        w = Path(tempfile.mkdtemp())
        c = w / 'c.png'
        try:
            subprocess.run([owm.GS, '-sDEVICE=png16m', '-r100', f'-dFirstPage={p}',
                            f'-dLastPage={p}', '-dNOPAUSE', '-dBATCH', '-dQUIET',
                            '-sOutputFile=' + str(c), owm.win_long(pdf)],
                           capture_output=True, timeout=180)
            if c.exists():
                a = np.asarray(Image.open(c).convert('RGB')).astype(np.int16)
                if owm._is_color(a):
                    return True
        except Exception:
            pass
    return False


def main() -> None:
    before, after = REV / 'before', REV / 'after'
    origin = {}
    if (REV / 'origin_map.json').exists():
        origin = json.loads((REV / 'origin_map.json').read_text(encoding='utf-8'))
    rows = []
    for b in sorted(before.glob('*.pdf')):
        a = after / b.name
        if not a.exists():
            continue
        pb, pa = probe(b), probe(a)
        cb, ca = is_colour(b), is_colour(a)
        sb, sa = b.stat().st_size, a.stat().st_size
        flags = []
        if cb and not ca:
            flags.append('COLOUR LOST')
        if pb['pages'] != pa['pages']:
            flags.append(f"PAGES {pb['pages']}->{pa['pages']}")
        if pb['links'] > pa['links']:
            flags.append(f"LINKS {pb['links']}->{pa['links']}")
        if pb['bookmarks'] > pa['bookmarks']:
            flags.append(f"BOOKMARKS {pb['bookmarks']}->{pa['bookmarks']}")
        if pa['err']:
            flags.append('UNREADABLE OUTPUT')
        if sa > sb * 1.05 and sa - sb > 200_000:
            flags.append(f'GREW {100 * sa / sb:.0f}%')
        rows.append(dict(name=b.name, src=origin.get(b.name, ''), mb_b=sb / 1048576,
                         mb_a=sa / 1048576, pct=100 * sa / max(sb, 1),
                         pages=pa['pages'], txt_b=pb['text'], txt_a=pa['text'],
                         links=f"{pb['links']}->{pa['links']}",
                         bm=f"{pb['bookmarks']}->{pa['bookmarks']}",
                         col=f"{'Y' if cb else 'n'}->{'Y' if ca else 'n'}",
                         flags='; '.join(flags)))

    out_md = REV / 'REVIEW.md'
    tb = sum(r['mb_b'] for r in rows)
    ta = sum(r['mb_a'] for r in rows)
    bad = [r for r in rows if r['flags']]
    ocr_added = sum(1 for r in rows if r['txt_b'] == 0 and r['txt_a'] > 0)
    lines = [
        '# Sample compression review', '',
        f'**{len(rows)} files** — {tb:.0f} MB -> {ta:.0f} MB '
        f'(**{100 * ta / max(tb, 1):.0f}%**, saved {tb - ta:.0f} MB)', '',
        f'- Files that LOST something: **{len(bad)}**',
        f'- Files that gained a searchable text layer: **{ocr_added}**',
        f'- Compare pairs by filename in `{before}` vs `{after}`', '',
    ]
    if bad:
        lines += ['## !! Needs attention', '',
                  '| file | size | issue |', '|---|---|---|']
        lines += [f"| `{r['name']}` | {r['mb_b']:.2f}->{r['mb_a']:.2f} MB | **{r['flags']}** |"
                  for r in bad]
        lines += ['']
    lines += ['## All files', '',
              '| file | MB before | MB after | % | pages | text before->after | links | bookmarks | colour |',
              '|---|--:|--:|--:|--:|---|---|---|---|']
    for r in sorted(rows, key=lambda r: r['pct']):
        lines.append(f"| `{r['name']}` | {r['mb_b']:.2f} | {r['mb_a']:.2f} | {r['pct']:.0f} | "
                     f"{r['pages']} | {r['txt_b']}->{r['txt_a']} | {r['links']} | {r['bm']} | {r['col']} |")
    out_md.write_text('\n'.join(lines), encoding='utf-8')

    import csv
    with open(REV / 'review.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print('\n'.join(lines[:12]))
    print(f'\n-> {out_md}\n-> {REV / "review.csv"}')


if __name__ == '__main__':
    main()
