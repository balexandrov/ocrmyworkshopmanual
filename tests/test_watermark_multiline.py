"""A stamp is a BLOCK, not a line, and removing half of one leaves the file stamped.

THE BUG. The 1997 Mazda 626 manual carries two lines in the same corner of all 662 pages:

    ry=0.973  http://www.adultpdf.com
    ry=0.963  Created by TIFF To PDF trial version, to remove this mark, please
              register this software.

The link test saw the first and not the second -- the second is pure prose, with no URL
and no bare host in it -- so 662 pages were reported clean while still carrying the nag.

Two rules close it, and they cover different files:
  * `looks_like_nag` recognises a converter's licence wording directly, so a nag is caught
    even on a file that carries NO accompanying URL line
  * `_companions` takes lines that repeat character for character ADJACENT to a confirmed
    stamp, which reaches a line that carries no recognisable marker at all
"""
import pikepdf
import pytest

import pdfwatermark as W
import _util as U


def _detect(path):
    with pikepdf.open(str(path)) as pdf:
        hits, _ = W.detect(pdf)
    return hits


def _labels(path):
    return sorted(h.label() for h in _detect(path))


# ── the phrase gate ───────────────────────────────────────────────────────────

@pytest.mark.parametrize('text', [
    'Created by TIFF To PDF trial version, to remove this mark, please register '
    'this software.',
    'This document was created with an evaluation copy of the converter',
    'Unregistered version - purchase the full version to remove this watermark',
])
def test_a_converter_nag_is_a_stamp_even_with_no_url_in_it(text):
    assert W.stamp_marker(text), f'not recognised: {text!r}'


@pytest.mark.parametrize('text', [
    'Road trial results are recorded in Section 4',
    'Please register your vehicle with the dealer before delivery',
    'Remove this mark from the housing before reassembly',
    'Torque the bolt to 45 Nm and inspect the seal',
])
def test_a_manual_talking_about_trials_and_registration_is_not_a_stamp(text):
    """The phrases are about SOFTWARE LICENSING on purpose. A looser `trial` or `register`
    matches a workshop manual's own sentences, which is how a phrase list starts deleting
    publisher content."""
    assert W.looks_like_nag(text) is None, f'false positive on {text!r}'


def test_a_nag_with_no_url_beside_it_is_found_and_removed(tmp_path):
    f = U.make_rotated_stamped_pdf(
        tmp_path / 'nag.pdf', npages=4, rotate=0,
        stamp='Created by TIFF To PDF trial version, please register this software')
    assert _detect(f), 'a nag alone must be enough to open a candidate'

    out = tmp_path / 'clean.pdf'
    res = W.clean_file(f, out, render=False)
    assert res['pages'] == 4, res

    from pypdf import PdfReader
    for i, page in enumerate(PdfReader(str(out)).pages):
        t = page.extract_text() or ''
        assert 'trial version' not in t, f'page {i + 1} kept the nag'
        assert f'torque the bolt to {40 + i} Nm' in t, f'page {i + 1} lost its body text'


# ── the adjacency rule ────────────────────────────────────────────────────────

def _stamped_block(path, npages=4, extra=(), far=()):
    """A stamp of several lines in one corner, plus optional lines elsewhere on the page.

    `extra` lines sit just above the marker line (a real stamping tool's block); `far`
    lines repeat identically too but live at the other end of the paper, standing in for
    a publisher's own running footer.
    """
    pdf = pikepdf.Pdf.new()
    for k in range(npages):
        page = pdf.add_blank_page(page_size=(612, 792))
        res = U._sub_dict(page.obj, '/Resources')
        U._add_font(pdf, res, '/NxF0')
        ops = f' q BT /NxF0 11 Tf 1 0 0 1 200 400 Tm (Section {k + 1}: torque 40 Nm.) Tj ET Q'
        ops += ' q BT /NxF0 8 Tf 1 0 0 1 20 24 Tm (www.example-stamp.org) Tj ET Q'
        for j, line in enumerate(extra):
            ops += f' q BT /NxF0 8 Tf 1 0 0 1 20 {32 + j * 8} Tm ({line}) Tj ET Q'
        for j, line in enumerate(far):
            ops += f' q BT /NxF0 8 Tf 1 0 0 1 20 {700 + j * 8} Tm ({line}) Tj ET Q'
        page.contents_add(pikepdf.Stream(pdf, (ops + chr(10)).encode('latin1')),
                          prepend=False)
    pdf.save(str(path))
    pdf.close()
    return path


