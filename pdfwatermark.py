#!/usr/bin/env python3
"""
pdfwatermark.py

Find and remove a re-distributor's stamp from a manual: text that looks like somebody
else's mark, written on a line of its own, repeating character for character page after
page.

    python pdfwatermark.py <file-or-dir>                  # report only
    python pdfwatermark.py <file-or-dir> --apply          # rewrite in place
    python pdfwatermark.py <file> --pages 1-8 --dest DIR  # a few pages, to a copy

WHAT IT LOOKS FOR, in three parts, each of which earned its place by being measured:

  1. A **marker** — text that is *link-like* (`example.org`, `www.x.net`, `https://…`) or
     is a converter's *licence nag* (`trial version`, `to remove this mark`). This is the
     one that says SOMEBODY ELSE wrote it. Nothing about a specific site is hard-coded;
     the domain is whatever was found, and the nag phrases are about software licensing
     only, never a manual's own words.
  2. It **repeats** — the same operator draws CHARACTER-FOR-CHARACTER the same text on
     consecutive pages. A manual cites a real URL in its body once; it does not cite it
     identically on page after page from one operator.
  3. It is **on a line of its own** — everything else on its baseline comes to almost no
     text. See `_uncrowded`.

Dropping any one of them was measured to cause damage. Repetition alone (no marker) takes
section titles, running footers, `HINT:` labels, wiring-diagram terminal names and at least
one repair instruction out of manuals that carry no stamp at all. The marker alone (no
line test) deletes `: www.motul.fr` from a Motul datasheet's own address block.

Once a stamp is confirmed, the REST OF ITS BLOCK goes with it: lines that repeat the same
way immediately beside it, even with no marker of their own, because a stamping tool writes
a block and half a stamp removed is still a stamped file. See `_companions`.

WHERE THE STAMP SITS IS NOT A TEST, and used to be — "a margin band, the bottom 12% of
the page". Measured over 3,007 archive files, that condition prevented zero false
positives: detection without it agreed on 3,006, and the one disagreement was a real
stamp the band had been hiding. It did not protect the Motul datasheet either — that URL
is in the bottom margin, so the band flagged it too. See `detect`. Position is now
measured by the audit, from rendered pixels, where it cannot be got wrong.

WHERE THE STAMP SITS IS NOT A TEST, and used to be — "a margin band, the bottom 12% of
the page". Measured over 3,007 archive files, that condition prevented zero false
positives: detection without it agreed on 3,006, and the one disagreement was a real
stamp the band had been hiding. See `detect`. Position is now measured by the audit,
from rendered pixels, where it cannot be got wrong.

WHY IT IS NOT A PIXEL JOB. These stamps are drawn, not painted in: a show-text
operator, usually inside a Form XObject invoked by one `Do`. That means removal can be
exact — delete the operator, keep every byte of the page's real content, no
re-rendering, no re-compression, no loss on the scan underneath. A raster inpainting
pass would rewrite the whole page image to erase 16pt of text.

WHAT IS REMOVED IS ONLY EVER A SHOW-TEXT OR `Do` OPERATOR. Graphics state, the text
matrix and the marked-content nesting are all left alone. This matters: `Tj` and `TJ`
do not move the text matrix, so dropping them cannot shift the text that follows.
`'` and `"` *do* advance a line, so they are replaced by `T*` rather than deleted, which
advances identically and shows nothing. A `BDC`/`EMC` pair left empty is dropped only
after its contents are gone, and `q`/`Q` are never touched, so the stack stays balanced.

A FORM XOBJECT IS REMOVED AT ITS CALL SITE, not by editing the form, when everything the
form paints is watermark — one `Do` goes and the form becomes unreferenced. A form that
paints real content as well is rewritten instead, and only when every page that invokes
it was flagged; a form shared with an unflagged page is left alone and counted, because
editing it would alter a page this run never examined.

THE AUDIT DECIDES, NOT THE DIFF. Following the rule this repo is built on, a rewrite is
compared against the source before anything is kept, and the original stays if a check
is fatal:

  * page count, annotation count and bookmark count identical
  * extracted text: every token that disappeared must be a token of the watermark, and
    nothing may appear that was not there before
  * rendered pixels (--verify-render): the region where ink changed must contain the
    operators that were deleted, and must be the SAME region on every page checked — one
    stamp cannot leave two different holes. A page whose ink changed anywhere else is
    damage, whatever the file size says. See `_render_audit`.

Text extraction is not enough on its own — a stamp in a font with no /ToUnicode extracts
as nothing, so the text check would pass a page that still shows the stamp *and* a page
that lost a figure. The render check is what proves the visible result, and is the one
to run when a file is unfamiliar.
"""
import argparse
import collections
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import pikepdf
except ImportError:                                     # pragma: no cover
    pikepdf = None

# A run of text with where it landed on the page, and where its operator lives so the
# operator can be deleted later. `where` is ('page', n) or ('form', objgen) — the
# container whose operator list holds index `op`.
# `size` is the text's height on the page in points (font size through the text matrix and
# the CTM), and `ex, ey` where the text position stands after the run -- its end, from the
# font's widths. Together they are how much paper a run covers, which `strip_lines` needs.
Run = collections.namedtuple('Run', 'text x y page where op size ex ey',
                             defaults=(0.0, None, None))

# converted : pages a watermark operator was removed from
# forms     : form XObjects dropped at the call site
# rewritten : form XObjects whose own stream had to be edited
# shared    : forms left alone because an unflagged page also invokes them
Removal = collections.namedtuple('Removal', 'pages ops forms rewritten shared merged')

# Link-like: an explicit URL, or a bare host under a TLD people actually stamp with.
# A closed TLD list and not `\.[a-z]{2,}` — the loose form matches "Fig.4a", "No.1b",
# and every sentence-ending abbreviation in a service manual.
_TLDS = ('com|org|net|info|io|co|ru|de|uk|biz|us|eu|pl|it|es|fr|nl|se|cz|jp|cn|in|au'
         '|ca|shop|store|online|site|club|xyz|me|tv|cc|pro|top|download|pdf')
_URLISH = re.compile(
    r'(?:https?://|www\.)\S+'
    r'|\b[A-Za-z0-9][A-Za-z0-9._-]{1,60}\.(?:' + _TLDS + r')\b', re.I)

# A trial converter's nag. Deliberately about SOFTWARE LICENSING and nothing else: these
# are phrases a workshop manual has no reason to print, whereas a looser 'trial' or
# 'register' would match "road trial" and "register your vehicle". Measured need: the
# 1997 Mazda 626 manual carries `Created by TIFF To PDF trial version, to remove this
# mark, please register this software.` on all 662 pages, one line above the URL stamp --
# pure prose, so the link test never saw it and 662 pages kept it.
_NAGS = re.compile(
    r'trial version'
    r'|evaluation (?:version|copy)'
    r'|unregistered version'
    r'|to remove (?:this|the) (?:mark|watermark|message|notice|banner)'
    r'|register this software'
    r'|purchase the full version', re.I)

_SHOW = ('Tj', 'TJ', "'", '"')


def looks_like_link(text):
    m = _URLISH.search(text or '')
    return m.group(0).lower() if m else None


def looks_like_nag(text):
    m = _NAGS.search(text or '')
    return m.group(0).lower() if m else None


def stamp_marker(text):
    """What makes a run recognisable as somebody else's mark rather than the manual's own
    words: a link, or a converter's licence nag. Either is enough to open a candidate; the
    repeat test is what confirms it.
    """
    return looks_like_link(text) or looks_like_nag(text)


# ---------------------------------------------------------------- text decoding

