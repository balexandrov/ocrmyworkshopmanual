#!/usr/bin/env python3
"""
pdffonts.py

Merge the per-page subsets of one embedded TrueType face back into one font.

    python pdffonts.py <file-or-dir>                  # report only
    python pdffonts.py <file-or-dir> --apply          # rewrite in place
    python pdffonts.py <file> --dest DIR --apply      # rewrite to a copy

WHAT IT FIXES. Some PDF producers embed a fresh subset of the same face on every page, each
under its own scrambled name. Measured on a 1,449-page chapter of a repair-information
printout: 733 simple TrueType fonts, 755 embedded font programs, 18.3 MB of a 29.3 MB file
-- against 4 font programs in a 1,252-page chapter of the same book printed without it. The
733 were four faces. Every subset carries the face's full hinting programs (fpgm, prep,
cvt), so ~25 KB each for the 6-20 glyphs a page uses.

WHEN TWO SUBSETS ARE ONE FACE -- all of these, each measured to hold on the file above:

  * simple /TrueType, embedded /FontFile2, not symbolic-by-encoding (no /Differences), and
    no /ToUnicode (a /ToUnicode would be per-subset and is left alone rather than merged)
  * the same unitsPerEm, head flags and table set, and byte-identical fpgm, prep and cvt:
    a glyph's instructions call into these, so a glyph moved to another subset's font must
    find the same functions there
  * the same descriptor /Flags and /ItalicAngle
  * every character code both subsets draw has a byte-identical glyph outline, and they
    share at least `MIN_SHARED` codes -- two faces that happen to agree on three glyphs are
    not one face. Measured: zero conflicting codes among the 733.
  * the PDF /Widths agree for every shared code (0 conflicts measured)

WHAT IS WRITTEN. One font per face: the member with most glyphs, plus every glyph another
member draws that it lacks (composites with their components), mapped under the same codes
in every cmap subtable. Each member's font dictionary is pointed at one shared descriptor
and font program; its /FirstChar, /LastChar, /Widths and /Encoding stay exactly as they
were, and no content stream is touched -- a simple font's codes ARE the characters, so the
same bytes find the same glyph in the merged font.

THE AUDIT DECIDES. The rewrite is compared with the source before anything is kept, and the
source stays if a check fails: page count, bookmarks and annotations; every page's extracted
text identical; a spread of pages rendered and compared pixel for pixel; and the file must
be smaller.
"""
import argparse
import collections
import copy
import glob
import hashlib
import io
import os
import shutil
import sys
import tempfile

import pikepdf

try:
    from fontTools.ttLib import TTFont
except ImportError:                                     # pragma: no cover
    TTFont = None

MIN_SHARED = 5
_HINTING = ('fpgm', 'prep', 'cvt ')


# ---------------------------------------------------------------- reading fonts

def _font_dicts(pdf):
    """Every simple embedded TrueType font dictionary reachable from the pages, by objgen."""
    out = {}
    seen = set()

    def visit(res, depth=0):
        if res is None or depth > 8:
            return
        for _, f in ((res.get('/Font') or {}).items()):
            try:
                if (f.get('/Subtype') == '/TrueType' and f.is_indirect
                        and '/FontDescriptor' in f and '/FontFile2' in f.FontDescriptor):
                    out[f.objgen] = f
            except Exception:
                continue
        for _, xo in ((res.get('/XObject') or {}).items()):
            try:
                if xo.get('/Subtype') == '/Form' and xo.objgen not in seen:
                    seen.add(xo.objgen)
                    visit(xo.get('/Resources'), depth + 1)
            except Exception:
                continue

    for page in pdf.pages:
        visit(page.get('/Resources'))
    return out


def _code_cmap(tt):
    """code (0-255) -> glyph name, the way a viewer finds a simple TrueType font's glyph."""
    tables = {(t.platformID, t.platEncID): t for t in tt['cmap'].tables}
    if (1, 0) in tables:
        return {c & 0xFF: g for c, g in tables[(1, 0)].cmap.items() if c < 256}
    if (3, 0) in tables:
        return {c & 0xFF: g for c, g in tables[(3, 0)].cmap.items() if 0xF000 <= c <= 0xF0FF or c < 256}
    if (3, 1) in tables:
        out = {}
        for code in range(32, 256):
            try:
                u = ord(bytes([code]).decode('cp1252'))
            except UnicodeDecodeError:
                continue
            g = tables[(3, 1)].cmap.get(u)
            if g:
                out[code] = g
        return out
    return {}


