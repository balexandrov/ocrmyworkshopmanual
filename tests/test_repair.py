"""pdfrepair: a page whose content uses a resource its /Resources lack gets it back from a neighbour,
but only when that measurably helps; a page whose content is lost is reported, never invented.

The shape of the 2022-2024 Honda Civic FE/FL download: page 1501 drew with /F4, its /Font held only
/F5, and it read as "+RZ WR XVH WKLV PDQXDO" -- the pages around it, from the same print run, define
/F4.
"""
import pikepdf
from pypdf import PdfReader

import pdfrepair as R
import _util as U


def _book(path, drop_on=1, lose_content_on=None):
    pdf = pikepdf.Pdf.new()
    for k in range(3):
        page = pdf.add_blank_page(page_size=(612, 792))
        res = U._sub_dict(page.obj, '/Resources')
        if k != drop_on:
            U._add_font(pdf, res, '/F4')
        page.contents_add(pikepdf.Stream(pdf, f'BT /F4 18 Tf 60 600 Td (How to use this manual {k}) Tj ET'
                                         .encode('latin1')), prepend=False)
    if lose_content_on is not None:
        pg = pdf.pages[lose_content_on].obj
        pg.Contents = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.ExtGState, ca=1))
    pdf.save(str(path))
    return path


def test_a_missing_font_is_taken_from_a_neighbour_and_the_text_comes_back(tmp_path):
    src = _book(tmp_path / 'b.pdf')
    with pikepdf.open(str(src)) as p:
        assert R.missing(p.pages[1]) == {'/Font': {'F4'}}
    out = tmp_path / 'out'
    r = R.repair(str(src), str(out), apply=True)
    assert r['fixed'] == 1 and r['unfixed'] == 0, r
    with pikepdf.open(str(out / 'b.pdf')) as p:
        assert R.missing(p.pages[1]) == {}
        assert len(p.pages) == 3
    assert 'How to use this manual 1' in PdfReader(str(out / 'b.pdf')).pages[1].extract_text()


def test_a_page_whose_content_is_lost_is_reported_and_made_valid(tmp_path):
    """Its /Contents points at a graphics state. The content is not in the file; an empty stream
    shows what every viewer already shows (nothing) and lets other passes read the file."""
    src = _book(tmp_path / 'b.pdf', drop_on=None, lose_content_on=2)
    r = R.repair(str(src), str(tmp_path / 'out'), apply=True)
    assert r['lost_content'] == [2]
    assert r['fixed'] == 0
    with pikepdf.open(str(tmp_path / 'out' / 'b.pdf')) as p:
        assert len(p.pages) == 3
        assert isinstance(p.pages[2].obj.Contents, pikepdf.Stream)
        assert p.pages[2].obj.Contents.read_bytes() == b''
    assert 'manual 1' in PdfReader(str(tmp_path / 'out' / 'b.pdf')).pages[1].extract_text()


def test_an_image_filed_as_an_unused_font_is_removed_so_the_text_can_be_read(tmp_path):
    """The Civic's page 1501 listed an image as font /F6, which its content never uses. pypdf gives
    up on the page's text at it -- so no repair could even be measured -- and nothing draws with it."""
    src = _book(tmp_path / 'b.pdf')
    with pikepdf.open(str(src), allow_overwriting_input=True) as p:
        img = pikepdf.Stream(p, b'\x00' * 3)
        img.Type, img.Subtype, img.Width, img.Height = pikepdf.Name.XObject, pikepdf.Name.Image, 1, 1
        img.ColorSpace, img.BitsPerComponent = pikepdf.Name.DeviceRGB, 8
        U._sub_dict(p.pages[1].obj.Resources, '/Font')['/F6'] = p.make_indirect(img)
        p.save()
    out = tmp_path / 'out'
    r = R.repair(str(src), str(out), apply=True)
    assert r['misfiled'] == 1 and r['fixed'] == 1, r
    with pikepdf.open(str(out / 'b.pdf')) as p:
        assert '/F6' not in p.pages[1].obj.Resources.Font
    assert 'How to use this manual 1' in PdfReader(str(out / 'b.pdf')).pages[1].extract_text()