def _tounicode(font):
    """{code: str} from a font's /ToUnicode CMap, plus the code width in bytes.

    Only bfchar and bfrange are read. Without this a stamp in a subset font decodes to
    mojibake and never matches the link test, which is how a CID-font watermark hides.
    """
    table, width = {}, 1
    try:
        cmap = bytes(font['/ToUnicode'].read_bytes())
    except Exception:
        return table, width
    csr = re.search(rb'begincodespacerange\s*<([0-9A-Fa-f]+)>', cmap)
    if csr:
        width = max(1, len(csr.group(1)) // 2)

    def _txt(h):
        raw = bytes.fromhex(h.decode('ascii'))
        try:
            return raw.decode('utf-16-be', 'ignore')
        except Exception:
            return ''

    for block in re.findall(rb'beginbfchar(.*?)endbfchar', cmap, re.S):
        for src, dst in re.findall(rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', block):
            table[int(src, 16)] = _txt(dst)
    for block in re.findall(rb'beginbfrange(.*?)endbfrange', cmap, re.S):
        for a, b, dst in re.findall(
                rb'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', block):
            start, end, base = int(a, 16), int(b, 16), int(dst, 16)
            for i in range(start, min(end, start + 65535) + 1):
                try:
                    table[i] = chr(base + (i - start))
                except ValueError:
                    pass
    return table, width


def _decoder(font):
    """A callable turning a PDF string's bytes into text for one font."""
    if font is None:
        return lambda raw: raw.decode('latin-1', 'replace')
    table, width = _tounicode(font)
    two = width == 2 or str(font.get('/Subtype', '')) == '/Type0'
    if not table:
        if two:
            return lambda raw: ''
        return lambda raw: raw.decode('latin-1', 'replace')

    def decode(raw):
        out = []
        step = 2 if two else 1
        for i in range(0, len(raw) - step + 1, step):
            code = int.from_bytes(raw[i:i + step], 'big')
            out.append(table.get(code, ''))
        return ''.join(out)
    return decode


def _advancer(font):
    """A callable giving a PDF string's advance for one font: (glyph widths summed in
    thousandths of text space, codes shown, single-byte spaces shown).

    This is how far a viewer moves the text position after showing the string, and so where
    the NEXT piece of a line lands. Without it every piece after the first in one BT block
    was placed at the block's start: measured, a running header's last piece ended 1.5 pt
    right of where its line was thought to end, and an audit of what changed on the page
    failed a correct rewrite over that sliver. Widths come from the font's /Widths (simple
    fonts) or its descendant's /W and /DW (Type0). A font with neither -- the standard 14
    may omit /Widths -- is taken at a full em a code: the widest a glyph's advance runs, so
    a box built from it covers the ink ("M" is 833 in Helvetica, and 500 under-covered it).
    """
    if font is None:
        return lambda raw: (1000 * len(raw), len(raw), raw.count(b' '))
    try:
        if str(font.get('/Subtype', '')) == '/Type0':
            desc = font['/DescendantFonts'][0]
            dw = float(desc.get('/DW', 1000))
            widths = {}
            w = list(desc.get('/W') or [])
            i = 0
            while i + 1 < len(w):
                first = int(w[i])
                if isinstance(w[i + 1], pikepdf.Array):
                    for k, v in enumerate(w[i + 1]):
                        widths[first + k] = float(v)
                    i += 2
                elif i + 2 < len(w):
                    for c in range(first, int(w[i + 1]) + 1):
                        widths[c] = float(w[i + 2])
                    i += 3
                else:
                    break

            def adv2(raw):
                codes = [int.from_bytes(raw[k:k + 2], 'big') for k in range(0, len(raw) - 1, 2)]
                return sum(widths.get(c, dw) for c in codes), len(codes), 0
            return adv2
        fc = int(font.get('/FirstChar', 0))
        ws = [float(v) for v in (font.get('/Widths') or [])]
        fd = font.get('/FontDescriptor')
        missing = float(fd.get('/MissingWidth', 0)) if fd is not None else 0.0
        if not ws:
            missing = missing or 1000.0
    except Exception:
        return lambda raw: (1000 * len(raw), len(raw), raw.count(b' '))

    def adv1(raw):
        total = 0.0
        for b in raw:
            k = b - fc
            total += ws[k] if 0 <= k < len(ws) else missing
        return total, len(raw), raw.count(b' ')
    return adv1


# ---------------------------------------------------------------- interpretation

def _mul(m, n):
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D,
            c * A + d * C, c * B + d * D,
            e * A + f * C + E, e * B + f * D + F)


def _num(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def _instructions(obj):
    try:
        return list(pikepdf.parse_content_stream(obj))
    except Exception:
        return []


def _walk(instructions, resources, ctm, where, page_no, runs, painters, depth=0):
    """Collect text runs in device space, and count painting ops per container.

    `painters` collects, per container, the INDEX of every operator that puts marks on
    the page, so a form can later be judged watermark-only: a form whose painting
    operators are all flagged can go at its call site.

    Indices and not a count. A count was the first version and it was wrong: a form used
    on 60 sampled pages is walked 60 times, so its painting total reached 60 while the
    flagged set stayed at the one operator index inside it. Every watermark form then
    looked "mixed" and was edited in place rather than unhooked at the call site --
    which still produced a correct file here, but by the riskier of the two paths.
    """
    stack = []
    tm = tlm = (1, 0, 0, 1, 0, 0)
    leading = 0.0
    font = None
    font_size = 0.0
    char_sp = word_sp = 0.0
    h_scale = 1.0
    decode = _decoder(None)
    advance = _advancer(None)
    fonts = (resources or {}).get('/Font') or {}
    xobjects = (resources or {}).get('/XObject') or {}

    for i, ins in enumerate(instructions):
        op = str(ins.operator)
        args = ins.operands
        if op == 'Tc' and args:
            char_sp = _num(args[0])
        elif op == 'Tw' and args:
            word_sp = _num(args[0])
        elif op == 'Tz' and args:
            h_scale = _num(args[0]) / 100.0
        if op == 'q':
            stack.append(ctm)
        elif op == 'Q':
            ctm = stack.pop() if stack else ctm
        elif op == 'cm' and len(args) == 6:
            ctm = _mul(tuple(_num(a) for a in args), ctm)
        elif op == 'BT':
            tm = tlm = (1, 0, 0, 1, 0, 0)
        elif op == 'Tf' and len(args) == 2:
            try:
                font = fonts[str(args[0])]
            except Exception:
                font = None
            font_size = _num(args[1])
            decode = _decoder(font)
            advance = _advancer(font)
        elif op == 'TL' and args:
            leading = _num(args[0])
        elif op in ('Td', 'TD') and len(args) == 2:
            if op == 'TD':
                leading = -_num(args[1])
            tlm = _mul((1, 0, 0, 1, _num(args[0]), _num(args[1])), tlm)
            tm = tlm
        elif op == 'Tm' and len(args) == 6:
            tm = tlm = tuple(_num(a) for a in args)
        elif op == 'T*':
            tlm = _mul((1, 0, 0, 1, 0, -leading), tlm)
            tm = tlm
        elif op in _SHOW:
            if op == '"' and len(args) == 3:
                word_sp, char_sp = _num(args[0]), _num(args[1])
            if op in ("'", '"'):
                tlm = _mul((1, 0, 0, 1, 0, -leading), tlm)
                tm = tlm
            parts = []
            tx = 0.0                                   # advance, in unscaled text space
            for a in args:
                if isinstance(a, pikepdf.String):
                    raw = bytes(a)
                    parts.append(decode(raw))
                    w, n, sp = advance(raw)
                    tx += w / 1000.0 * font_size + n * char_sp + sp * word_sp
                elif isinstance(a, pikepdf.Array):
                    for el in a:
                        if isinstance(el, pikepdf.String):
                            raw = bytes(el)
                            parts.append(decode(raw))
                            w, n, sp = advance(raw)
                            tx += w / 1000.0 * font_size + n * char_sp + sp * word_sp
                        else:
                            tx -= _num(el) / 1000.0 * font_size
            text = ''.join(parts).strip()
            dev = _mul(tm, ctm)
            end = _mul(_mul((1, 0, 0, 1, tx * h_scale, 0), tm), ctm)
            painters[where].add(i)
            if text:
                runs.append(Run(text, dev[4], dev[5], page_no, where, i,
                                abs(font_size) * (dev[2] ** 2 + dev[3] ** 2) ** 0.5,
                                end[4], end[5]))
            tm = _mul((1, 0, 0, 1, tx * h_scale, 0), tm)
        elif op in ('Do', 'sh', 'EI'):
            painters[where].add(i)
            if op == 'Do' and args and depth < 6:
                try:
                    xo = xobjects[str(args[0])]
                except Exception:
                    continue
                if str(xo.get('/Subtype', '')) != '/Form':
                    continue
                sub = (1, 0, 0, 1, 0, 0)
                if '/Matrix' in xo:
                    sub = tuple(_num(v) for v in xo['/Matrix'])
                _walk(_instructions(xo), xo.get('/Resources'), _mul(sub, ctm),
                      ('form', xo.objgen), page_no, runs, painters, depth + 1)
        elif op in ('S', 's', 'f', 'F', 'f*', 'B', 'B*', 'b', 'b*'):
            painters[where].add(i)
    return ctm


def page_runs(page, page_no, painters):
    runs = []
    _walk(_instructions(page), page.get('/Resources'), (1, 0, 0, 1, 0, 0),
          ('page', page_no), page_no, runs, painters)
    return runs


# ---------------------------------------------------------------- detection

def _box(page):
    try:
        b = [float(v) for v in (page.get('/MediaBox') or [0, 0, 612, 792])]
    except Exception:
        b = [0, 0, 612, 792]
    return b[0], b[1], b[2] - b[0], b[3] - b[1]


def _rotation(page):
    try:
        r = int(page.get('/Rotate') or 0) % 360
    except Exception:
        return 0
    return r // 90 * 90                            # /Rotate -90 is 270; snap junk down


def _display(x, y, box, rot):
    """A content-space point, and the page's size, AS THE READER SEES THEM.

    /Rotate is applied by the viewer, so content coordinates are not what a reader sees.
    Measured on a 346-page JK body-repair manual whose every page carries /Rotate 270
    (some spelled -90): the stamp `GETtheMANUALS.org` sits at content x=13.3, y=473.8 of
    a 612x792 box. Displayed, the same point is 13.3 up a 612-high page.

    The render audit compares against Ghostscript output, which honours /Rotate, so the
    positions it is given have to be in this frame or nothing lines up.

    Returns (x, y, width, height) with the origin at the displayed bottom-left.
    """
    x0, y0, w, h = box
    px, py = x - x0, y - y0
    if rot == 90:
        return py, w - px, h, w
    if rot == 180:
        return w - px, h - py, w, h
    if rot == 270:
        return h - py, px, h, w
    return px, py, w, h


class Candidate:
    """One repeating link-like stamp, and every operator that draws it."""

    def __init__(self, text, companion=False):
        self.text = text                               # the marker, which is the identity
        self.companion = companion                     # a line that only repeats BESIDE one
        self.pages = set()
        self.seen = []                                 # (x, y) fractions, for the report
        self.texts = collections.Counter()             # the WHOLE run text, as removed

    def add(self, run, rx, ry):
        self.pages.add(run.page)
        self.seen.append((rx, ry))
        self.texts[run.text] += 1

    def confirm(self, repeats=2):
        """Freeze the run texts that repeat, and say whether any did.

        This is the tool's real safety property, and it is cheap. A page where the stamp
        shares a show-text operator with body text produces a run text that appears once;
        the stamp's own text appears on page after page, character for character. Removing
        only operators whose whole text is in this set means a merged run is never
        deleted -- so nothing but the stamp can be lost, by construction, rather than by
        an after-the-fact check on every page.

        Measured on a 9,192-page manual: extracting text from both files to prove the same
        thing costs 15 minutes a file at 101 ms a page, which is not a default a bulk
        archive pass can carry.

        A text that appears on one page only is not a stamp and is never removed -- that
        is the whole test, and it is why a stamp whose text changes from page to page (one
        that numbers its pages, say) is reported and left alone rather than guessed at.
        """
        self.exact = {t for t, n in self.texts.items()
                      if n >= repeats and len(self.pages) >= repeats}
        return bool(self.exact)

    def label(self):
        """What a reader should be told was removed: the whole stamp, not the domain.

        The link is only how the stamp is recognised. Measured on a 9,192-page manual
        stamped with a personal address of the form `<name>@<host>.com`: reporting the
        regex match called it a `<host>.com` watermark, and -- worse -- the audit built
        its allowed-token set from that same string, so the `<name>` part counted as text
        the rewrite had LOST and the audit rejected its own correct output on every page.
        """
        return self.texts.most_common(1)[0][0] if self.texts else self.text

    def matches_text(self, text):
        """A companion line is matched on its exact text alone.

        It has no marker of its own -- that is exactly why it was missed -- so there is
        nothing else to match on. Its licence to be removed came from repeating,
        character for character, right beside a confirmed stamp; see `_companions`.
        """
        return bool(self.companion and text in (getattr(self, 'exact', None) or ()))

    def matches(self, link, text=None):
        if self.companion or link != self.text:
            return False
        if text is None or not getattr(self, 'exact', None):
            return True                            # no repeating text: match on the link
        return text in self.exact

    def where(self):
        xs = sorted(x for x, _ in self.seen)
        ys = sorted(y for _, y in self.seen)
        return f'x {xs[0]:.2f}-{xs[-1]:.2f}, y {ys[0]:.2f}-{ys[-1]:.2f}'


def detect(pdf, sample=2, give_up=8):
    """Candidates that are link-like and drawn by the SAME operator, with the same text,
    on consecutive pages. Reads from the front and stops as soon as one repeats.

    This kind of stamp is on every page or on none: it is added in one pass by whoever
    passed the file on, to sign it, and the pass writes the same operator into every page.
    So two pages settle it. Detection reads page 1 and page 2, and a run that is link-like
    with CHARACTER-FOR-CHARACTER the same text on both is the stamp; anything else is not.

    WHERE IT SITS IS NOT A CONDITION, and used to be. The rule was "a margin band, the
    bottom 12% of the page", and it was measured to buy nothing: over 3,007 archive files,
    detection with the band condition removed gave an identical answer on 3,006 of them,
    and the one disagreement was another real stamp the band had been hiding -- a
    converter's `http://www.adultpdf.com` nag sitting in the TOP band of a 662-page Mazda
    626 manual, which the `bottom` default rejected. Zero false positives were prevented
    by it in that sample. The repeat test is what does the discriminating: a manual cites
    a URL in its body once, not character-for-character on page after page from the same
    operator.

    Dropping it also removed a whole class of geometry bug. The band had to be computed in
    DISPLAY space, and doing it in content space silently missed every rotated file -- 346
    pages of a JK body-repair manual stamped `GETtheMANUALS.org`, reported clean, because
    /Rotate 270 put the stamp's content y at 0.60 of the box. Position now only has to be
    right for the audit, which measures it from rendered pixels and cannot get it wrong.

    Reading the front rather than a spread across the book is the cheap shape and it
    matches what is being looked for. An earlier version sampled 60 pages spread through
    the document, which on a 9,192-page manual is 60 content-stream walks to learn what
    page 2 already said.

    `give_up` is the tolerance for a front matter page that carries no stamp -- a cover, a
    blank, a scanned title page with no text operators at all. Pages are read one at a
    time and the walk stops the moment a text repeats, so the normal file costs exactly
    two pages and only an unusual one pays for more.
    """
    n = len(pdf.pages)
    if n == 0:
        return [], collections.defaultdict(set)

    groups, painters = {}, collections.defaultdict(set)
    hits, per_page = [], {}
    for i in range(min(n, max(sample, give_up))):
        page = pdf.pages[i]
        box, rot = _box(page), _rotation(page)
        kept = []
        per_page[i] = kept
        for run in page_runs(page, i, painters):
            dx, dy, w, h = _display(run.x, run.y, box, rot)
            kept.append((run, dx / w if w else 0, dy / h if h else 0))
        alone = _uncrowded(kept)
        for idx, (run, _krx, _kry) in enumerate(kept):
            if idx not in alone:
                continue                           # a URL inside a sentence is not a stamp
            marker = stamp_marker(run.text)
            if not marker:
                continue
            dx, dy, w, h = _display(run.x, run.y, box, rot)
            # The key is the text alone. An earlier version keyed on the x/y fraction and
            # it was measured wrong on a 3,538-page manual: pages 488-509 are landscape
            # (1684x1191 against 595x842 elsewhere), so the same stamp, at the same place
            # on the paper, has an x fraction of 0.45 there and 0.37 on a portrait page.
            # It grouped as a separate candidate and those 22 pages kept their watermark.
            # "Same fraction of the page" is not a property a stamp has across page sizes.
            rx = round(dx / w, 2) if w else 0
            ry = round(dy / h, 2) if h else 0
            groups.setdefault(marker, Candidate(marker)).add(run, rx, ry)

        if i + 1 >= sample:
            hits = [c for c in groups.values() if c.confirm()]
            if hits:
                break
    if hits:
        hits += _companions(hits, per_page, sample)
    hits.sort(key=lambda c: -len(c.pages))
    return hits, painters


# How close a line has to sit to a confirmed stamp to count as part of it, as a fraction
# of the page. Not tuned: measured on the 1997 Mazda 626, the nag line sits 0.010 from the
# URL it was stamped with, and the NEXT-nearest run that repeats on every sampled page is
# 0.924 away -- a speck of OCR noise on the far side of the paper. Two orders of magnitude
# of daylight, so anything in between decides the same way. On the 346-page JK manual
# nothing else repeats at all, and the rule changes nothing there.
_ADJACENT = 0.05

# Two pages of a line are the same line if their baselines are within this fraction of the
# page, and a run is "on a line of its own" when everything else on that line comes to
# fewer than this many characters.
#
# MEASURED, on the six stamped files in a 3,007-file sweep. Other text sharing the marker's
# line came to 0 characters for `GETtheMANUALS.org` on three Jeep manuals and for
# `http://www.adultpdf.com` on the Mazda 626; 3 characters for the Mazda's nag line, whose
# neighbours are the OCR specks 'he' and 'i' off the scan underneath; and 124 characters
# for `: www.motul.fr` -- which is not a stamp at all but Motul's own address block on
# Motul's own datasheet, sharing its line with the street address, the telephone and fax
# numbers and a date. A re-distributor writes its mark on a line of its own; a manufacturer
# prints its URL inside a sentence. 0-3 against 124 is the whole discriminator.
_SAME_LINE = 0.004
_LINE_NEIGHBOURS = 16


def _uncrowded(entries):
    """Indices of the runs that sit on a line of their own.

    THIS IS A DAMAGE GUARD, not an optimisation. Without it `: www.motul.fr` repeats in a
    margin on every page of a Motul product datasheet and satisfies every other test, so
    the tool deletes a manufacturer's contact details from the manufacturer's own document
    -- and, once companion lines were added, took 'Web' and 'development' out of the
    address block with it. That file was flagged by the ORIGINAL bottom-band rule too; the
    band never protected against this, it just happened to look elsewhere.
    """
    order = sorted(range(len(entries)), key=lambda i: entries[i][2])
    out = set()
    for a, i in enumerate(order):
        ry = entries[i][2]
        n = 0
        for step in (-1, 1):                       # walk out until the line ends
            b = a + step
            while 0 <= b < len(order) and abs(entries[order[b]][2] - ry) <= _SAME_LINE:
                n += len(entries[order[b]][0].text.strip())
                if n > _LINE_NEIGHBOURS:
                    break
                b += step
        if n <= _LINE_NEIGHBOURS:
            out.add(i)
    return out


def _companions(hits, per_page, sample):
    """The rest of a multi-line stamp: lines that carry no marker of their own.

    A stamping tool writes a block, not a line. `looks_like_link` recognises the line with
    the URL in it and `looks_like_nag` the one that asks you to register, but a stamp can
    equally carry a line of plain prose that neither can see -- and removing half a stamp
    leaves the file still stamped. Measured: the Mazda 626's `Created by TIFF To PDF trial
    version...` on 662 pages, one line above the URL that WAS removed.

    Two conditions, and both are needed. A companion must REPEAT character for character
    on every sampled page that carries the stamp -- which is what keeps a page number or
    any per-page text out of it -- and it must sit ADJACENT to the stamp, which is what
    keeps a publisher's own running footer at the other end of the page out of it. Neither
    alone is safe, and the rule can only fire on a file where a marker was already
    confirmed, so it cannot reach a file that has no stamp at all.
    """
    stamp_texts = set()
    for c in hits:
        stamp_texts |= set(getattr(c, 'exact', None) or ())
    if not stamp_texts:
        return []

    anchors = {}                                   # page -> [(rx, ry)] of the stamp itself
    for i, runs in per_page.items():
        spots = [(rx, ry) for run, rx, ry in runs if run.text in stamp_texts]
        if spots:
            anchors[i] = spots
    if not anchors:
        return []

    near = collections.defaultdict(list)
    for i, spots in anchors.items():
        alone = _uncrowded(per_page[i])
        for idx, (run, rx, ry) in enumerate(per_page[i]):
            if run.text in stamp_texts or idx not in alone:
                continue
            if any(max(abs(rx - ax), abs(ry - ay)) <= _ADJACENT for ax, ay in spots):
                near[run.text].append((i, rx, ry))

    out = []
    for text, seen in near.items():
        pages = {i for i, _, _ in seen}
        if len(pages) < max(sample, len(anchors)):   # every stamped page, not merely some
            continue
        c = Candidate(text, companion=True)
        for i, rx, ry in seen:
            c.pages.add(i)
            c.seen.append((round(rx, 2), round(ry, 2)))
            c.texts[text] += 1
        if c.confirm(repeats=sample):
            out.append(c)
    return out


# ---------------------------------------------------------------- removal

def _strip(instructions, drop):
    """Rebuild a container's operators without `drop`, keeping the stream valid.

    `'` and `"` advance a line as well as show text, so they become `T*`; deleting them
    would pull every following line of that block up by one. Everything else in `drop`
    is a `Tj`/`TJ`/`Do`, none of which move anything.
    """
    out = []
    for i, ins in enumerate(instructions):
        if i in drop:
            op = str(ins.operator)
            if op in ("'", '"'):
                out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator('T*')))
            continue
        out.append(ins)
    # Drop BDC/EMC pairs that are now empty — the /Artifact wrapper the stamp lived in.
    cleaned, i = [], 0
    while i < len(out):
        op = str(out[i].operator)
        if op in ('BDC', 'BMC') and i + 1 < len(out) and str(out[i + 1].operator) == 'EMC':
            i += 2
            continue
        cleaned.append(out[i])
        i += 1
    return cleaned