def _glyph_hash(tt, name, depth=0):
    """What a glyph draws, independent of where it sits in its subset.

    A simple glyph is its compiled outline and instructions plus its metrics. A composite
    is NOT its compiled bytes: those name components by glyph ID, and the same composite has
    different IDs in different subsets. It is hashed from its components' own hashes and
    their placement instead."""
    glyf = tt['glyf']
    g = glyf[name]
    h = hashlib.md5(repr(tt['hmtx'][name]).encode())
    if g.isComposite() and depth < 8:
        for c in g.components:
            h.update(_glyph_hash(tt, c.glyphName, depth + 1).encode())
            h.update(repr((c.x, c.y, c.flags, getattr(c, 'transform', None))).encode())
        if hasattr(g, 'program'):
            h.update(g.program.getBytecode())
    elif g.numberOfContours:
        h.update(g.compile(glyf))
    return h.hexdigest()


class Subset:
    """One embedded subset: its font dictionary, its program, and what each code draws."""

    def __init__(self, fdict):
        self.fdict = fdict
        self.program = bytes(fdict.FontDescriptor.FontFile2.read_bytes())
        self.tt = TTFont(io.BytesIO(self.program))
        order = set(self.tt.getGlyphOrder())
        self.codes = {c: _glyph_hash(self.tt, g) for c, g in _code_cmap(self.tt).items() if g in order}
        fc = int(fdict.get('/FirstChar', 0))
        # Only the codes this subset draws: /Widths also lists codes in FirstChar..LastChar
        # that the subset never kept, and those say nothing about the face.
        self.widths = {fc + k: float(w) for k, w in enumerate(fdict.get('/Widths') or [])
                       if fc + k in self.codes}
        fd = fdict.FontDescriptor
        head = self.tt['head']
        # head.flags bit 1 ("left sidebearing point at x=0") is a claim about the glyphs a
        # subset happens to hold, set by the subsetter: measured on one face, 2073 on some of
        # its subsets and 2075 on others. It is not part of what the face is.
        self.face_key = (head.unitsPerEm, head.flags & ~2, len(self.tt.getGlyphOrder()),
                         tuple(sorted(self.tt.keys())),
                         tuple(hashlib.md5(self.tt.getTableData(t)).hexdigest()
                               if t in self.tt else '' for t in _HINTING),
                         int(fd.get('/Flags', 0)), float(fd.get('/ItalicAngle', 0)))

    def agrees(self, codes, widths):
        shared = set(self.codes) & set(codes)
        return (len(shared) >= MIN_SHARED
                and all(self.codes[c] == codes[c] for c in shared)
                and all(self.widths.get(c) == widths.get(c) for c in shared
                        if c in self.widths and c in widths))


def eligible(fdict):
    enc = fdict.get('/Encoding')
    if isinstance(enc, pikepdf.Dictionary) and '/Differences' in enc:
        return False
    return '/ToUnicode' not in fdict


def detect(pdf):
    """[[Subset]] -- the faces with two or more subsets, largest first."""
    if TTFont is None:
        raise RuntimeError('fontTools is required')
    subsets = []
    for f in _font_dicts(pdf).values():
        if not eligible(f):
            continue
        try:
            subsets.append(Subset(f))
        except Exception:
            continue                                   # an unreadable program is left alone
    faces = []                                         # [(key, codes, widths, [Subset])]
    for s in subsets:
        for face in faces:
            if face[0] == s.face_key and s.agrees(face[1], face[2]):
                face[3].append(s)
                for c, h in s.codes.items():
                    face[1].setdefault(c, h)
                for c, w in s.widths.items():
                    face[2].setdefault(c, w)
                break
        else:
            faces.append((s.face_key, dict(s.codes), dict(s.widths), [s]))
    out = [f[3] for f in faces if len(f[3]) > 1]
    # Clustering is greedy, so re-check every member against the face's final union: a
    # member admitted early must still agree with glyphs other members brought in later.
    checked = []
    for members in out:
        union, widths = {}, {}
        for s in members:
            for c, h in s.codes.items():
                union.setdefault(c, h)
            for c, w in s.widths.items():
                widths.setdefault(c, w)
        if all(all(union[c] == h for c, h in s.codes.items()) for s in members) and \
                all(all(widths[c] == w for c, w in s.widths.items()) for s in members):
            checked.append(members)
    checked.sort(key=len, reverse=True)
    return checked


