"""Per-page subsets of one TrueType face, merged back into one font.

THE SHAPE, measured on a 1,449-page chapter of a repair-information printout: 733 simple
TrueType fonts, four faces, one fresh subset of each face per page. Every subset keeps the
face's glyph IDs -- all 3,416 slots, only the drawn ones filled -- but NAMES only the slots it
filled, so the same slot has a different name in almost every subset. Each carries the
face's full hinting programs; 18.3 MB of the 29.3 MB file was font programs.

These fixtures build a small face with fontTools and cut it the same way.
"""
import copy
import io

import pikepdf
import pytest

fontTools = pytest.importorskip('fontTools')
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._c_m_a_p import cmap_format_0

import pdffonts as F

LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ"'
NAMES = ['.notdef', 'space'] + [('quotedbl' if c == '"' else c) for c in LETTERS] + \
        [f'extra{i}' for i in range(30)]           # slots no page draws


def _shape(pen, k, wide):
    """A distinct outline per glyph: a box whose notch depends on k."""
    pen.moveTo((50, 0))
    pen.lineTo((50, 700))
    pen.lineTo((wide - 50, 700))
    pen.lineTo((wide - 50, 40 + 20 * (k % 25)))
    pen.lineTo((wide // 2, 40 + 20 * (k % 25)))
    pen.lineTo((wide // 2, 0))
    pen.closePath()


def build_face(variant=0, fpgm=b'\xb0\x00'):
    """A TrueType face; `variant` changes the outlines (another face), `fpgm` its hinting."""
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(NAMES)
    cmap = {32: 'space'}
    for c in LETTERS:
        cmap[ord(c)] = 'quotedbl' if c == '"' else c
    fb.setupCharacterMap(cmap)
    glyphs, metrics = {}, {}
    for k, name in enumerate(NAMES):
        pen = TTGlyphPen(None)
        if name not in ('.notdef', 'space'):
            _shape(pen, k + variant * 7, 600)
        glyphs[name] = pen.glyph()
        metrics[name] = (600, 50 if name not in ('.notdef', 'space') else 0)
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({'familyName': f'Scr{variant}', 'styleName': 'Regular'})
    fb.setupOS2()
    fb.setupPost()
    font = fb.font
    font['fpgm'] = newTable('fpgm')
    from fontTools.ttLib.tables import ttProgram
    font['fpgm'].program = ttProgram.Program()
    font['fpgm'].program.fromBytecode(fpgm)
    # the Mac (1,0) format-0 table a simple TrueType font is read through
    t = cmap_format_0(0)
    t.platformID, t.platEncID, t.language = 1, 0, 0
    t.cmap = dict(cmap)
    font['cmap'].tables.append(t)
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


def cut_subset(face_bytes, keep, tag):
    """Keep IDs, empty every glyph not in `keep`, and rename the emptied slots per subset."""
    tt = TTFont(io.BytesIO(face_bytes))
    order = tt.getGlyphOrder()
    keep_names = {'.notdef'} | {('quotedbl' if c == '"' else c) if c != ' ' else 'space' for c in keep}
    new_order = [n if n in keep_names else f'g{tag}_{i}' for i, n in enumerate(order)]
    glyf, hmtx = tt['glyf'], tt['hmtx']
    glyphs = {}
    metrics = {}
    for old, new in zip(order, new_order):
        if old in keep_names:
            glyphs[new] = glyf[old]
            metrics[new] = hmtx[old]
        else:
            glyphs[new] = TTGlyphPen(None).glyph()
            metrics[new] = (0, 0)
    for t in tt['cmap'].tables:
        t.ensureDecompiled()
        t.cmap = {c: g for c, g in t.cmap.items() if g in keep_names}
    tt.setGlyphOrder(new_order)
    glyf.glyphs, glyf.glyphOrder = glyphs, new_order
    hmtx.metrics = metrics
    tt['post'].formatType = 3.0
    out = io.BytesIO()
    tt.save(out)
    return out.getvalue()


def make_subset_pdf(path, pages, face=None, faces=None):
    """One page per text, each page with its own subset of the face that draws exactly it."""
    pdf = pikepdf.Pdf.new()
    for k, text in enumerate(pages):
        fb = (faces[k] if faces else face)
        # Every subset keeps the space and starts at code 32, as the measured printout's do: a
        # subset above 32 with the space unmapped is the box-space fault (pdfspaces.py), which
        # the main pass fixes FIRST -- and a font it has made symbolic is rightly no longer
        # the same face as one it left alone.
        prog = cut_subset(fb, set(text) | {' '}, k)
        page = pdf.add_blank_page(page_size=(612, 792))
        stream = pikepdf.Stream(pdf, prog)
        stream['/Length1'] = len(prog)
        fd = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.FontDescriptor, FontName=pikepdf.Name(f'/AAAA{k:02d}+Scr{k}'),
            Flags=32, FontBBox=[0, 0, 1000, 800], ItalicAngle=0, Ascent=800, Descent=-200,
            CapHeight=700, StemV=80, FontFile2=pdf.make_indirect(stream)))
        codes = sorted({ord(c) for c in text} | {32})
        fc, lc = codes[0], codes[-1]
        widths = [0 if c == 32 else 600 if chr(c) in text else 0 for c in range(fc, lc + 1)]
        font = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.TrueType,
            BaseFont=pikepdf.Name(f'/AAAA{k:02d}+Scr{k}'), FirstChar=fc, LastChar=lc,
            Widths=widths, Encoding=pikepdf.Name.WinAnsiEncoding, FontDescriptor=fd))
        page.obj['/Resources'] = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        esc = text.replace('\\', r'\\').replace('(', r'\(').replace(')', r'\)')
        page.contents_add(pikepdf.Stream(pdf, f'BT /F1 36 Tf 60 600 Td ({esc}) Tj ET'.encode('latin1')))
    pdf.save(str(path))
    pdf.close()
    return path