def flagged_on_page(page, page_no, candidates, spots=None):
    """Operators on one page that draw a confirmed stamp, and that page's painting ops.

    The page is walked afresh rather than reusing detection's result. Detection only
    samples -- measured on a 189-page manual, sampling 60 pages found four distinct
    watermark form objects and missed a fifth, so nine pages kept their stamp when
    removal trusted the sample. Detection decides WHAT the stamp is; every page in scope
    is then searched for it.

    `spots`, when given, collects each flagged run's origin as a fraction of the page AS
    DISPLAYED. That is what the render audit needs: it has to know where the ink it is
    about to see vanish was supposed to be, now that no fixed margin band says so.

    THE `_uncrowded` TEST DELIBERATELY DOES NOT RUN HERE, only in `detect`. It answers
    "is this a stamp or part of a sentence?", which is a question about the file, settled
    once from the sampled pages -- not a question to re-ask on every page, where the answer
    turns on whatever noise the scan happens to carry. Measured by putting it here: the
    Mazda 626's page 662 is a dense scan, 1,300 runs against page 1's 168, so OCR specks
    (`:` `.` `'7` `-` `*`) land on the nag's baseline and come to 23 characters against
    page 1's 3. The nag survived on that one page of 662, and the audit -- correctly --
    failed the whole file and kept the original. Raising the threshold would only move the
    page it happens on.

    What keeps removal honest instead is `Candidate.matches`: a run is flagged only when
    its WHOLE text equals a text the stamp was confirmed on, so a marker embedded in a
    sentence can never match one. Detection decides WHAT the stamp is; this decides WHERE.
    """
    painters = collections.defaultdict(set)
    runs = page_runs(page, page_no, painters)
    box, rot = _box(page), _rotation(page)
    flags = collections.defaultdict(set)
    skipped = 0
    def _take(run):
        flags[run.where].add(run.op)
        if spots is not None:
            dx, dy, w, h = _display(run.x, run.y, box, rot)
            if w and h:
                spots.setdefault(page_no, []).append((dx / w, dy / h))

    for run in runs:
        if any(c.matches_text(run.text) for c in candidates):
            _take(run)                             # a companion line of a confirmed stamp
            continue
        link = stamp_marker(run.text)
        if not link:
            continue
        if any(c.matches(link, run.text) for c in candidates):
            _take(run)
        elif any(c.matches(link) for c in candidates):
            # The right stamp, but this operator's text is not the stamp's -- it has page
            # content merged into it. Deleting it would take that content with it, so it
            # stays, and the count says so on the report row.
            skipped += 1
    return flags, painters, skipped


