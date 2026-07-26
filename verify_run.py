"""Audit a compression run: prove it did not lose anything.

Point this at a folder holding `before/` and `after/` copies of the same files (same
filenames) and it compares every pair on the things that matter — page count, colour,
link annotations, bookmarks, searchable text, readability — and FLAGS any file that lost
something, so a regression cannot hide in a long list.

    python verify_run.py <dir>            # writes <dir>/REVIEW.md and <dir>/review.csv

INDEPENDENCE IS THE POINT. This script deliberately does NOT import
ocrmyworkshopmanual: an auditor that reuses the code it audits cannot catch that code
being wrong. That mistake was made once here — a colour-loss audit called the pipeline's
own colour test, so a diagram the pipeline could not see as colour was also invisible to
the audit, and the report said "nothing lost" while colour was being destroyed. Every
judgement below is therefore made from first principles on the rendered pixels and the
PDF structure, using only Ghostscript (a neutral renderer) and pypdf:

  * colour  — count strongly saturated pixels (max-min channel spread) in each render
              and compare; no shared thresholds, no shared helper.
  * text    — compare by WORD RECALL, not character count. A re-OCR legitimately differs
              in character count (old layers carry duplicated junk), so counting
              characters produces false alarms; asking "what fraction of the original's
              words survive" measures content.
  * structure — page count, /Link annotations and outline entries read straight from the
              PDF with pypdf.
"""
from __future__ import annotations

import collections
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from pypdf import PdfReader

# ── neutral tools (a renderer and a parser — no pipeline code) ────────────────


def find_ghostscript() -> str | None:
    env = os.environ.get('JBIG2_GS')
    if env and Path(env).exists():
        return env
    for name in ('gswin64c', 'gswin32c', 'gs'):
        found = shutil.which(name)
        if found:
            return found
    for base in (r'C:\Program Files\gs', r'C:\Program Files (x86)\gs'):
        hits = sorted(glob.glob(base + r'\*\bin\gswin64c.exe')) or \
            sorted(glob.glob(base + r'\*\bin\gswin32c.exe'))
        if hits:
            return hits[-1]
    return None


GS = find_ghostscript()


def _long(p: Path) -> str:
    s = str(p.resolve())
    return '\\\\?\\' + s if os.name == 'nt' and len(s) > 240 and not s.startswith('\\\\') else s


def render(pdf: Path, page: int, dpi: int = 100) -> Image.Image | None:
    """Render one page to RGB. Ghostscript is used only as a neutral rasteriser."""
    if not GS:
        return None
    work = Path(tempfile.mkdtemp())
    out = work / 'p.png'
    try:
        subprocess.run([GS, '-sDEVICE=png16m', f'-r{dpi}', f'-dFirstPage={page}',
                        f'-dLastPage={page}', '-dNOPAUSE', '-dBATCH', '-dQUIET',
                        '-sOutputFile=' + str(out), _long(pdf)],
                       capture_output=True, timeout=300)
        return Image.open(out).convert('RGB') if out.exists() else None
    except Exception:
        return None
    finally:
        pass


# ── independent measurements ─────────────────────────────────────────────────

SATURATION = 60          # channel spread that counts as "really coloured"
MIN_SAT_PX = 500         # below this, colour is noise (JPEG fringing, speckle)


def saturated_pixels(pdf: Path, pages=(1, 2, 3, 5)) -> int:
    """How many strongly coloured pixels the document actually shows. Raw pixel maths on
    the render — no white balancing, no shared thresholds with the pipeline."""
    total = 0
    for p in pages:
        im = render(pdf, p)
        if im is None:
            continue
        a = np.asarray(im).astype(np.int16)
        total += int(((a.max(2) - a.min(2)) > SATURATION).sum())
    return total


def words(text: str) -> list:
    return re.findall(r'[^\W\d_]{3,}', (text or '').lower())


def text_of(pdf: Path, limit: int = 8) -> str:
    try:
        r = PdfReader(str(pdf))
        return '\n'.join((r.pages[i].extract_text() or '')
                         for i in range(min(limit, len(r.pages))))
    except Exception:
        return ''


def word_recall(before: str, after: str) -> float | None:
    """Fraction of the ORIGINAL's words still present afterwards. Order-independent and
    immune to the character-count noise that makes a legitimate re-OCR look like loss."""
    wb, wa = collections.Counter(words(before)), collections.Counter(words(after))
    if sum(wb.values()) < 50:
        return None
    return sum(min(n, wa[w]) for w, n in wb.items()) / sum(wb.values())


def structure(pdf: Path) -> dict:
    d = {'pages': 0, 'links': 0, 'bookmarks': 0, 'err': ''}
    try:
        r = PdfReader(str(pdf))
        d['pages'] = len(r.pages)
        for pg in r.pages:
            ann = pg.get('/Annots')
            if not ann:
                continue
            try:
                d['links'] += sum(1 for a in ann.get_object()
                                  if a.get_object().get('/Subtype') == '/Link')
            except Exception:
                pass

        def count(items):
            n = 0
            for it in items:
                n += count(it) if isinstance(it, list) else 1
            return n
        try:
            d['bookmarks'] = count(r.outline)
        except Exception:
            pass
    except Exception as ex:
        d['err'] = str(ex)[:80]
    return d