def test_a_markerless_line_beside_the_stamp_goes_with_it(tmp_path):
    """The line has no URL and no nag wording — nothing recognises it on its own. It is
    removed because it repeats, character for character, right next to a confirmed stamp.
    """
    f = _stamped_block(tmp_path / 'block.pdf',
                       extra=['Scanned and shared by a friendly stranger'])
    assert _labels(f) == ['Scanned and shared by a friendly stranger',
                          'www.example-stamp.org']


def test_a_running_footer_far_from_the_stamp_is_left_alone(tmp_path):
    """The other half of the rule. This line repeats identically on every page too, but it
    is the publisher's, at the other end of the paper. Repetition alone would delete it.
    """
    f = _stamped_block(tmp_path / 'footer.pdf',
                       far=['MAZDA 626 WORKSHOP MANUAL 1997'])
    assert _labels(f) == ['www.example-stamp.org']


def test_a_line_that_changes_per_page_is_never_a_companion(tmp_path):
    """A page number sits right beside the stamp and repeats in POSITION on every page,
    but not in TEXT. That is what keeps it."""
    pdf = pikepdf.Pdf.new()
    for k in range(4):
        page = pdf.add_blank_page(page_size=(612, 792))
        U._add_font(pdf, U._sub_dict(page.obj, '/Resources'), '/NxF0')
        ops = (' q BT /NxF0 8 Tf 1 0 0 1 20 24 Tm (www.example-stamp.org) Tj ET Q'
               f' q BT /NxF0 8 Tf 1 0 0 1 20 34 Tm (Page {k + 1} of 4) Tj ET Q')
        page.contents_add(pikepdf.Stream(pdf, (ops + chr(10)).encode('latin1')),
                          prepend=False)
    f = tmp_path / 'pageno.pdf'
    pdf.save(str(f))
    pdf.close()
    assert _labels(f) == ['www.example-stamp.org']


def test_companions_need_a_confirmed_stamp_to_anchor_to(tmp_path):
    """The rule cannot reach a file with no stamp: with nothing confirmed there is no
    anchor, so a repeating footer on a clean file stays put."""
    pdf = pikepdf.Pdf.new()
    for k in range(4):
        page = pdf.add_blank_page(page_size=(612, 792))
        U._add_font(pdf, U._sub_dict(page.obj, '/Resources'), '/NxF0')
        ops = (' q BT /NxF0 8 Tf 1 0 0 1 20 24 Tm (MAZDA 626 WORKSHOP MANUAL) Tj ET Q'
               ' q BT /NxF0 8 Tf 1 0 0 1 20 34 Tm (Body and Chassis) Tj ET Q')
        page.contents_add(pikepdf.Stream(pdf, (ops + chr(10)).encode('latin1')),
                          prepend=False)
    f = tmp_path / 'clean.pdf'
    pdf.save(str(f))
    pdf.close()
    assert not _detect(f)


# ── the crowding guard: a URL inside a sentence is not a stamp ─────────────────

def _footer_pdf(path, npages=4):
    """A manufacturer's own datasheet: its URL sits in its address block, on a line with
    the street, the telephone and fax numbers and a date.

    The shape of two Motul product datasheets in the archive, where `: www.motul.fr`
    repeats in the bottom margin of every page and satisfies every other test. Removing it
    deletes a manufacturer's contact details from the manufacturer's own document.
    """
    parts = ['MOTOR OIL CO', '. 119 Bd Example - 93303 SOMEWHERE CEDEX - BP 94 - Tel',
             ': 33 1 48 11 70 00 - FAX', ': 33 1 48 33 28 79 - Site', 'Web',
             ': www.example-lubricants.fr', '05/02']
    pdf = pikepdf.Pdf.new()
    for k in range(npages):
        page = pdf.add_blank_page(page_size=(612, 792))
        U._add_font(pdf, U._sub_dict(page.obj, '/Resources'), '/NxF0')
        ops = f' q BT /NxF0 11 Tf 1 0 0 1 60 400 Tm (Product sheet page {k + 1}.) Tj ET Q'
        x = 40
        for part in parts:                         # ALL AT THE SAME y: one printed line
            ops += f' q BT /NxF0 7 Tf 1 0 0 1 {x} 26 Tm ({part}) Tj ET Q'
            x += 6 * len(part)
        page.contents_add(pikepdf.Stream(pdf, (ops + chr(10)).encode('latin1')),
                          prepend=False)
    pdf.save(str(path))
    pdf.close()
    return path


