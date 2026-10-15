"""A stamp written as ONE LINE IN PIECES over a scan: a link with the site's own words either
side of it.

THE BUG. Three ManualsLib downloads of the Subaru 360 manuals (16, 246 and 198 pages) carry
on every page, at the foot, one visible line drawn as separate show-text operators, over a
scanned page whose own words are an invisible OCR layer:

    `Downloaded from ` | `www.Manualslib.com` | ` ` | `manuals search engine`

The file was reported clean. Two rules each stopped it:

  * the crowding guard (`_uncrowded`) counted the stamp's OWN words as other text on the
    link's line -- 36 characters against a budget of 16 -- and read the link as a URL inside
    a sentence. Raising the budget was measured unsafe: publishers' own address lines sit
    all through 16-60. `_sole_line` admits it instead: on a scan the stamp is the only
    visible text on the page, which a publisher's line never is
  * the companion rule (`_beside`) measured origin to origin, and the words' origins sit
    0.098 and 0.122 of the page from the link's, although they touch it end to start; so
    even with the link found, removal would leave `Downloaded from manuals search engine`
"""
import pikepdf

import pdfwatermark as W
import _util as U

PT = 8                                             # font size; the fixture font is 1 em a glyph
STAMP = ['Downloaded from ', 'www.example-stamp.com', ' ', 'manuals search engine']


def _one_line_stamp(path, npages=4, also=(), body_shown=False, scan=True):
    """The ManualsLib shape: the pieces abut on one baseline at the foot of every page, over
    a scan image (unless not `scan`) whose words are an invisible OCR layer (`3 Tr`) unless
    `body_shown`.

    `also` is [(x, text)] put on the same baseline, every page, as the publisher's own.
    """
    mode = 0 if body_shown else 3
    pdf = pikepdf.Pdf.new()
    for k in range(npages):
        page = pdf.add_blank_page(page_size=(612, 792))
        res = U._sub_dict(page.obj, '/Resources')
        U._add_font(pdf, res, '/NxF0')
        ops = ''
        if scan:
            U._sub_dict(res, '/XObject')['/Im0'] = pikepdf.Stream(
                pdf, bytes([200]), Type=pikepdf.Name('/XObject'),
                Subtype=pikepdf.Name('/Image'), Width=1, Height=1,
                ColorSpace=pikepdf.Name('/DeviceGray'), BitsPerComponent=8)
            ops += ' q 612 0 0 792 0 0 cm /Im0 Do Q'
        ops += (f' q BT {mode} Tr /NxF0 11 Tf 1 0 0 1 200 400 Tm (Step {k + 1}: torque to 40 Nm.) Tj'
               f' 1 0 0 1 200 380 Tm (Check the seal for wear.) Tj ET Q')
        x = 3
        for piece in STAMP:
            ops += f' q BT /NxF0 {PT} Tf 1 0 0 1 {x} 6 Tm ({piece}) Tj ET Q'
            x += PT * len(piece)
        for ax, text in also:
            ops += f' q BT /NxF0 {PT} Tf 1 0 0 1 {ax} 6 Tm ({text}) Tj ET Q'
        page.contents_add(pikepdf.Stream(pdf, (ops + chr(10)).encode('latin1')),
                          prepend=False)
    pdf.save(str(path))
    pdf.close()
    return path


def _labels(path):
    with pikepdf.open(str(path)) as pdf:
        hits, _ = W.detect(pdf)
    return sorted(h.label() for h in hits)


def test_a_stamp_written_as_one_line_in_pieces_is_found_whole(tmp_path):
    labels = _labels(_one_line_stamp(tmp_path / 'oneline.pdf'))
    assert 'www.example-stamp.com' in labels, 'the link must not read as part of a sentence'
    assert 'Downloaded from' in labels and 'manuals search engine' in labels, \
        f'the words touching the link are part of the stamp: {labels}'


def test_a_stamp_written_as_one_line_in_pieces_is_removed_whole(tmp_path):
    f = _one_line_stamp(tmp_path / 'oneline.pdf')
    out = tmp_path / 'clean.pdf'
    res = W.clean_file(f, out, render=False)
    assert res.get('err') is None and res['pages'] == 4, res

    from pypdf import PdfReader
    for i, page in enumerate(PdfReader(str(out)).pages):
        t = page.extract_text() or ''
        for word in ('Downloaded', 'example-stamp', 'search engine'):
            assert word not in t, f'page {i + 1} kept {word!r}: {t!r}'
        assert f'Step {i + 1}: torque to 40 Nm.' in t, f'page {i + 1} lost its OCR layer'


