#!/usr/bin/env python3
"""
pdfwatermark.py

Find and remove a re-distributor's stamp from a manual: text that looks like a link,
sits in a page margin, and repeats on nearly every page.

    python pdfwatermark.py <file-or-dir>                  # report only
    python pdfwatermark.py <file-or-dir> --apply          # rewrite in place
    python pdfwatermark.py <file> --pages 1-8 --dest DIR  # a few pages, to a copy

WHAT IT LOOKS FOR, and why all three conditions are needed. A stamp is *link-like*
(`example.org`, `www.x.net`, `https://…`), it is in a **margin band** — the
bottom 12% of the page by default — and it **repeats** on at least half the pages
sampled at the same place. Each condition alone is a false-positive machine: a manual
cites real URLs in its body, a page number is in the bottom band on every page, and a
running footer repeats. Together they describe something the publisher did not put
there. Nothing about a specific site is hard-coded; the domain is whatever was found.

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
  * rendered pixels (--verify-render): the changed region must lie inside the margin
    band the watermark was found in. A page whose ink changed anywhere else is damage,
    whatever the file size says.

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
Run = collections.namedtuple('Run', 'text x y page where op')

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

_SHOW = ('Tj', 'TJ', "'", '"')


def looks_like_link(text):
    m = _URLISH.search(text or '')
    return m.group(0).lower() if m else None


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
    decode = _decoder(None)
    fonts = (resources or {}).get('/Font') or {}
    xobjects = (resources or {}).get('/XObject') or {}

    for i, ins in enumerate(instructions):
        op = str(ins.operator)
        args = ins.operands
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
            decode = _decoder(font)
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
            if op in ("'", '"'):
                tlm = _mul((1, 0, 0, 1, 0, -leading), tlm)
                tm = tlm
            parts = []
            for a in args:
                if isinstance(a, pikepdf.String):
                    parts.append(decode(bytes(a)))
                elif isinstance(a, pikepdf.Array):
                    for el in a:
                        if isinstance(el, pikepdf.String):
                            parts.append(decode(bytes(el)))
            text = ''.join(parts).strip()
            dev = _mul(tm, ctm)
            painters[where].add(i)
            if text:
                runs.append(Run(text, dev[4], dev[5], page_no, where, i))
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


def band_of(y, y0, h, margin):
    if h <= 0:
        return None
    r = (y - y0) / h
    if r <= margin:
        return 'bottom'
    if r >= 1 - margin:
        return 'top'
    return None


class Candidate:
    """One repeating link-like margin stamp, and every operator that draws it."""

    def __init__(self, text, band):
        self.text = text                               # the link, which is the identity
        self.band = band
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

    def matches(self, link, band, text=None):
        if link != self.text or band != self.band:
            return False
        if text is None or not getattr(self, 'exact', None):
            return True                            # no repeating text: match on the link
        return text in self.exact

    def where(self):
        xs = sorted(x for x, _ in self.seen)
        ys = sorted(y for _, y in self.seen)
        return f'x {xs[0]:.2f}-{xs[-1]:.2f}, y {ys[0]:.2f}-{ys[-1]:.2f}'


def detect(pdf, sample=2, margin=0.12, band='bottom', give_up=8):
    """Candidates that are link-like, in a margin band, and drawn by the SAME operator on
    consecutive pages. Reads from the front and stops as soon as one repeats.

    This kind of stamp is on every page or on none: it is added in one pass by whoever
    passed the file on, to sign it, and the pass writes the same operator into every page.
    So two pages settle it. Detection reads page 1 and page 2, and a run that is link-like,
    in the same margin band, with CHARACTER-FOR-CHARACTER the same text on both, is the
    stamp; anything else is not.

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
    hits = []
    for i in range(min(n, max(sample, give_up))):
        page = pdf.pages[i]
        x0, y0, w, h = _box(page)
        for run in page_runs(page, i, painters):
            link = looks_like_link(run.text)
            if not link:
                continue
            b = band_of(run.y, y0, h, margin)
            if b is None or (band != 'any' and b != band):
                continue
            # The key is the text and the band, NOT the exact spot. An earlier version
            # keyed on the x/y fraction too and it was measured wrong on a 3,538-page
            # manual: pages 488-509 are landscape (1684x1191 against 595x842 elsewhere),
            # so the same stamp, at the same place on the paper, has an x fraction of
            # 0.45 there and 0.37 on a portrait page. It grouped as a separate candidate
            # and those 22 pages kept their watermark. Position still has to be a margin
            # band -- that is what keeps a URL in the body text out of it -- but "same
            # fraction of the page width" is not a property a stamp has across page sizes.
            rx = round((run.x - x0) / w, 2) if w else 0
            ry = round((run.y - y0) / h, 2) if h else 0
            groups.setdefault((link, b), Candidate(link, b)).add(run, rx, ry)

        if i + 1 >= sample:
            hits = [c for c in groups.values() if c.confirm()]
            if hits:
                break
    hits.sort(key=lambda c: -len(c.pages))
    return hits, painters


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