def remove(pdf, candidates, pages=None, spots=None, flagger=None):
    """Delete every operator that draws a confirmed stamp. Returns a Removal.

    `spots` is filled in with {page index: [(x, y) fractions of the displayed page]} for
    every run removed, for the render audit to check against.

    `flagger(page, page_no, spots) -> (flags, painters, skipped)` replaces the stamp test
    with another one -- `strip_lines` passes its named-line test -- while everything below
    (call-site unhooking of a form, in-place edit of a mixed one, the shared-form refusal)
    stays the one implementation.
    """
    n = len(pdf.pages)
    scope = list(range(n)) if pages is None else [p for p in pages if 0 <= p < n]
    in_scope = set(scope)

    touched, dropped, dead_forms, rewritten, shared = set(), 0, set(), 0, 0
    mixed_done = set()
    merged = 0

    for i in scope:
        page = pdf.pages[i]
        ins = _instructions(page)
        if not ins:
            continue
        flags, painters, skip = (flagger(page, i, spots) if flagger is not None
                                 else flagged_on_page(page, i, candidates, spots))
        merged += skip
        if not flags:
            continue

        # A form all of whose painting operators are flagged is unhooked at the call
        # site; a form that also paints real content has its own stream edited, and only
        # when no page outside the scope invokes it.
        dead_here = {where[1] for where, ops in flags.items()
                     if where[0] == 'form' and ops >= painters[where]}
        mixed_here = {where[1]: ops for where, ops in flags.items()
                      if where[0] == 'form' and where[1] not in dead_here}

        drop = set(flags.get(('page', i), ()))
        xobjects = (page.get('/Resources') or {}).get('/XObject') or {}
        orphans = set()
        for j, instr in enumerate(ins):
            if str(instr.operator) != 'Do' or not instr.operands:
                continue
            name = str(instr.operands[0])
            try:
                xo = xobjects[name]
            except Exception:
                continue
            if xo.objgen in dead_here:
                drop.add(j)
                orphans.add(name)

        for objgen, ops in mixed_here.items():
            if objgen in mixed_done:
                touched.add(i)
                continue
            users = _form_users(pdf, objgen)
            if users - in_scope:
                shared += 1
                mixed_done.add(objgen)
                continue
            for name in list(xobjects.keys()):
                xo = xobjects[name]
                if xo.objgen == objgen:
                    xo.write(pikepdf.unparse_content_stream(
                        _strip(_instructions(xo), ops)))
                    rewritten += 1
                    mixed_done.add(objgen)
                    touched.add(i)
                    break

        if drop:
            page.Contents = pdf.make_stream(
                pikepdf.unparse_content_stream(_strip(ins, drop)))
            # Unhook the form from /Resources as well, so the object becomes unreachable
            # and qpdf leaves it out on save. Dropping only the `Do` was measured to keep
            # the stamp's bytes in the file -- invisible and unextractable, but still
            # there for anything that greps, and still restorable.
            for name in orphans:
                try:
                    del xobjects[name]
                except Exception:
                    pass
            touched.add(i)
            dropped += len(drop)
            dead_forms |= dead_here

    return Removal(sorted(touched), dropped, len(dead_forms), rewritten, shared, merged)


def _form_users(pdf, objgen):
    """Every page that invokes this form through its /Resources."""
    users = set()
    for i in range(len(pdf.pages)):
        xobjects = (pdf.pages[i].get('/Resources') or {}).get('/XObject') or {}
        for name in list(xobjects.keys()):
            try:
                if xobjects[name].objgen == objgen:
                    users.add(i)
                    break
            except Exception:
                pass
    return users


# ---------------------------------------------------------------- audit

def _flat(text):
    """A page's text as one lower-case alphanumeric stream, with layout thrown away.

    The audit compares these streams, not word sets. WHY, measured on a 9,192-page manual
    stamped with an email address: the stamp is drawn twice, one copy a hair off the other,
    and pypdf extracts the pair as one run in which the tail of the first copy and the head
    of the second are joined into a word that is in neither. Against a set of the stamp's
    own words that reads as a word the rewrite LOST, and the audit failed a correct output
    on every page. Removing the stamp can equally SPLIT a merged
    word and make one appear. Neither is a change to the page -- both are artefacts of how
    a text extractor joins two draws that overlap -- and a stream comparison is blind to
    them while still catching a genuinely missing word, which no stream can hide.
    """
    return re.sub(r'[^0-9a-z]+', '', (text or '').lower())


def _render_sample(pages, want):
    """Up to `want` page indices spread across `pages`, both ends included."""
    pages = list(pages)
    if want <= 0 or not pages:
        return []
    if len(pages) <= want:
        return pages
    step = (len(pages) - 1) / (want - 1)
    return sorted({pages[round(i * step)] for i in range(want)})


def _outline_count(reader):
    """How many bookmarks the document has, counting nested ones.

    These manuals navigate file-to-file through the outline tree, so a bookmark lost is
    content lost even when every page survives.
    """
    def walk(items):
        n = 0
        for it in items:
            if isinstance(it, list):
                n += walk(it)
            else:
                n += 1
        return n
    try:
        return walk(reader.outline)
    except Exception:
        return -1                                  # unreadable either side; not a verdict