def test_a_makers_own_url_in_its_address_block_is_not_a_stamp(tmp_path):
    """THE FALSE POSITIVE. It is link-like, it is in the bottom margin, and it repeats on
    every page — it passes every other test the tool has. What it is not is alone on its
    line, and that is the whole difference between a mark someone added and a line the
    publisher typeset."""
    assert not _detect(_footer_pdf(tmp_path / 'sheet.pdf'))


def test_the_same_url_on_a_line_of_its_own_is_a_stamp(tmp_path):
    """The control for the guard above: nothing changed but the company of the run."""
    f = U.make_rotated_stamped_pdf(tmp_path / 'stamped.pdf', npages=4, rotate=0,
                                   stamp='www.example-lubricants.fr')
    assert [h.label() for h in _detect(f)] == ['www.example-lubricants.fr']


def test_a_stamp_survives_ocr_specks_landing_on_its_line(tmp_path):
    """The guard must not be so strict that a scan's noise defeats it. The Mazda nag line
    shares its line with the OCR fragments 'he' and 'i' off the page underneath — 3
    characters against Motul's 124, which is why the rule counts characters rather than
    asking whether the line is empty."""
    f = U.make_rotated_stamped_pdf(tmp_path / 'speck.pdf', npages=4, rotate=0,
                                   stamp='www.example-stamp.org')
    with pikepdf.open(str(f), allow_overwriting_input=True) as pdf:
        for k, page in enumerate(pdf.pages):
            U._add_font(pdf, U._sub_dict(page.obj, '/Resources'), '/NxF1')
            # two specks on the stamp's own baseline (the fixture stamps at y=473.8)
            ops = (' q BT /NxF1 6 Tf 1 0 0 1 300 473.8 Tm (he) Tj ET Q'
                   ' q BT /NxF1 6 Tf 1 0 0 1 340 473.8 Tm (i) Tj ET Q')
            page.contents_add(pikepdf.Stream(pdf, (ops + chr(10)).encode('latin1')),
                              prepend=False)
        pdf.save()
    assert [h.label() for h in _detect(f)] == ['www.example-stamp.org']


def test_a_noisy_page_still_gets_the_stamp_removed(tmp_path):
    """THE REGRESSION, and it cost a 662-page file a FAILED audit to find.

    The crowding test asks "is this a stamp, or part of a sentence?". That is a question
    about the FILE, settled once from the sampled pages. Re-asking it per page makes the
    answer depend on whatever noise the scan carries: measured on the Mazda 626, page 662
    is a dense scan (1,300 runs against page 1's 168) whose OCR specks land on the nag's
    baseline and come to 23 characters against page 1's 3. With the test in the removal
    path the stamp survived on that one page of 662, the audit caught the mismatch, and
    the whole file was refused.

    So: a page crowded with noise must still have its stamp taken off. What keeps removal
    honest is that a run is flagged only when its WHOLE text equals a confirmed stamp.
    """
    f = U.make_rotated_stamped_pdf(tmp_path / 'noisy.pdf', npages=4, rotate=0,
                                   stamp='www.example-stamp.org')
    # bury the LAST page's stamp line in OCR specks, the way a dense scan does
    with pikepdf.open(str(f), allow_overwriting_input=True) as pdf:
        page = pdf.pages[-1]
        U._add_font(pdf, U._sub_dict(page.obj, '/Resources'), '/NxF2')
        ops = ''.join(
            f' q BT /NxF2 6 Tf 1 0 0 1 {120 + i * 14} 473.8 Tm (.,-*{i}) Tj ET Q'
            for i in range(12))
        page.contents_add(pikepdf.Stream(pdf, (ops + chr(10)).encode('latin1')),
                          prepend=False)
        pdf.save()

    out = tmp_path / 'clean.pdf'
    res = W.clean_file(f, out, render=False)
    assert res.get('err') is None, res
    assert res['pages'] == 4, f'the noisy page must be cleaned too: {res}'

    from pypdf import PdfReader
    for i, page in enumerate(PdfReader(str(out)).pages):
        assert 'example-stamp' not in (page.extract_text() or ''), f'page {i + 1} kept it'