def flagged_on_page(page, page_no, candidates, margin, band_want):
    """Operators on one page that draw a confirmed stamp, and that page's painting ops.

    The page is walked afresh rather than reusing detection's result. Detection only
    samples -- measured on a 189-page manual, sampling 60 pages found four distinct
    watermark form objects and missed a fifth, so nine pages kept their stamp when
    removal trusted the sample. Detection decides WHAT the stamp is; every page in scope
    is then searched for it.
    """
    painters = collections.defaultdict(set)
    runs = page_runs(page, page_no, painters)
    x0, y0, w, h = _box(page)
    flags = collections.defaultdict(set)
    skipped = 0
    for run in runs:
        link = looks_like_link(run.text)
        if not link:
            continue
        b = band_of(run.y, y0, h, margin)
        if b is None or (band_want != 'any' and b != band_want):
            continue
        if any(c.matches(link, b, run.text) for c in candidates):
            flags[run.where].add(run.op)
        elif any(c.matches(link, b) for c in candidates):
            # Right stamp, right place, but this operator's text is not the stamp's --
            # it has page content merged into it. Deleting it would take that content
            # with it, so it stays, and the count says so on the report row.
            skipped += 1
    return flags, painters, skipped


def remove(pdf, candidates, pages=None, margin=0.12, band='bottom'):
    """Delete every operator that draws a confirmed stamp. Returns a Removal."""
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
        flags, painters, skip = flagged_on_page(page, i, candidates, margin, band)
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


def audit(src_path, out_path, candidates, pages, margin=0.12, render=False,
          render_sample=0, shots_from=None):
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

    for i in sorted(shots):
        reason = _render_diff(src_path, out_path, i, margin)
        if reason:
            bad.append(reason)
    return bad


def _render_diff(src_path, out_path, i, margin):
    """Where did ink change? Anything outside the margin bands is damage.

    This is the check that sees what a READER sees. Text extraction cannot do that job
    alone: a stamp in a font with no /ToUnicode extracts as nothing, so the text check
    would pass a page that still shows the stamp and equally a page that lost a figure.
    """
    from PIL import Image, ImageChops

    tmp = tempfile.mkdtemp(prefix='wm_')
    try:
        pa, pb = os.path.join(tmp, 'a.png'), os.path.join(tmp, 'b.png')
        if not (_render(src_path, i + 1, pa) and _render(out_path, i + 1, pb)):
            return None                            # no Ghostscript: the text checks stand
        ia, ib = Image.open(pa).convert('RGB'), Image.open(pb).convert('RGB')
        if ia.size != ib.size:
            return f'page {i + 1}: page size changed {ia.size} -> {ib.size}'
        box = (ImageChops.difference(ia, ib).convert('L')
               .point(lambda v: 255 if v > 24 else 0).getbbox())
        ia.close()
        ib.close()
        if box is None:
            return None
        h = ia.size[1]
        # image coordinates grow downward, so the bottom band is the high end
        if box[3] <= h * margin or box[1] >= h * (1 - margin):
            return None
        return (f'page {i + 1}: pixels changed outside the margin bands, '
                f'bbox={box} of height {h}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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


def clean_file(src, out, margin=0.12, band='bottom', sample=2, render=False,
               render_sample=8, pages=None):
    """Write a de-watermarked copy of `src` to `out`. Returns a dict; writes nothing on
    failure or when there is nothing to remove.

        found    [(text, band, pages seen)] -- empty means no stamp, `out` not written
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
        hits, _ = detect(pdf, sample=sample, margin=margin, band=band)
        if not hits:
            return {'found': [], 'pages': 0, 'ops': 0, 'forms': 0, 'err': None}
        scope = pages if pages is not None else list(range(n))
        rem = remove(pdf, hits, pages=scope, margin=margin, band=band)
        found = [(c.label(), c.band, len(c.pages)) for c in hits]
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
    bad = audit(str(src), tmp, hits, text_pages, margin=margin, render=render,
                render_sample=render_sample, shots_from=list(rem.pages))
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


def process(path, out=None, pages=None, apply_=False, margin=0.12, band='bottom',
            sample=2, render=False, keep_pages_only=False):
    """Report — and if asked, rewrite — one file. Returns a status line."""
    with pikepdf.open(path) as pdf:
        n = len(pdf.pages)
        want = _page_spec(pages, n)
        hits, _ = detect(pdf, sample=sample, margin=margin, band=band)
        if not hits:
            return f'{os.path.basename(path)}: no repeating margin link found ({n} pages)'
        found = ', '.join(f'{c.label()!r} {c.band} ({c.where()}), same on '
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
            hits, _ = detect(pdf, sample=sample, margin=margin, band=band)
        rem = remove(pdf, hits, pages=scope, margin=margin, band=band)
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

    bad = audit(src_for_audit, tmp, hits, audit_pages, margin=margin, render=render)
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
    ap.add_argument('--band', choices=('bottom', 'top', 'any'), default='bottom')
    ap.add_argument('--margin', type=float, default=0.12,
                    help='margin band as a fraction of page height (default 0.12)')
    ap.add_argument('--sample', type=int, default=2,
                    help='leading pages that must carry the same stamp for it to count '
                         '(default 2). This stamp is added in one pass over the whole '
                         'file, so two pages settle it; more front matter is read only '
                         'if those pages carry no stamp at all')
    ap.add_argument('--verify-render', action='store_true',
                    help='also compare rendered pixels; fail if ink changed elsewhere')
    args = ap.parse_args(argv)

    if pikepdf is None:
        print('pikepdf is required', file=sys.stderr)
        return 2
    if args.dest:
        os.makedirs(args.dest, exist_ok=True)

    for path in _pdfs_under(args.path):
        out = os.path.join(args.dest, os.path.basename(path)) if args.dest else None
        try:
            print(process(path, out=out, pages=args.pages, apply_=args.apply,
                          margin=args.margin, band=args.band, sample=args.sample,
                          render=args.verify_render,
                          keep_pages_only=args.only_pages))
        except Exception as exc:
            print(f'{os.path.basename(path)}: ERROR {exc.__class__.__name__}: {exc}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