def _page_text(page):
    try:
        return page.extract_text() or ''
    except Exception:
        return ''


def _annot_count(page):
    try:
        return len(page.get('/Annots') or [])
    except Exception:
        return 0


def _gs():
    """Ghostscript, the same one the compressor uses. None if it is not installed."""
    env = os.environ.get('JBIG2_GS')
    if env and os.path.exists(env):
        return env
    for name in ('gswin64c', 'gswin32c', 'gs'):
        found = shutil.which(name)
        if found:
            return found
    for base in (r'C:\Program Files\gs', r'C:\Program Files (x86)\gs'):
        hits = sorted(glob.glob(os.path.join(base, '*', 'bin', 'gswin64c.exe')))
        if hits:
            return hits[-1]
    return None


def _render(path, page_no, out_png, dpi=100):
    gs = _gs()
    if not gs:
        return False
    try:
        subprocess.run([gs, '-sDEVICE=png16m', f'-r{dpi}',
                        f'-dFirstPage={page_no}', f'-dLastPage={page_no}',
                        '-dNOPAUSE', '-dBATCH', '-dQUIET',
                        '-sOutputFile=' + str(out_png), str(path)],
                       capture_output=True, timeout=300)
    except Exception:
        return False
    return os.path.exists(out_png)


def audit(src_path, out_path, candidates, pages, render=False,
          render_sample=0, shots_from=None, spots=None):
    """Compare the rewrite against the SOURCE. Returns a list of fatal reasons.

    Text is checked on every touched page -- it is milliseconds each and it is the check
    that proves nothing but the stamp went. Rendering is far dearer, so `render_sample`
    renders a spread of N touched pages instead of all of them. The removal is the same
    operator on every page, so a defect in it shows on any page that carries the stamp; a
    sample is the right shape for a bulk run, while `render=True` (every page) stays there
    for a file being examined on its own.

    Built on pypdf and Ghostscript, which this project already depends on, rather than on
    PyMuPDF -- which would be both a new dependency and an AGPL one in an MIT tool.
    """
    from pypdf import PdfReader

    bad = []
    a, b = PdfReader(str(src_path)), PdfReader(str(out_path))
    if len(a.pages) != len(b.pages):
        return [f'page count {len(a.pages)} -> {len(b.pages)}']
    oa, ob = _outline_count(a), _outline_count(b)
    if oa >= 0 and ob >= 0 and ob < oa:
        bad.append(f'bookmarks lost: {oa} -> {ob}')

    # Every whole run text that was removed, not just the link inside it -- see
    # Candidate.label. Longest first, so removing `<name>@<host>.com` from the stream is
    # not pre-empted by removing the bare `<host>.com` inside it.
    stamps = set()
    for c in candidates:
        stamps.add(_flat(c.text))
        for t in getattr(c, 'texts', ()):
            stamps.add(_flat(t))
    stamps = sorted((x for x in stamps if x), key=len, reverse=True)

    shots = set((shots_from if shots_from is not None else pages) if render
                else _render_sample(shots_from if shots_from is not None else pages,
                                    render_sample))
    for i in pages:
        pa, pb = a.pages[i], b.pages[i]
        if _annot_count(pa) != _annot_count(pb):
            bad.append(f'page {i + 1}: annotations lost')
        want = _flat(_page_text(pa))
        for stamp in stamps:
            want = want.replace(stamp, '')
        got = _flat(_page_text(pb))
        if got != want:
            j = next((k for k in range(min(len(got), len(want)))
                      if got[k] != want[k]), min(len(got), len(want)))
            bad.append(f'page {i + 1}: text changed beyond the stamp '
                       f'(expected ...{want[max(0, j - 20):j + 20]!r}, '
                       f'got ...{got[max(0, j - 20):j + 20]!r})')
        if bad:
            return bad                             # one page is enough to reject the file

    bad += _render_audit(src_path, out_path, sorted(shots), spots or {})
    return bad


# How far a measured box may sit from where the operators said it would, as a fraction of
# the page. Not a tuned threshold: it covers glyph extent (the run origin is the text's
# baseline start, so the ink reaches above and to the right of it), antialiasing, and the
# fact that a file's pages are not all the same size -- measured on a 662-page Mazda 626,
# MediaBox 2499x3520 on page 1 against 2484x3509 on page 2, which is 0.6% on its own.
_SLOP = 0.05


def _render_audit(src_path, out_path, shots, spots):
    """Did ink change anywhere it had no business changing?

    This replaces a fixed margin band, and is strictly tighter than one. The band asked
    "is the change in the bottom 12% of the paper?", which passes a figure dropped from a
    footer and fails a stamp legitimately removed from mid-page. Two questions are asked
    instead, both derived from what was actually removed:

      1. CONTAINMENT -- the changed region must hold every flagged run's origin, padded by
         `_SLOP`. Ink vanished where the operators we deleted were drawing, which is what
         "we removed the stamp" means.
      2. CONSISTENCY -- every sampled page must change the same region of the paper. The
         stamp is one operator writing the same text in the same place on every page, so
         its footprint cannot vary; damage is content-dependent and does vary. This is the
         half that catches a dropped figure, which containment alone would not.

    Consistency needs two pages to say anything, so a single-page scope falls back to
    containment. Both are computed as fractions of the rendered page, never pixels: pages
    within one file are not all the same size.
    """
    bad, boxes = [], {}
    for i in shots:
        box, reason = _changed_box(src_path, out_path, i)
        if reason:
            bad.append(reason)
            continue
        if box is None:
            continue           # nothing visibly changed: an invisible stamp is not damage
        boxes[i] = box
        loose = (box[0] - _SLOP, box[1] - _SLOP, box[2] + _SLOP, box[3] + _SLOP)
        for rx, ry in spots.get(i, ()):
            # The render's y grows downward; `ry` grows up from the displayed bottom.
            iy = 1.0 - ry
            if not (loose[0] <= rx <= loose[2] and loose[1] <= iy <= loose[3]):
                bad.append(f'page {i + 1}: ink changed at {_fmt(box)} but the stamp was '
                           f'drawn at ({rx:.3f}, {iy:.3f}) -- something else moved')
                break

    if len(boxes) > 1:
        spread = max(max(b[k] for b in boxes.values()) - min(b[k] for b in boxes.values())
                     for k in range(4))
        if spread > _SLOP:
            worst = sorted(boxes.items(), key=lambda kv: kv[1])
            bad.append(f'page {worst[0][0] + 1} changed {_fmt(worst[0][1])} but page '
                       f'{worst[-1][0] + 1} changed {_fmt(worst[-1][1])} -- one stamp '
                       f'cannot leave two different holes ({spread:.3f} apart)')
    return bad


def _fmt(box):
    return '(' + ', '.join(f'{v:.3f}' for v in box) + ')'


def _changed_box(src_path, out_path, i):
    """Where ink changed on page `i`, as fractions of the rendered page, or None.

    Returns (box, reason). This is the check that sees what a READER sees. Text extraction
    cannot do that job alone: a stamp in a font with no /ToUnicode extracts as nothing, so
    the text check would pass a page that still shows the stamp and equally a page that
    lost a figure.
    """
    from PIL import Image, ImageChops

    tmp = tempfile.mkdtemp(prefix='wm_')
    try:
        pa, pb = os.path.join(tmp, 'a.png'), os.path.join(tmp, 'b.png')
        if not (_render(src_path, i + 1, pa) and _render(out_path, i + 1, pb)):
            return None, None                      # no Ghostscript: the text checks stand
        ia, ib = Image.open(pa).convert('RGB'), Image.open(pb).convert('RGB')
        if ia.size != ib.size:
            return None, f'page {i + 1}: page size changed {ia.size} -> {ib.size}'
        box = (ImageChops.difference(ia, ib).convert('L')
               .point(lambda v: 255 if v > 24 else 0).getbbox())
        w, h = ia.size
        ia.close()
        ib.close()
        if box is None or not (w and h):
            return None, None
        return (box[0] / w, box[1] / h, box[2] / w, box[3] / h), None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- caller-named lines
#
# Everything above FINDS a stamp: it needs a marker (a link, a licence nag) because nothing
# else tells somebody else's text from the manual's own. Some additions carry no marker at
# all. Measured on a 23,970-page repair manual that reached the archive as a third-party
# information service's printout of the maker's pages: every page carries that service's
# two-line running header ("<year> <make> <model>" / "<year> <GROUP> <article title>"),
# drawn in two to four show-text pieces and on 247 pages drawn twice over itself, and
# 13,461 figures carry a "Courtesy of <maker's sales company>" credit line the service
# added under the picture. Neither repeats in one place (the credit sits under each figure,
# wherever it falls), and neither has a marker. Detection cannot and must not find these;
# the CALLER names them, and this section removes exactly what was named.
#
# What keeps that safe is the unit it works on: a whole displayed LINE, never a run. A
# baseline is removed only when all of its text, joined in reading order, fully matches a
# rule (one or more copies of it, for the doubled headers). A matching phrase inside a
# longer line -- the same manual's "Courtesy Light Switch Assembly Connector", its "Door
# courtesy switch circuit" -- is part of a line that does not match, and stays. A band,
# when given, confines a rule to the top or bottom of the displayed page.

# Two runs share a line when their displayed baselines are this close, in points. The
# measured file draws a header's pieces on one baseline exactly; its closest distinct lines
# are 11 pt apart.
_BASELINE = 0.6