TEXTS = ['ABCDEFGHIJ', 'FGHIJKLMNO', 'KLMNOPQRST', 'PQRSTUVWXY', 'SWITCH "A" CIRCUIT']


def _programs(path):
    with pikepdf.open(str(path)) as pdf:
        return {f.FontDescriptor.FontFile2.objgen for f in F._font_dicts(pdf).values()}


def _texts(path):
    from pypdf import PdfReader
    return [p.extract_text() for p in PdfReader(str(path)).pages]


def test_subsets_of_one_face_become_one_font_and_nothing_else_changes(tmp_path):
    face = build_face()
    src = make_subset_pdf(tmp_path / 'p.pdf', TEXTS, face=face)
    assert len(_programs(src)) == len(TEXTS)
    out = tmp_path / 'o.pdf'
    res = F.clean_file(src, out, render_sample=len(TEXTS))
    assert res['err'] is None, res
    assert res['faces'] == [len(TEXTS)]
    assert len(_programs(out)) == 1, 'one program for the whole face'
    assert _texts(out) == _texts(src)
    assert out.stat().st_size < src.stat().st_size


def test_a_code_only_a_smaller_subset_draws_survives_the_merge(tmp_path):
    """The regression that measured this: the quote mark is drawn by one subset only, and a
    merged cmap that was never decoded kept the base member's codes and dropped it -- every
    "A" on that page rendered as A. The render audit caught it; this pins it directly."""
    face = build_face()
    src = make_subset_pdf(tmp_path / 'p.pdf', TEXTS, face=face)
    with pikepdf.open(str(src)) as pdf:
        faces = F.detect(pdf)
        prog = F.merge_face(faces[0])
    tt = TTFont(io.BytesIO(prog))
    for t in tt['cmap'].tables:
        assert 34 in t.cmap, f'({t.platformID},{t.platEncID}) lost the quote mark'
        assert all(ord(c) in t.cmap for c in ''.join(TEXTS) if c != ' ')