# ---------------------------------------------------------------- merging

def _used_glyphs(tt):
    """Glyph names a font's cmaps reach, with the components of composites."""
    used = set()
    todo = [g for t in tt['cmap'].tables for g in t.cmap.values()]
    while todo:
        g = todo.pop()
        if g in used or g not in tt['glyf'].glyphs:
            continue
        used.add(g)
        gl = tt['glyf'][g]
        if gl.isComposite():
            todo += [c.glyphName for c in gl.components]
    return used


class MergeConflict(Exception):
    """Two members put different glyphs in one slot, or map one code to different slots."""


def merge_face(members):
    """One TrueType program (bytes) that draws every code any member draws.

    BY GLYPH ID, NOT BY NAME. The subsetter kept glyph IDs: every subset of a face holds all
    of its slots (measured: 3,416 in two faces, 3,417 in the other two) and fills only the
    ones its page draws. But it names only the slots it filled -- the rest carry generated
    placeholder names -- so slot 23 is "four" in one subset and a placeholder in the next;
    278 of 285 subsets of one face had a glyph order of their own. A merge by name appended
    duplicates and pushed IDs past 255, which the Mac (1,0) cmap cannot hold. So each slot
    takes the glyph of whichever member draws it, under that member's name; a slot no member
    draws keeps the first member's placeholder. The IDs are then exactly the face's own, and
    every member's cmap entry points at the same slot it always did.
    """
    members = sorted(members, key=lambda s: len(s.codes), reverse=True)
    base = TTFont(io.BytesIO(members[0].program))
    n = len(base.getGlyphOrder())
    slot = {}                                          # gid -> (name, member font)
    for s in members:
        tt = s.tt
        if len(tt.getGlyphOrder()) != n:
            raise MergeConflict('members differ in glyph count')
        for g in _used_glyphs(tt):
            gid = tt.getGlyphID(g)
            if gid in slot:
                name0, tt0 = slot[gid]
                if _glyph_hash(tt0, name0) != _glyph_hash(tt, g):
                    raise MergeConflict(f'slot {gid}: {name0!r} and {g!r} differ')
            else:
                slot[gid] = (g, tt)
    old = base.getGlyphOrder()
    order = [slot[i][0] if i in slot else old[i] for i in range(n)]
    if len(set(order)) != n:
        raise MergeConflict('a drawn glyph name collides with another slot')
    glyphs, metrics = {}, {}
    for i, name in enumerate(order):
        if i in slot:
            g, tt = slot[i]
            glyphs[name] = copy.deepcopy(tt['glyf'][g])
            metrics[name] = tt['hmtx'][g]
        else:
            glyphs[name] = base['glyf'][old[i]]
            metrics[name] = base['hmtx'][old[i]]
    cmaps = {}
    for s in members:
        for t in s.tt['cmap'].tables:
            m = cmaps.setdefault((t.platformID, t.platEncID), {})
            for c, g in t.cmap.items():
                gid = s.tt.getGlyphID(g)
                if m.setdefault(c, gid) != gid:
                    raise MergeConflict(f'code {c} maps to slots {m[c]} and {gid}')
    base.setGlyphOrder(order)
    base['glyf'].glyphs = glyphs
    base['glyf'].glyphOrder = order
    base['hmtx'].metrics = metrics
    for t in base['cmap'].tables:
        # Decode first. fontTools reads a subtable lazily and, never decoded, writes its
        # ORIGINAL bytes back on save whatever `.cmap` was set to -- measured: the merged
        # font kept the first member's 66 codes out of the face's 76, and every quote mark
        # drawn through another member vanished from the page.
        t.ensureDecompiled()
        t.cmap = {c: order[gid] for c, gid in cmaps.get((t.platformID, t.platEncID), {}).items()}
    for s in members:                  # claim "lsb at x=0" only if every member's glyphs do
        base['head'].flags &= s.tt['head'].flags | ~2
    # The limits a hinting interpreter sizes itself by must cover every glyph now in the
    # font, whichever subset it came from.
    for field in ('maxZones', 'maxTwilightPoints', 'maxStorage', 'maxFunctionDefs',
                  'maxInstructionDefs', 'maxStackElements', 'maxSizeOfInstructions',
                  'maxComponentElements', 'maxComponentDepth'):
        vals = [getattr(s.tt['maxp'], field, None) for s in members]
        vals = [v for v in vals if v is not None]
        if vals and hasattr(base['maxp'], field):
            setattr(base['maxp'], field, max(vals + [getattr(base['maxp'], field)]))
    if 'post' in base and base['post'].formatType == 2.0:
        base['post'].extraNames = []
        base['post'].mapping = {}
    out = io.BytesIO()
    base.save(out)
    return out.getvalue()