class LineRule:
    """A line the caller says is not the manual's: a regex the WHOLE line must match."""

    def __init__(self, pattern, band=None):
        self.pattern = pattern
        self.rx = re.compile(rf'(?:{pattern})(?:\s*(?:{pattern}))*')
        self.band = band                           # None, or ('top'|'bottom', fraction)

    def in_band(self, ry):
        """`ry` is the baseline's height as a fraction of the displayed page, 0 = bottom."""
        if not self.band:
            return True
        side, frac = self.band
        return ry >= 1 - frac if side == 'top' else ry <= frac

    def matches(self, line):
        return any(self.rx.fullmatch(t) for t in (line['spaced'], line['joined']))


def page_lines(page, page_no, painters):
    """The page's text as displayed lines: [{'runs', 'spaced', 'joined', 'ry', 'rect'}].

    Both joins are offered to a rule because a piece boundary may or may not be a word
    boundary: the measured header splits "2010 BRAKES Brake (Front)" | "(Preparation)" at a
    space the walker has stripped, and the credit line "Courtesy of <MAKER> MO" | "TOR SALES"
    in the middle of a word. `rect` is the paper the line's ink can reach, as fractions of
    the displayed page with y growing DOWN (the render's frame), for the render audit.
    """
    box, rot = _box(page), _rotation(page)
    placed = []
    for run in page_runs(page, page_no, painters):
        dx, dy, w, h = _display(run.x, run.y, box, rot)
        if w and h:
            ex, ey = (_display(run.ex, run.ey, box, rot)[:2] if run.ex is not None else (dx, dy))
            placed.append((dy, dx, run, w, h, ex, ey))
    placed.sort(key=lambda p: (-p[0], p[1]))
    lines = []
    for dy, dx, run, w, h, ex, ey in placed:
        if lines and abs(lines[-1]['dy'] - dy) <= _BASELINE:
            lines[-1]['items'].append((dx, run, ex, ey))
        else:
            lines.append({'dy': dy, 'w': w, 'h': h, 'items': [(dx, run, ex, ey)]})
    out = []
    for ln in lines:
        items = sorted(ln['items'], key=lambda it: it[0])
        runs = [it[1] for it in items]
        size = max((r.size for r in runs), default=0.0) or 12.0
        # Each run's extent is its start to where the walker's text position stood after it,
        # from the font's own widths (see `_advancer`); glyph ink can overhang its advance a
        # little, which the audit's padding covers. An earlier estimate of 0.62 em a
        # character both under-covered (a header's last piece, placed at its BT block's start
        # before the walker advanced, ended 1.5 pt past it) and over-covered (a header drawn
        # twice summed to past the page edge) -- and over-covering is not the safe side: a
        # bigger box is more paper the audit does not look at.
        xs = [x for dx, _, ex, _ in items for x in (dx, ex)]
        ys = [y for _, _, _, ey in items for y in (ln['dy'], ey)]
        w, h, dy = ln['w'], ln['h'], ln['dy']
        out.append({'runs': runs,
                    'spaced': ' '.join(r.text for r in runs),
                    'joined': ''.join(r.text for r in runs),
                    'ry': dy / h,
                    'rect': (min(xs) / w, 1 - (max(ys) + size * 1.05) / h,
                             max(xs) / w, 1 - (min(ys) - size * 0.35) / h)})
    return out


Removed = collections.namedtuple('Removed', 'order text line x ry where op')


def _line_flagger(rules, removed, rects, lines, left):
    """A `remove()` flagger for named lines. Fills `removed` {page: [Removed]} and `rects`
    {page: [rect]} for the audit, and counts the removed line texts in `lines` and the
    unmatched lines a banded rule's band held in `left`, for the report."""
    def flag(page, page_no, spots):
        painters = collections.defaultdict(set)
        flags = collections.defaultdict(set)
        for n, ln in enumerate(page_lines(page, page_no, painters)):
            hit = any(r.in_band(ln['ry']) and r.matches(ln) for r in rules)
            if not hit:
                if any(r.band and r.in_band(ln['ry']) for r in rules):
                    left[ln['spaced'][:80]] += 1
                continue
            lines[ln['spaced'][:80]] += 1
            for run in ln['runs']:
                flags[run.where].add(run.op)
                removed.setdefault(page_no, []).append(Removed(
                    (run.where[0] != 'page', str(run.where[1]), run.op), run.text, n,
                    run.x, ln['ry'], run.where, run.op))
            rects.setdefault(page_no, []).append(ln['rect'])
        return flags, painters, 0
    return flag


def _recheck_lines(page, page_no, items, rules):
    """The audit's own look at what was removed from one SOURCE page, trusting nothing the
    flagger said about lines. None if sound, else why not.

    1. Every removed line, rebuilt from the text of the runs actually taken, matches a rule
       inside its band -- so a line the flagger mislabelled is caught here.
    2. Every run the source draws on a removed line's baseline was taken too -- a line is
       removed whole or not at all, walked afresh from the content stream.
    """
    by_line = collections.defaultdict(list)
    for it in items:
        by_line[it.line].append(it)
    for its in by_line.values():
        its.sort(key=lambda it: it.x)
        probe = {'spaced': ' '.join(it.text for it in its), 'joined': ''.join(it.text for it in its)}
        if not any(r.in_band(its[0].ry) and r.matches(probe) for r in rules):
            return f'page {page_no + 1}: removed a line no rule names: {probe["spaced"][:60]!r}'
    taken = {(it.where, it.op) for it in items}
    box, rot = _box(page), _rotation(page)
    placed = []
    for run in page_runs(page, page_no, collections.defaultdict(set)):
        dx, dy, w, h = _display(run.x, run.y, box, rot)
        placed.append((dy, (run.where, run.op), run.text))
    removed_dy = {dy for dy, key, _ in placed if key in taken}
    for dy, key, text in placed:
        if key not in taken and any(abs(dy - r) <= _BASELINE for r in removed_dy):
            return f'page {page_no + 1}: part of a line was removed; {text[:40]!r} stayed on it'
    return None


def _removable(src, got, pieces, budget=20000):
    """True if deleting each of `pieces` once, at SOME occurrence, turns `src` into `got`.

    Which occurrence was drawn by the removed operator is not knowable from extracted text,
    and guessing one is wrong both ways, each measured on one page of a 23,970-page manual:
    pypdf extracted a component-locator page's running header ahead of the body the stream
    draws first, so deleting in drawing order looked in the wrong place; and that page's
    body opens with the title "<YEAR> <MAKE> / <MODEL>", which flattens to the same
    characters as the removed header "<Year> <Make> <Model>", so first-occurrence deletion
    took the body's copy. Both failed a correct rewrite.

    So every choice is tried, longest piece first, memoised. `budget` bounds the search;
    running out of it answers False, which fails the file -- the safe side. That a
    body copy of a piece's text is not what went is established apart from this, by
    `_recheck_lines` (whole named lines only, from the source's own runs) and the render
    check."""
    pieces = sorted((p for p in pieces if p), key=len, reverse=True)
    if len(src) - sum(map(len, pieces)) != len(got):
        return False
    seen = set()
    steps = [0]

    def go(s, i):
        if i == len(pieces):
            return s == got
        key = (s, i)
        if key in seen:
            return False
        seen.add(key)
        steps[0] += 1
        if steps[0] > budget:
            return False
        p = pieces[i]
        k = s.find(p)
        while k >= 0:
            if go(s[:k] + s[k + len(p):], i + 1):
                return True
            k = s.find(p, k + 1)
        return False

    return go(src, 0)


def audit_lines(src_path, out_path, removed, rects, text_pages, render_pages, rules):
    """The named-line removal against the SOURCE. Returns a list of fatal reasons.

    LINES (`_recheck_lines`): what was taken is whole lines that the rules name, judged from
    the source's own runs rather than from the flagger's account. Without this the text
    check below proves only that the flagged runs went -- measured in this repo's suite: a
    flagger made to mislabel a body line as the header passed every other check.

    TEXT: each checked page's text must be the source's with exactly the removed runs
    deleted, in drawing order, and nothing else missing or added.

    RENDER: every pixel that changed must lie inside a removed line's rectangle. This is
    the containment half of `_render_audit` made exact for lines; its consistency half
    ("one stamp leaves the same hole on every page") does not apply -- a credit line sits
    under each figure, wherever the figure is. It is stricter in the other direction: a
    changed pixel anywhere else on the page fails the file, however small.
    """
    from pypdf import PdfReader

    bad = []
    a, b = PdfReader(str(src_path)), PdfReader(str(out_path))
    if len(a.pages) != len(b.pages):
        return [f'page count {len(a.pages)} -> {len(b.pages)}']
    oa, ob = _outline_count(a), _outline_count(b)
    if oa >= 0 and ob >= 0 and ob < oa:
        return [f'bookmarks lost: {oa} -> {ob}']
    with pikepdf.open(str(src_path)) as src_pdf:
        for i in text_pages:
            why = _recheck_lines(src_pdf.pages[i], i, removed.get(i, []), rules)
            if why:
                return [why]
    for i in text_pages:
        pa, pb = a.pages[i], b.pages[i]
        if _annot_count(pa) != _annot_count(pb):
            return [f'page {i + 1}: annotations lost']
        pieces = [_flat(it.text) for it in sorted(removed.get(i, []))]
        src_text = _flat(_page_text(pa))
        got = _flat(_page_text(pb))
        if not _removable(src_text, got, pieces):
            j = next((k for k in range(min(len(got), len(src_text))) if got[k] != src_text[k]),
                     min(len(got), len(src_text)))
            return [f'page {i + 1}: text changed beyond the named lines '
                    f'(source ...{src_text[max(0, j - 20):j + 20]!r}, '
                    f'rewrite ...{got[max(0, j - 20):j + 20]!r})']
    for i in render_pages:
        reason = _changed_outside(src_path, out_path, i, rects.get(i, []))
        if reason:
            bad.append(reason)
            break
    return bad