def test_the_same_line_on_a_page_of_visible_text_is_left_alone(tmp_path):
    """THE GUARD, unchanged. Put the very same line on pages whose own words are visible
    type and it is what `_uncrowded` protects: a URL with 36 characters beside it, the shape
    of a publisher's address footer. Only a scan, where nothing else is visible text, lets
    the sole-line rule speak for it."""
    assert _labels(_one_line_stamp(tmp_path / 'typeset.pdf', body_shown=True)) == []


def test_a_sole_line_on_a_page_with_no_image_is_left_alone(tmp_path):
    """The rule is about a line written over a SCAN. A page with nothing but a typeset line
    -- an address footer on an otherwise empty sheet -- has one line of visible text too."""
    assert _labels(_one_line_stamp(tmp_path / 'noscan.pdf', scan=False)) == []


def test_text_on_the_stamps_baseline_but_apart_from_it_stays(tmp_path):
    """Same line is not enough: the publisher's form number at the far end of the stamp's
    baseline repeats on every page too, but there is a gap of 0.14 of the page between it
    and the stamp's last word. Only what touches the stamp goes with it."""
    labels = _labels(_one_line_stamp(tmp_path / 'apart.pdf', also=[(560, 'SM-12')]))
    assert 'www.example-stamp.com' in labels
    assert 'SM-12' not in labels, labels


def test_render_mode_follows_the_graphics_state():
    """`Tr` is saved by `q` and restored by `Q`; a run after the restore is visible again."""
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    U._add_font(pdf, U._sub_dict(page.obj, '/Resources'), '/NxF0')
    ops = (' q 3 Tr BT /NxF0 9 Tf 1 0 0 1 10 700 Tm (under the scan) Tj ET Q'
           ' BT /NxF0 9 Tf 1 0 0 1 10 10 Tm (on top) Tj ET')
    page.contents_add(pikepdf.Stream(pdf, (ops + chr(10)).encode('latin1')), prepend=False)
    runs = W.page_runs(pdf.pages[0], 0, __import__('collections').defaultdict(set))
    assert {r.text: r.hidden for r in runs} == {'under the scan': True, 'on top': False}


def test_the_beside_rule_reads_one_way():
    """A vertical run is never on a horizontal run's line, however close its start."""
    assert W._beside((0.10, 0.01, False, 0.20, 0.01), (0.21, 0.01, False, 0.30, 0.01))
    assert not W._beside((0.10, 0.01, False, 0.20, 0.01), (0.27, 0.01, False, 0.40, 0.01))
    assert not W._beside((0.10, 0.01, False, 0.20, 0.01), (0.21, 0.01, True, 0.21, 0.10))
    assert not W._beside((0.10, 0.01, False, 0.20, 0.01), (0.21, 0.02, False, 0.30, 0.02)), \
        'a different baseline is judged origin to origin only'


def test_the_beside_rule_runs_along_a_column_too():
    """Turned a quarter, the same line is a column: same x, the gap measured along y."""
    assert W._beside((0.99, 0.10, True, 0.99, 0.20), (0.99, 0.21, True, 0.99, 0.30))
    assert not W._beside((0.99, 0.10, True, 0.99, 0.20), (0.99, 0.27, True, 0.99, 0.40))
    assert not W._beside((0.99, 0.10, True, 0.99, 0.20), (0.95, 0.21, True, 0.95, 0.30))


def test_a_stamp_turned_up_the_page_edge_is_removed_whole(tmp_path):
    """The Hijet shape: the stamp reads across the foot of most pages, and up the edge of
    the last one, a landscape page. The words must go with the link there as well."""
    f = _one_line_stamp(tmp_path / 'turned.pdf', npages=4)
    with pikepdf.open(str(f), allow_overwriting_input=True) as pdf:
        page = pdf.pages[-1]
        ops, y = '', 3
        for piece in STAMP:                        # drawn upwards at x 606
            ops += f' q BT /NxF0 {PT} Tf 0 1 -1 0 606 {y} Tm ({piece}) Tj ET Q'
            y += PT * len(piece)
        img = ' q 612 0 0 792 0 0 cm /Im0 Do Q'
        body = ' q BT 3 Tr /NxF0 11 Tf 1 0 0 1 200 400 Tm (Step 4: torque to 40 Nm.) Tj ET Q'
        page.Contents = pdf.make_stream((img + body + ops + chr(10)).encode('latin1'))
        pdf.save()

    out = tmp_path / 'clean.pdf'
    res = W.clean_file(f, out, render=False)
    assert res.get('err') is None and res['pages'] == 4, res
    from pypdf import PdfReader
    t = PdfReader(str(out)).pages[-1].extract_text() or ''
    for word in ('Downloaded', 'example-stamp', 'search engine'):
        assert word not in t, f'the turned page kept {word!r}: {t!r}'
    assert 'Step 4: torque to 40 Nm.' in t