def merge(pdf, faces, skipped=None):
    """Point every member at one merged program per face. Returns (fonts, programs dropped).
    A face whose members conflict is left as it was, and its reason appended to `skipped`."""
    fonts = dropped = 0
    for members in faces:
        try:
            program = merge_face(members)
        except MergeConflict as exc:
            if skipped is not None:
                skipped.append(f'{len(members)} subsets: {exc}')
            continue
        base = max(members, key=lambda s: len(s.codes))
        fd = pdf.make_indirect(pikepdf.Dictionary(
            {k: v for k, v in base.fdict.FontDescriptor.items() if k != '/FontFile2'}))
        stream = pikepdf.Stream(pdf, program)
        stream['/Length1'] = len(program)
        fd['/FontFile2'] = pdf.make_indirect(stream)
        tt = TTFont(io.BytesIO(program))
        head, upem = tt['head'], tt['head'].unitsPerEm
        fd['/FontBBox'] = pikepdf.Array([round(v * 1000 / upem) for v in
                                         (head.xMin, head.yMin, head.xMax, head.yMax)])
        for s in members:
            s.fdict['/FontDescriptor'] = fd
            s.fdict['/BaseFont'] = fd['/FontName']
            fonts += 1
        dropped += len(members) - 1
    return fonts, dropped


# ---------------------------------------------------------------- audit

def _gs():
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
    import subprocess
    gs = _gs()
    if not gs:
        return False
    subprocess.run([gs, '-sDEVICE=png16m', f'-r{dpi}', f'-dFirstPage={page_no}',
                    f'-dLastPage={page_no}', '-dNOPAUSE', '-dBATCH', '-dQUIET',
                    '-sOutputFile=' + str(out_png), str(path)], capture_output=True, timeout=300)
    return os.path.exists(out_png)


def _spread(n, want):
    if want <= 0 or n <= 0:
        return []
    if n <= want:
        return list(range(n))
    step = (n - 1) / (want - 1)
    return sorted({round(i * step) for i in range(want)})


def audit(src_path, out_path, render_sample=24, text_pages=None):
    """Fatal reasons the rewrite differs from the source, or []."""
    from pypdf import PdfReader
    from PIL import Image, ImageChops

    a, b = PdfReader(str(src_path)), PdfReader(str(out_path))
    if len(a.pages) != len(b.pages):
        return [f'page count {len(a.pages)} -> {len(b.pages)}']
    try:
        if len(a.outline) != len(b.outline):
            return ['bookmarks changed']
    except Exception:
        pass
    for i in (range(len(a.pages)) if text_pages is None else text_pages):
        pa, pb = a.pages[i], b.pages[i]
        if len(pa.get('/Annots') or []) != len(pb.get('/Annots') or []):
            return [f'page {i + 1}: annotations changed']
        if (pa.extract_text() or '') != (pb.extract_text() or ''):
            return [f'page {i + 1}: extracted text changed']
    tmp = tempfile.mkdtemp(prefix='ff_')
    try:
        for i in _spread(len(a.pages), render_sample):
            pa, pb = os.path.join(tmp, 'a.png'), os.path.join(tmp, 'b.png')
            if not (_render(src_path, i + 1, pa) and _render(out_path, i + 1, pb)):
                return []                              # no Ghostscript: the text check stands
            ia, ib = Image.open(pa).convert('RGB'), Image.open(pb).convert('RGB')
            same = ia.size == ib.size and ImageChops.difference(ia, ib).getbbox() is None
            ia.close()
            ib.close()
            os.remove(pa)
            os.remove(pb)
            if not same:
                return [f'page {i + 1}: renders differently']
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return []