def _changed_outside(src_path, out_path, i, rects, pad=0.004):
    """None if every changed pixel of page `i` lies inside one of `rects`, else why not."""
    from PIL import Image, ImageChops, ImageDraw

    tmp = tempfile.mkdtemp(prefix='wl_')
    try:
        pa, pb = os.path.join(tmp, 'a.png'), os.path.join(tmp, 'b.png')
        if not (_render(src_path, i + 1, pa) and _render(out_path, i + 1, pb)):
            return None                            # no Ghostscript: the text check stands
        ia, ib = Image.open(pa).convert('RGB'), Image.open(pb).convert('RGB')
        if ia.size != ib.size:
            return f'page {i + 1}: page size changed {ia.size} -> {ib.size}'
        diff = (ImageChops.difference(ia, ib).convert('L')
                .point(lambda v: 255 if v > 24 else 0))
        w, h = diff.size
        draw = ImageDraw.Draw(diff)
        for x0, y0, x1, y1 in rects:
            draw.rectangle([(x0 - pad) * w, (y0 - pad) * h, (x1 + pad) * w, (y1 + pad) * h],
                           fill=0)
        left = diff.getbbox()
        ia.close()
        ib.close()
        if left:
            return (f'page {i + 1}: ink changed outside the removed lines at '
                    f'{_fmt((left[0] / w, left[1] / h, left[2] / w, left[3] / h))}')
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def strip_lines(src, out, rules, pages=None, text_sample=0, render_sample=24, apply_=True):
    """Remove every displayed line that a rule names. Writes `out` only if the audit passes.

        rules         [LineRule]
        text_sample   touched pages whose text is audited; 0 = every one
        render_sample touched pages rendered and compared, spread across the file

    Returns {'pages', 'ops', 'lines': Counter of removed line texts, 'left_in_band':
    Counter of lines a banded rule's band held that no rule matched, 'err'}. The last is
    for the caller to read: a band that still holds text after the run means a rule missed
    a shape, which is better reported than guessed at.
    """
    removed, rects = {}, {}
    lines, left = collections.Counter(), collections.Counter()
    with pikepdf.open(str(src)) as pdf:
        n = len(pdf.pages)
        scope = pages if pages is not None else list(range(n))
        rem = remove(pdf, [], pages=scope,
                     flagger=_line_flagger(rules, removed, rects, lines, left))
        res = {'pages': len(rem.pages), 'ops': rem.ops, 'forms': rem.forms,
               'shared': rem.shared, 'lines': lines, 'left_in_band': left, 'err': None}
        if not rem.pages or not apply_:
            return res
        tmp = str(out) + '.wl.tmp'
        pdf.save(tmp)
    text_pages = (list(rem.pages) if not text_sample
                  else _render_sample(rem.pages, text_sample))
    bad = audit_lines(str(src), tmp, removed, rects, text_pages,
                      _render_sample(rem.pages, render_sample), rules)
    if bad:
        _unlink(tmp)
        res.update(pages=0, ops=0, err='; '.join(bad[:3]))
        return res
    os.replace(tmp, str(out))
    return res


def parse_band(spec):
    """'top:0.08' -> ('top', 0.08); None stays None."""
    if not spec:
        return None
    side, _, frac = spec.partition(':')
    if side not in ('top', 'bottom') or not frac:
        raise ValueError(f'band must be top:<fraction> or bottom:<fraction>, not {spec!r}')
    return side, float(frac)


# ---------------------------------------------------------------- a band's own graphics
#
# Taking a running header's text off leaves its frame: measured on the same 23,970-page
# printout, an outer box and two shaded bars drawn as vector paths in the top 7% of every
# page, which read as an empty grey banner once the words were gone. `--band-graphics`
# removes the paths drawn entirely inside the band, and only those.
#
# Two guards, both from that file. A path reaching the page's side margins is not the
# header's: the printout also frames every page with a thin border, whose top edge runs
# through the band, and removing it would leave the frame open at the top. And a clipping
# path is never removed, because content drawn after it would lose its clip and could show
# anywhere on the page. Text, images and form XObjects are not touched at all.

_PATH_BUILD = {'m': 1, 'l': 1, 'c': 3, 'v': 2, 'y': 2, 're': 0, 'h': 0}
_PATH_PAINT = {'S', 's', 'f', 'F', 'f*', 'B', 'B*', 'b', 'b*', 'n'}
_SIDE_INSET = 10.0        # points: a path this close to either side edge may be a page border


def _band_paths(page, band):
    """Indices of the page's own path operators (construction and painting) whose every
    point lies inside `band` and clear of the side margins."""
    ins = _instructions(page)
    box, rot = _box(page), _rotation(page)
    probe = LineRule('', band)
    ctm, stack = (1, 0, 0, 1, 0, 0), []
    cur, pts, clip, drop = [], [], False, set()

    def inside(x, y):
        dx, dy, w, h = _display(x, y, box, rot)
        return bool(w and h) and probe.in_band(dy / h) and _SIDE_INSET <= dx <= w - _SIDE_INSET

    for i, x in enumerate(ins):
        op = str(x.operator)
        if op == 'q':
            stack.append(ctm)
        elif op == 'Q':
            ctm = stack.pop() if stack else ctm
        elif op == 'cm' and len(x.operands) == 6:
            ctm = _mul(tuple(_num(a) for a in x.operands), ctm)
        elif op in _PATH_BUILD:
            a = [_num(v) for v in x.operands]
            cur.append(i)
            if op == 're' and len(a) == 4:
                corners = [(a[0], a[1]), (a[0] + a[2], a[1]), (a[0], a[1] + a[3]),
                           (a[0] + a[2], a[1] + a[3])]
            else:
                corners = [(a[k], a[k + 1]) for k in range(0, len(a) - 1, 2)]
            for px, py in corners:
                d = _mul((1, 0, 0, 1, px, py), ctm)
                pts.append((d[4], d[5]))
        elif op in ('W', 'W*'):
            clip = True
            cur.append(i)
        elif op in _PATH_PAINT:
            cur.append(i)
            if op != 'n' and not clip and pts and all(inside(px, py) for px, py in pts):
                drop |= set(cur)
            cur, pts, clip = [], [], False
    return ins, drop


def strip_band_graphics(src, out, band, pages=None, text_sample=0, render_sample=24,
                        apply_=True):
    """Remove the vector paths drawn entirely inside `band`. Writes `out` only if the audit
    passes: page count, bookmarks and annotations kept; the text of every checked page
    unchanged; and on a rendered sample, no pixel changed outside the band."""
    if not band:
        raise ValueError('band graphics need a band')
    touched, ops = [], 0
    with pikepdf.open(str(src)) as pdf:
        n = len(pdf.pages)
        for i in (pages if pages is not None else range(n)):
            page = pdf.pages[i]
            ins, drop = _band_paths(page, band)
            if drop:
                page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(_strip(ins, drop)))
                touched.append(i)
                ops += len(drop)
        res = {'pages': len(touched), 'ops': ops, 'err': None}
        if not touched or not apply_:
            return res
        tmp = str(out) + '.wg.tmp'
        pdf.save(tmp)

    from pypdf import PdfReader
    bad = []
    a, b = PdfReader(str(src)), PdfReader(tmp)
    oa, ob = _outline_count(a), _outline_count(b)
    if len(a.pages) != len(b.pages):
        bad.append(f'page count {len(a.pages)} -> {len(b.pages)}')
    elif oa >= 0 and ob >= 0 and ob < oa:
        bad.append(f'bookmarks lost: {oa} -> {ob}')
    else:
        for i in (touched if not text_sample else _render_sample(touched, text_sample)):
            if _annot_count(a.pages[i]) != _annot_count(b.pages[i]):
                bad.append(f'page {i + 1}: annotations lost')
                break
            if _flat(_page_text(a.pages[i])) != _flat(_page_text(b.pages[i])):
                bad.append(f'page {i + 1}: text changed')
                break
    if not bad:
        side, frac = band
        area = [(0.0, 0.0, 1.0, frac)] if side == 'top' else [(0.0, 1.0 - frac, 1.0, 1.0)]
        for i in _render_sample(touched, render_sample):
            reason = _changed_outside(str(src), tmp, i, area, pad=0.0)
            if reason:
                bad.append(reason.replace('removed lines', 'band'))
                break
    if bad:
        _unlink(tmp)
        res.update(pages=0, ops=0, err='; '.join(bad[:3]))
        return res
    os.replace(tmp, str(out))
    return res


# ---------------------------------------------------------------- driver

def _page_spec(spec, n):
    if not spec:
        return None
    out = []
    for part in spec.split(','):
        part = part.strip()
        if '-' in part:
            a, b = part.split('-', 1)
            out += list(range(int(a) - 1, min(int(b), n)))
        elif part:
            out.append(int(part) - 1)
    return [p for p in out if 0 <= p < n]