def test_a_font_that_is_a_page_is_replaced_and_no_longer_drags_the_book_along(tmp_path):
    """The Civic FE's fonts /F1 and graphics states /G3 that ARE page objects: a page carries /Parent,
    so copying the page that uses such a "font" copied the whole page tree -- three copies of an
    11,008-page book in a 744-page chapter. The entry is removed, the real font borrowed."""
    src = _book(tmp_path / 'b.pdf', drop_on=None)
    with pikepdf.open(str(src), allow_overwriting_input=True) as p:
        p.pages[1].obj.Resources.Font['/F4'] = p.pages[2].obj     # the scramble: a font that is a page
        p.save()
    out = tmp_path / 'out'
    r = R.repair(str(src), str(out), apply=True)
    assert r['misfiled'] == 1 and r['fixed'] == 1, r
    with pikepdf.open(str(out / 'b.pdf')) as p:
        f4 = p.pages[1].obj.Resources.Font.F4
        assert f4.get('/Type') == '/Font', 'the real font, borrowed'
        one = pikepdf.new()
        one.pages.append(p.pages[1])
        one.save(str(tmp_path / 'one.pdf'))
    with pikepdf.open(str(tmp_path / 'one.pdf')) as one:
        pages = [o for o in one.objects if isinstance(o, pikepdf.Dictionary) and o.get('/Type') == '/Page']
        assert len(pages) == 1, 'copying the page no longer copies its neighbours'
    assert 'How to use this manual 1' in PdfReader(str(out / 'b.pdf')).pages[1].extract_text()


def test_a_lost_page_s_font_that_is_a_page_is_removed_too(tmp_path):
    """The Civic FE's fonts-that-are-pages sat mostly on the pages whose content is lost; skipping
    those pages left 9 such entries, each still dragging the whole book into any chapter copy."""
    src = _book(tmp_path / 'b.pdf', drop_on=None, lose_content_on=1)
    with pikepdf.open(str(src), allow_overwriting_input=True) as p:
        p.pages[1].obj.Resources.Font['/F1'] = p.pages[2].obj
        p.save()
    r = R.repair(str(src), str(tmp_path / 'out'), apply=True)
    assert r['lost_content'] == [1] and r['misfiled'] == 1, r
    with pikepdf.open(str(tmp_path / 'out' / 'b.pdf')) as p:
        assert '/F1' not in p.pages[1].obj.Resources.Font


def test_a_neighbour_s_scrambled_entry_is_never_borrowed(tmp_path):
    """Page 2 and page 3 both list a page as their /F4; only page 1 holds the real font. The nearer
    neighbour's "font" must not be taken just because it is nearer."""
    src = _book(tmp_path / 'b.pdf', drop_on=None)
    with pikepdf.open(str(src), allow_overwriting_input=True) as p:
        p.pages[1].obj.Resources.Font['/F4'] = p.pages[0].obj
        p.pages[2].obj.Resources.Font['/F4'] = p.pages[0].obj
        p.save()
    r = R.repair(str(src), str(tmp_path / 'out'), apply=True)
    with pikepdf.open(str(tmp_path / 'out' / 'b.pdf')) as p:
        for i in (1, 2):
            assert p.pages[i].obj.Resources.Font.F4.get('/Type') == '/Font', (i, r)


def test_a_name_no_neighbour_defines_stays_missing(tmp_path):
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.contents_add(pikepdf.Stream(pdf, b'BT /F9 18 Tf 60 600 Td (alone) Tj ET'), prepend=False)
    src = tmp_path / 'one.pdf'
    pdf.save(str(src))
    r = R.repair(str(src), str(tmp_path / 'out'), apply=True)
    assert r['fixed'] == 0 and r['unfixed'] == 1