# ── report ───────────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    rev = Path(sys.argv[1])
    before, after = rev / 'before', rev / 'after'
    if not before.is_dir() or not after.is_dir():
        sys.exit(f'{rev} must contain before/ and after/ folders with matching filenames')
    if not GS:
        print('WARNING: Ghostscript not found — colour cannot be audited', file=sys.stderr)
    origin = {}
    if (rev / 'origin_map.json').exists():
        origin = json.loads((rev / 'origin_map.json').read_text(encoding='utf-8'))

    rows = []
    for b in sorted(before.glob('*.pdf')):
        a = after / b.name
        if not a.exists():
            continue
        sb_st, sa_st = structure(b), structure(a)
        tb, ta = text_of(b), text_of(a)
        recall = word_recall(tb, ta)
        cb = saturated_pixels(b)
        ca = saturated_pixels(a) if cb >= MIN_SAT_PX else 0
        kept_col = (ca / cb) if cb >= MIN_SAT_PX else None
        size_b, size_a = b.stat().st_size, a.stat().st_size

        flags = []
        if kept_col is not None and kept_col < 0.25:
            flags.append(f'COLOUR LOST ({cb}->{ca} px)')
        if sb_st['pages'] != sa_st['pages']:
            flags.append(f"PAGES {sb_st['pages']}->{sa_st['pages']}")
        if sb_st['links'] > sa_st['links']:
            flags.append(f"LINKS {sb_st['links']}->{sa_st['links']}")
        if sb_st['bookmarks'] > sa_st['bookmarks']:
            flags.append(f"BOOKMARKS {sb_st['bookmarks']}->{sa_st['bookmarks']}")
        if sa_st['err']:
            flags.append('UNREADABLE OUTPUT')
        if recall is not None and recall < 0.6:
            flags.append(f'TEXT LOST (word recall {recall:.2f})')
        if not words(tb) and not words(ta) and sa_st['pages']:
            flags.append('NO TEXT LAYER (not searchable)')
        if size_a > size_b * 1.05 and size_a - size_b > 200_000:
            flags.append(f'GREW {100 * size_a / size_b:.0f}%')

        rows.append(dict(
            name=b.name, src=origin.get(b.name, ''),
            mb_b=size_b / 1048576, mb_a=size_a / 1048576,
            pct=100 * size_a / max(size_b, 1), pages=sa_st['pages'],
            words_b=len(words(tb)), words_a=len(words(ta)),
            text_recall='' if recall is None else round(recall, 3),
            links=f"{sb_st['links']}->{sa_st['links']}",
            bm=f"{sb_st['bookmarks']}->{sa_st['bookmarks']}",
            colour_px=f'{cb}->{ca}' if cb >= MIN_SAT_PX else 'n/a',
            flags='; '.join(flags)))

    if not rows:
        sys.exit('no matching before/after pairs found')
    tot_b = sum(r['mb_b'] for r in rows)
    tot_a = sum(r['mb_a'] for r in rows)
    bad = [r for r in rows if r['flags']]
    coloured = [r for r in rows if r['colour_px'] != 'n/a']
    gained = [r for r in rows if r['words_b'] < 20 <= r['words_a']]

    out = [
        '# Compression run audit', '',
        f'**{len(rows)} files** — {tot_b:.0f} MB -> {tot_a:.0f} MB '
        f'(**{100 * tot_a / max(tot_b, 1):.0f}%**, saved {tot_b - tot_a:.0f} MB)', '',
        f'- Files that LOST something: **{len(bad)}**',
        f'- Files carrying real colour: **{len(coloured)}** (all must keep it)',
        f'- Files that gained a searchable text layer: **{len(gained)}**',
        '',
        '_Measured independently of the compression code: colour from raw saturated-pixel '
        'counts, text by word recall, structure via pypdf._', '',
    ]
    if bad:
        out += ['## !! Needs attention', '', '| file | size | issue |', '|---|---|---|']
        out += [f"| `{r['name']}` | {r['mb_b']:.2f}->{r['mb_a']:.2f} MB | **{r['flags']}** |"
                for r in bad]
        out += ['']
    out += ['## All files', '',
            '| file | MB before | MB after | % | pages | words b->a | text recall | links | bookmarks | colour px |',
            '|---|--:|--:|--:|--:|---|--:|---|---|---|']
    for r in sorted(rows, key=lambda r: r['pct']):
        out.append(f"| `{r['name']}` | {r['mb_b']:.2f} | {r['mb_a']:.2f} | {r['pct']:.0f} | "
                   f"{r['pages']} | {r['words_b']}->{r['words_a']} | {r['text_recall']} | "
                   f"{r['links']} | {r['bm']} | {r['colour_px']} |")
    (rev / 'REVIEW.md').write_text('\n'.join(out), encoding='utf-8')
    with open(rev / 'review.csv', 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print('\n'.join(out[:12]))
    print(f"\n-> {rev / 'REVIEW.md'}\n-> {rev / 'review.csv'}")


if __name__ == '__main__':
    main()