def clean_file(src, out, sample=2, render=False,
               render_sample=8, pages=None):
    """Write a de-watermarked copy of `src` to `out`. Returns a dict; writes nothing on
    failure or when there is nothing to remove.

        found    [(text, where, pages seen)] -- empty means no stamp, `out` not written
        pages    how many pages were changed
        ops      operators deleted
        forms    form XObjects dropped
        err      why `out` was not written, or None

    This is the entry point for a caller inside another pipeline; `process` below is the
    command line around it. A caller that rasterises pages MUST run this on its SOURCE:
    once a page is rendered to an image the stamp is baked into the pixels and no
    operator-level removal is possible any more.
    """
    with pikepdf.open(str(src)) as pdf:
        n = len(pdf.pages)
        hits, _ = detect(pdf, sample=sample)
        if not hits:
            return {'found': [], 'pages': 0, 'ops': 0, 'forms': 0, 'err': None}
        scope = pages if pages is not None else list(range(n))
        spots = {}
        rem = remove(pdf, hits, pages=scope, spots=spots)
        found = [(c.label(), c.where(), len(c.pages)) for c in hits]
        if not rem.pages:
            return {'found': found, 'pages': 0, 'ops': 0, 'forms': 0,
                    'err': 'found but nothing removable'}
        tmp = str(out) + '.wm.tmp'
        pdf.save(tmp)

    # An `exact` match cannot have taken page content with it (see Candidate.confirm),
    # so the extraction audit is defence in depth on a sample. Without one, the link match
    # COULD have caught an operator carrying content, and every touched page is read.
    exact = all(getattr(c, 'exact', None) for c in hits)
    text_pages = (_render_sample(rem.pages, 12) if exact else list(rem.pages))
    bad = audit(str(src), tmp, hits, text_pages, render=render,
                render_sample=render_sample, shots_from=list(rem.pages), spots=spots)
    if bad:
        _unlink(tmp)
        return {'found': found, 'pages': 0, 'ops': 0, 'forms': 0,
                'err': '; '.join(bad[:3])}
    os.replace(tmp, str(out))
    return {'found': found, 'pages': len(rem.pages), 'ops': rem.ops, 'forms': rem.forms,
            'rewritten': rem.rewritten, 'shared': rem.shared, 'merged': rem.merged,
            'exact': exact, 'err': None}


def note_for(res) -> str:
    """One parenthesised clause for a report row, or '' when there is nothing to say."""
    if not res or not res.get('found'):
        return ''
    what = ', '.join(repr(t) for t, _, _ in res['found'])
    if res.get('err'):
        return f' (watermark {what} found but NOT removed: {res["err"]})'
    note = (f' (watermark {what} removed from {res["pages"]} pages, '
            f'{res["ops"]} operators)')
    if res.get('merged'):
        note += (f' ({res["merged"]} left: the stamp shares an operator with page '
                 f'content there)')
    return note


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


def process(path, out=None, pages=None, apply_=False,
            sample=2, render=False, keep_pages_only=False):
    """Report — and if asked, rewrite — one file. Returns a status line."""
    with pikepdf.open(path) as pdf:
        n = len(pdf.pages)
        want = _page_spec(pages, n)
        hits, _ = detect(pdf, sample=sample)
        if not hits:
            return f'{os.path.basename(path)}: no repeating link found ({n} pages)'
        found = ', '.join(f'{c.label()!r} at {c.where()}, same on '
                          f'{len(c.pages)} leading pages' for c in hits)
        if not apply_:
            return f'{os.path.basename(path)}: {found} [{n} pages] -- report only'

        scope = want if want is not None else list(range(n))
        if keep_pages_only and want:
            # A small proof copy: keep only the pages under test, so a reviewer can open
            # 8 pages instead of 3,500 and the audit still compares like for like.
            for i in reversed([p for p in range(n) if p not in set(want)]):
                del pdf.pages[i]
            scope = list(range(len(pdf.pages)))
            hits, _ = detect(pdf, sample=sample)
        spots = {}
        rem = remove(pdf, hits, pages=scope, spots=spots)
        tmp = (out or path) + '.wm.tmp'
        pdf.save(tmp)

    src_for_audit = path
    audit_pages = list(rem.pages)
    if keep_pages_only and want:
        # The proof copy is a different document; audit it against a source cut the same
        # way, or every page number would compare against the wrong page.
        cut = (out or path) + '.src.tmp'
        with pikepdf.open(path) as pdf:
            for i in reversed([p for p in range(n) if p not in set(want)]):
                del pdf.pages[i]
            pdf.save(cut)
        src_for_audit = cut

    bad = audit(src_for_audit, tmp, hits, audit_pages, render=render, spots=spots)
    if src_for_audit != path:
        os.replace(src_for_audit, (out or path) + '.source-sample.pdf')
    if bad:
        os.remove(tmp)
        return (f'{os.path.basename(path)}: FAILED, original kept -- '
                + '; '.join(bad[:4]))
    dest = out or path
    os.replace(tmp, dest)
    note = (f'{os.path.basename(path)}: removed {found}; '
            f'{len(rem.pages)} pages, {rem.ops} operators, {rem.forms} forms dropped')
    if rem.rewritten:
        note += f', {rem.rewritten} forms rewritten'
    if rem.shared:
        note += f', {rem.shared} shared forms left alone'
    if rem.merged:
        note += f', {rem.merged} left (stamp merged with page content)'
    return note + (' [render-verified]' if render else '')


def _pdfs_under(path):
    if os.path.isfile(path):
        return [path]
    out = []
    for root, _, files in os.walk(path):
        out += [os.path.join(root, f) for f in sorted(files)
                if f.lower().endswith('.pdf')]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[3].strip(),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('path')
    ap.add_argument('--apply', action='store_true',
                    help='rewrite (in place unless --dest is given)')
    ap.add_argument('--dest', help='write results into this directory instead')
    ap.add_argument('--pages', help='only these pages, e.g. 1-8 or 1,5,9')
    ap.add_argument('--only-pages', action='store_true',
                    help='with --pages, output just those pages as a proof copy')
    ap.add_argument('--sample', type=int, default=2,
                    help='leading pages that must carry the same stamp for it to count '
                         '(default 2). This stamp is added in one pass over the whole '
                         'file, so two pages settle it; more front matter is read only '
                         'if those pages carry no stamp at all')
    ap.add_argument('--verify-render', action='store_true',
                    help='also compare rendered pixels; fail if ink changed elsewhere')
    ap.add_argument('--strip-line', action='append', metavar='REGEX',
                    help='instead of detecting a stamp, remove every displayed line whose '
                         'WHOLE text matches REGEX (one or more copies of it); repeatable. '
                         'For additions with no marker, e.g. a printout service\'s running '
                         'header. See "caller-named lines" in the source')
    ap.add_argument('--band', metavar='top:F|bottom:F',
                    help='with --strip-line, only lines whose baseline is in the top (or '
                         'bottom) fraction F of the displayed page')
    ap.add_argument('--band-graphics', action='store_true',
                    help='with --band: remove the vector paths drawn entirely inside the band '
                         '(a running header\'s frame), never one reaching the side margins '
                         'and never a clipping path')
    ap.add_argument('--text-sample', type=int, default=0,
                    help='with --strip-line, audit the text of this many touched pages '
                         '(default 0 = every one)')
    args = ap.parse_args(argv)

    if pikepdf is None:
        print('pikepdf is required', file=sys.stderr)
        return 2
    if args.dest:
        os.makedirs(args.dest, exist_ok=True)

    if args.band_graphics:
        band = parse_band(args.band)
        for path in _pdfs_under(args.path):
            out = os.path.join(args.dest, os.path.basename(path)) if args.dest else path
            with pikepdf.open(path) as probe:
                pages = _page_spec(args.pages, len(probe.pages))
            res = strip_band_graphics(path, out, band, pages=pages,
                                      text_sample=args.text_sample, apply_=args.apply)
            verb = ('removed' if args.apply and not res['err'] else
                    'NOT removed' if res['err'] else 'would remove')
            print(f"{os.path.basename(path)}: {verb} {res['ops']} path operators on "
                  f"{res['pages']} pages" + (f" -- {res['err']}" if res['err'] else ''))
        return 0

    if args.strip_line:
        band = parse_band(args.band)
        rules = [LineRule(p, band) for p in args.strip_line]
        for path in _pdfs_under(args.path):
            out = os.path.join(args.dest, os.path.basename(path)) if args.dest else path
            with pikepdf.open(path) as probe:
                pages = _page_spec(args.pages, len(probe.pages))
            res = strip_lines(path, out, rules, pages=pages, text_sample=args.text_sample,
                              apply_=args.apply)
            verb = ('removed' if args.apply and not res['err'] else
                    'NOT removed' if res['err'] else 'would remove')
            print(f"{os.path.basename(path)}: {verb} {sum(res['lines'].values())} lines, "
                  f"{len(res['lines'])} distinct"
                  + (f" -- {res['err']}" if res['err'] else
                     f", {res['pages']} pages, {res['ops']} operators"))
            for t, c in res['lines'].most_common(8):
                print(f'    {c:6d}  {t}')
            if res['left_in_band']:
                print(f"  left in the band, matching no rule: {sum(res['left_in_band'].values())}")
                for t, c in res['left_in_band'].most_common(8):
                    print(f'    {c:6d}  {t}')
        return 0

    for path in _pdfs_under(args.path):
        out = os.path.join(args.dest, os.path.basename(path)) if args.dest else None
        try:
            print(process(path, out=out, pages=args.pages, apply_=args.apply,
                          sample=args.sample,
                          render=args.verify_render,
                          keep_pages_only=args.only_pages))
        except Exception as exc:
            print(f'{os.path.basename(path)}: ERROR {exc.__class__.__name__}: {exc}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
