"""A stamp written up the margin is on a line of its own even when a header shares its y.

The regression: a Toyota A442F repair manual carries `WWW.ALL-TRANS.BY` bottom-to-top in
the top-left margin of all 142 pages. Its start point sits on the same baseline as the
running header (`INTRODUCTION - ABBREVIATIONS USED IN THIS MANUAL`), so the line test
counted the header as the stamp's neighbours and the file was reported clean -- while 30
other books from the same site, whose pages have no header at that height, were cleaned.
"""
import pikepdf

import pdfwatermark as W
from tests import _util as U

HEADER = 'INTRODUCTION - ABBREVIATIONS USED IN THIS MANUAL'


def _book(path, stamp='WWW.ALL-TRANS.BY', column_text=None, npages=4):
    pdf = pikepdf.Pdf.new()
    for k in range(npages):
        page = pdf.add_blank_page(page_size=(612, 792))
        res = U._sub_dict(page.obj, '/Resources')
        U._add_font(pdf, res, '/VxF0')
        ops = [
            # running header, on the baseline where the stamp starts
            f'BT /VxF0 12 Tf 1 0 0 1 60 758 Tm ({HEADER}) Tj ET',
            f'BT /VxF0 11 Tf 1 0 0 1 60 400 Tm (Torque the bolt to {40 + k} Nm.) Tj ET',
            # the stamp, rotated 90 degrees: reads bottom-to-top from (31, 758)
            f'BT /VxF0 8 Tf 0 1 -1 0 31 758 Tm ({stamp}) Tj ET',
        ]
        if column_text:
            ops.append(f'BT /VxF0 11 Tf 1 0 0 1 31 300 Tm ({column_text}) Tj ET')
        page.contents_add(pikepdf.Stream(pdf, (' q ' + ' '.join(ops) + ' Q' + chr(10))
                                         .encode('latin1')), prepend=False)
    pdf.save(str(path))
    pdf.close()
    return path


def _detect(path):
    with pikepdf.open(str(path)) as pdf:
        hits, _ = W.detect(pdf)
    return [h.label() for h in hits]


def test_a_vertical_stamp_beside_a_header_is_found(tmp_path):
    assert _detect(_book(tmp_path / 'v.pdf')) == ['WWW.ALL-TRANS.BY']


def test_removal_takes_the_vertical_stamp_and_keeps_the_header(tmp_path):
    src = _book(tmp_path / 'src.pdf')
    out = tmp_path / 'out.pdf'
    res = W.clean_file(src, out, render=False)
    assert res and res['pages'] == 4, res

    from pypdf import PdfReader
    for i, page in enumerate(PdfReader(str(out)).pages):
        t = page.extract_text() or ''
        assert 'ALL-TRANS' not in t, f'page {i + 1} kept the stamp'
        assert HEADER in t, f'page {i + 1} lost its running header'
        assert f'Torque the bolt to {40 + i} Nm.' in t, f'page {i + 1} lost its body text'


def test_a_vertical_run_is_still_crowded_by_text_in_its_column(tmp_path):
    """The guard is not dropped for vertical runs, only turned the way they read: a line
    of text starting in the same column still makes it not a line of its own."""
    f = _book(tmp_path / 'c.pdf', column_text='see the parts list for this assembly')
    assert _detect(f) == []


def test_a_horizontal_stamp_is_judged_on_its_baseline_as_before(tmp_path):
    """The Motul case, unchanged: a URL sharing its baseline with an address block."""
    pdf = pikepdf.Pdf.new()
    for _k in range(4):
        page = pdf.add_blank_page(page_size=(612, 792))
        res = U._sub_dict(page.obj, '/Resources')
        U._add_font(pdf, res, '/VxF0')
        page.contents_add(pikepdf.Stream(pdf, (
            ' q BT /VxF0 9 Tf 1 0 0 1 60 40 Tm (Motul 119 bd Felix Faure 93300 Aubervilliers) Tj ET'
            ' BT /VxF0 9 Tf 1 0 0 1 400 40 Tm (: www.motul.fr) Tj ET Q' + chr(10))
            .encode('latin1')), prepend=False)
    f = tmp_path / 'h.pdf'
    pdf.save(str(f))
    pdf.close()
    assert _detect(f) == []
