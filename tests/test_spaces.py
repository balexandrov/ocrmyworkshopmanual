"""Box-shaped spaces: a converter subset its TrueType fonts at /FirstChar 37, so code 32 maps
to .notdef and a strict viewer draws a box at every word gap. See pdfspaces.py.

The fixture is the real shape, built from scratch: a TrueType font whose glyph 32 exists and is
empty, whose (1,0) format-0 cmap maps code 32 to glyph 0, embedded as /FontFile2 in a simple
/TrueType font with /FirstChar 37, /Flags Nonsymbolic and /Encoding /WinAnsiEncoding -- and a
page that draws "(AB CD)Tj", the literal-space form the converter uses.
"""
import io
import struct

import pikepdf
import pytest

import _util as U
import pdfspaces as S

owm = U.owm
_missing = U.tools_missing()


def _font_bytes(space_mapped=False):
    """A tiny TrueType: .notdef, then glyphs 1..90 where 32 is the empty 'space' and 65+ are
    boxes, with a (1,0) format-0 cmap that maps 37..90 and leaves 32 at glyph 0 unless asked."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

    order = [".notdef"] + [f"g{i}" for i in range(1, 91)]
    order[32] = "space"
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({i: order[i] for i in range(37, 91)})
    glyphs = {}
    for name in order:
        pen = TTGlyphPen(None)
        if name not in (".notdef", "space"):
            pen.moveTo((50, 0)); pen.lineTo((50, 700)); pen.lineTo((450, 700)); pen.lineTo((450, 0))
            pen.closePath()
        glyphs[name] = pen.glyph()
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({n: (500, 50) for n in order})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "Boxy", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    mac = CmapSubtable.newSubtable(0)
    mac.platformID, mac.platEncID, mac.language = 1, 0, 0
    mac.cmap = {i: order[i] for i in range(37, 91)}
    if space_mapped:
        mac.cmap[32] = "space"
    fb.font["cmap"].tables.append(mac)
    buf = io.BytesIO()
    fb.font.save(buf)
    return buf.getvalue()


def make_boxed_pdf(path, space_mapped=False, first_char=37, in_form=False):
    pdf = pikepdf.new()
    ff = pdf.make_stream(_font_bytes(space_mapped))
    fd = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.FontDescriptor, FontName=pikepdf.Name("/ABCDEF+Boxy"), Flags=32,
        FontBBox=[0, -200, 1000, 800], ItalicAngle=0, Ascent=800, Descent=-200, CapHeight=700,
        StemV=80, FontFile2=ff))
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.TrueType, BaseFont=pikepdf.Name("/ABCDEF+Boxy"),
        FirstChar=first_char, LastChar=90, Widths=[500] * (91 - first_char),
        Encoding=pikepdf.Name.WinAnsiEncoding, FontDescriptor=fd))
    ops = b"BT /F1 24 Tf 72 700 Td 4 Tw (AB CD)Tj ET"
    page = pdf.add_blank_page(page_size=(612, 792))
    if in_form:
        form = pdf.make_stream(ops, Type=pikepdf.Name.XObject, Subtype=pikepdf.Name.Form,
                               BBox=[0, 0, 612, 792],
                               Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)))
        page.obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fm1=form))
        page.obj.Contents = pdf.make_stream(b"q /Fm1 Do Q")
    else:
        page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        page.obj.Contents = pdf.make_stream(ops)
    pdf.save(str(path))
    return path


def _flat_space_entry(pdf_path):
    with pikepdf.open(str(pdf_path)) as pdf:
        font = S.fonts_of(pdf)[0]
        data = font.FontDescriptor.FontFile2.read_bytes()
        tabs = S._tables(data)
        return data[S._flat_cmap_offset(data, tabs) + 32], int(font.FontDescriptor.Flags), \
            "/Encoding" in font


def test_the_fixture_has_the_fault(tmp_path):
    src = make_boxed_pdf(tmp_path / "boxed.pdf")
    with pikepdf.open(str(src)) as pdf:
        assert len(S.detect(pdf)) == 1
    assert _flat_space_entry(src) == (0, 32, True)


def test_the_three_edits_and_nothing_else(tmp_path):
    src = make_boxed_pdf(tmp_path / "boxed.pdf")
    out = tmp_path / "out.pdf"
    res = S.clean_file(src, out)
    assert res["found"] == 1 and not res["err"], res
    entry, flags, has_enc = _flat_space_entry(out)
    assert entry == 32                       # code 32 -> the font's own empty glyph 32
    assert flags & S.SYMBOLIC and not flags & S.NONSYMBOLIC
    assert not has_enc
    with pikepdf.open(str(src)) as a, pikepdf.open(str(out)) as b:
        assert S._fingerprint(a) == S._fingerprint(b)       # page content untouched
        fa = a.pages[0].Resources.Font.F1.FontDescriptor.FontFile2.read_bytes()
        fb = b.pages[0].Resources.Font.F1.FontDescriptor.FontFile2.read_bytes()
        assert len(fa) == len(fb) and sum(x != y for x, y in zip(fa, fb)) == 1  # one byte


def test_a_font_inside_a_form_xobject_is_found(tmp_path):
    src = make_boxed_pdf(tmp_path / "form.pdf", in_form=True)
    res = S.clean_file(src, tmp_path / "out.pdf")
    assert res["found"] == 1 and not res["err"], res


@pytest.mark.parametrize("kw", [dict(space_mapped=True), dict(first_char=32)])
def test_a_healthy_font_is_left_alone(tmp_path, kw):
    src = make_boxed_pdf(tmp_path / "ok.pdf", **kw)
    out = tmp_path / "out.pdf"
    res = S.clean_file(src, out)
    assert res["found"] == 0 and not out.exists()


def test_rerunning_is_a_no_op(tmp_path):
    src = make_boxed_pdf(tmp_path / "boxed.pdf")
    once = tmp_path / "once.pdf"
    assert S.clean_file(src, once)["found"] == 1
    assert S.clean_file(once, tmp_path / "twice.pdf")["found"] == 0


@pytest.mark.skipif(_missing, reason=_missing or "")
def test_the_pipeline_fixes_a_born_digital_file_it_would_otherwise_copy(tmp_path):
    """The born-digital lane copies bytes; without the source step the boxes shipped as-is."""
    src = make_boxed_pdf(tmp_path / "boxed.pdf")
    dest = tmp_path / "dest.pdf"
    res = owm.compress_one(str(src), str(dest), 200, ocr=False, lossless=False)
    assert not res.get("err"), res
    assert "box spaces fixed" in res.get("note", ""), res.get("note")
    with pikepdf.open(str(dest)) as pdf:
        assert not S.detect(pdf)


@pytest.mark.skipif(_missing, reason=_missing or "")
def test_no_fix_spaces_leaves_them(tmp_path):
    src = make_boxed_pdf(tmp_path / "boxed.pdf")
    dest = tmp_path / "dest.pdf"
    res = owm.compress_one(str(src), str(dest), 200, ocr=False, lossless=False, fix_spaces=False)
    assert not res.get("err"), res
    with pikepdf.open(str(dest)) as pdf:
        assert len(S.detect(pdf)) == 1