# ---------------------------------------------------------------- driver

def clean_file(src, out, render_sample=24, text_sample=0, apply_=True):
    """Merge `src`'s subset faces into `out`. Returns {'faces', 'fonts', 'dropped', 'bytes',
    'err'}; writes nothing when there is nothing to merge or the audit fails."""
    with pikepdf.open(str(src)) as pdf:
        faces = detect(pdf)
        res = {'faces': [len(f) for f in faces], 'fonts': 0, 'dropped': 0,
               'bytes': (os.path.getsize(src), None), 'skipped': [], 'err': None}
        if not faces or not apply_:
            return res
        res['fonts'], res['dropped'] = merge(pdf, faces, res['skipped'])
        if not res['fonts']:
            res['err'] = 'no face could be merged: ' + '; '.join(res['skipped'][:3])
            return res
        tmp = str(out) + '.ff.tmp'
        pdf.save(tmp)
        n = len(pdf.pages)
    size = os.path.getsize(tmp)
    res['bytes'] = (os.path.getsize(src), size)
    bad = [] if size < os.path.getsize(src) else ['not smaller']
    if not bad:
        text_pages = None if not text_sample else _spread(n, text_sample)
        bad = audit(src, tmp, render_sample, text_pages)
    if bad:
        os.remove(tmp)
        res.update(fonts=0, dropped=0, err='; '.join(bad[:3]))
        return res
    os.replace(tmp, str(out))
    return res


def note_for(res):
    """The report note for a clean_file result, '' when there was nothing to merge."""
    if not res or not res.get('faces'):
        return ''
    subsets = sum(res['faces'])
    if res.get('err'):
        return f' (font subsets: {subsets} in {len(res["faces"])} face(s) left as they were: {res["err"]})'
    a, b = res['bytes']
    return (f' (font subsets merged: {res["fonts"]} fonts into {len(res["faces"])} face(s), '
            f'{a / 1048576:.1f} -> {b / 1048576:.1f} MB)')


def _pdfs_under(path):
    if os.path.isfile(path):
        return [path]
    return sorted(os.path.join(r, f) for r, _, fs in os.walk(path)
                  for f in fs if f.lower().endswith('.pdf'))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[3].strip(),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('path')
    ap.add_argument('--apply', action='store_true', help='rewrite (in place unless --dest)')
    ap.add_argument('--dest', help='write results into this directory instead')
    ap.add_argument('--render-sample', type=int, default=24,
                    help='pages rendered and compared pixel for pixel (default 24)')
    ap.add_argument('--text-sample', type=int, default=0,
                    help='pages whose text is compared (default 0 = every page)')
    args = ap.parse_args(argv)
    if TTFont is None:
        print('fontTools is required', file=sys.stderr)
        return 2
    if args.dest:
        os.makedirs(args.dest, exist_ok=True)
    for path in _pdfs_under(args.path):
        out = os.path.join(args.dest, os.path.basename(path)) if args.dest else path
        try:
            res = clean_file(path, out, args.render_sample, args.text_sample, args.apply)
        except Exception as exc:
            print(f'{os.path.basename(path)}: ERROR {exc.__class__.__name__}: {exc}')
            continue
        name = os.path.basename(path)
        if not res['faces']:
            print(f'{name}: no face embedded more than once')
        elif res['err']:
            print(f'{name}: {len(res["faces"])} faces NOT merged -- {res["err"]}')
        elif not args.apply:
            print(f'{name}: would merge {sum(res["faces"])} subsets into {len(res["faces"])} faces '
                  f'{res["faces"][:8]}')
        else:
            a, b = res['bytes']
            print(f'{name}: merged {res["fonts"]} fonts into {len(res["faces"])} faces, '
                  f'{res["dropped"]} programs dropped, {a / 1e6:.1f} -> {b / 1e6:.1f} MB')
    return 0


if __name__ == '__main__':
    sys.exit(main())