def test_glyph_ids_are_the_faces_own(tmp_path):
    """Merging by name appended copies and pushed IDs past 255, which format 0 cannot hold."""
    face = build_face()
    src = make_subset_pdf(tmp_path / 'p.pdf', TEXTS, face=face)
    with pikepdf.open(str(src)) as pdf:
        prog = F.merge_face(F.detect(pdf)[0])
    merged, whole = TTFont(io.BytesIO(prog)), TTFont(io.BytesIO(face))
    assert len(merged.getGlyphOrder()) == len(whole.getGlyphOrder())
    mac = {(t.platformID, t.platEncID): t for t in merged['cmap'].tables}[(1, 0)]
    wmac = {(t.platformID, t.platEncID): t for t in whole['cmap'].tables}[(1, 0)]
    for c, g in mac.cmap.items():
        assert merged.getGlyphID(g) == whole.getGlyphID(wmac.cmap[c]), chr(c)


def test_two_faces_are_not_merged_into_one(tmp_path):
    """Same glyph count, same hinting, different outlines: two faces, never one."""
    a, b = build_face(0), build_face(1)
    src = make_subset_pdf(tmp_path / 'p.pdf', [TEXTS[0], TEXTS[0], TEXTS[1], TEXTS[1]], faces=[a, b, a, b])
    with pikepdf.open(str(src)) as pdf:
        faces = F.detect(pdf)
    assert sorted(len(f) for f in faces) == [2, 2]


def test_different_hinting_programs_are_different_faces(tmp_path):
    """A glyph's instructions call into fpgm; moving it next to another fpgm is not safe."""
    a, b = build_face(0), build_face(0, fpgm=b'\xb0\x01')
    src = make_subset_pdf(tmp_path / 'p.pdf', [TEXTS[0], TEXTS[0], TEXTS[1], TEXTS[1]], faces=[a, b, a, b])
    with pikepdf.open(str(src)) as pdf:
        faces = F.detect(pdf)
    assert sorted(len(f) for f in faces) == [2, 2]


def test_the_render_audit_refuses_a_merge_that_draws_differently(tmp_path, monkeypatch):
    if not F._gs():
        pytest.skip('Ghostscript not installed')
    face = build_face()
    src = make_subset_pdf(tmp_path / 'p.pdf', TEXTS, face=face)
    real = F.merge_face

    def wrong(members):                       # the face's glyphs, but one letter redrawn
        prog = TTFont(io.BytesIO(real(members)))
        other = TTFont(io.BytesIO(build_face(3)))
        name = prog['cmap'].getcmap(3, 1).cmap[ord('A')]
        prog['glyf'].glyphs[name] = copy.deepcopy(other['glyf']['A'])
        out = io.BytesIO()
        prog.save(out)
        return out.getvalue()

    monkeypatch.setattr(F, 'merge_face', wrong)
    out = tmp_path / 'o.pdf'
    res = F.clean_file(src, out, render_sample=len(TEXTS))
    assert res['err'] and 'renders differently' in res['err'], res
    assert not out.exists()


import _util as U

_missing = U.tools_missing()


@pytest.mark.skipif(_missing, reason=_missing or '')
def test_the_pipeline_merges_a_born_digital_file_it_would_otherwise_copy(tmp_path):
    """The born-digital lane copies bytes: without the source step the subsets shipped as-is."""
    src = make_subset_pdf(tmp_path / 'p.pdf', TEXTS, face=build_face())
    dest = tmp_path / 'dest.pdf'
    res = U.owm.compress_one(str(src), str(dest), 200, ocr=False, lossless=False)
    assert not res.get('err'), res
    assert 'font subsets merged' in res.get('note', ''), res.get('note')
    assert len(_programs(dest)) == 1
    assert _texts(dest) == _texts(src)


@pytest.mark.skipif(_missing, reason=_missing or '')
def test_no_merge_fonts_leaves_them(tmp_path):
    src = make_subset_pdf(tmp_path / 'p.pdf', TEXTS, face=build_face())
    dest = tmp_path / 'dest.pdf'
    res = U.owm.compress_one(str(src), str(dest), 200, ocr=False, lossless=False, merge_fonts=False)
    assert not res.get('err'), res
    assert len(_programs(dest)) == len(TEXTS)


def test_report_only_does_not_touch_the_file(tmp_path, capsys):
    src = make_subset_pdf(tmp_path / 'p.pdf', TEXTS, face=build_face())
    before = src.read_bytes()
    assert F.main([str(src)]) == 0
    assert f'would merge {len(TEXTS)} subsets into 1 faces' in capsys.readouterr().out
    assert src.read_bytes() == before
