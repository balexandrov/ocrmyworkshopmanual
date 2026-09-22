"""The stamp has to be judged where the READER sees it, not in content coordinates.

The regression: a 346-page JK body-repair manual stamped `GETtheMANUALS.org` on every
page reported "no repeating margin link found". Its pages carry /Rotate 270, so the
stamp's content y of 473.8 in a 792-high box is 0.60 — mid-page — and the band test
returned None on all 346. In display space the same point is 0.02 up a 612-high page:
the bottom band. Ghostscript honours /Rotate, so the render half of the audit was
already judging in that frame; only the text half was not.
"""
import pikepdf
import pytest

import pdfwatermark as W
from tests import _util as U


def _detect(path):
    with pikepdf.open(str(path)) as pdf:
        hits, _ = W.detect(pdf)
    return hits


# Position is no longer a condition at all (see `detect`), so the stamp is found under
# every /Rotate. What the rotation still has to be right about is the ORIGIN the audit
# compares rendered pixels against — that is what `_display` computes.
@pytest.mark.parametrize('rotate', (0, 90, 180, 270))
def test_a_repeating_stamp_is_found_whatever_rotate_says(tmp_path, rotate):
    f = U.make_rotated_stamped_pdf(tmp_path / f'r{rotate}.pdf', npages=4, rotate=rotate)
    hits = _detect(f)
    assert [h.label() for h in hits] == ['GETtheMANUALS.org'], f'/Rotate {rotate}'


@pytest.mark.parametrize('rotate, ry', [(270, 0.02), (90, 0.98), (180, 0.40), (0, 0.60)])
def test_the_reported_position_is_where_the_reader_sees_it(tmp_path, rotate, ry):
    """The same left-edge stamp lands somewhere different on the paper per /Rotate: 270
    puts it 2% up the page (the real file), 90 puts it at the top, 180 and 0 mid-page.
    The audit checks rendered ink against these numbers, so they have to be the ones a
    reader would name."""
    f = U.make_rotated_stamped_pdf(tmp_path / f'p{rotate}.pdf', npages=4, rotate=rotate)
    hit = _detect(f)[0]
    got = {round(y, 2) for _x, y in hit.seen}
    assert got == {round(ry, 2)}, f'/Rotate {rotate}: {got}'


def test_removal_takes_the_stamp_off_every_rotated_page_and_nothing_else(tmp_path):
    src = U.make_rotated_stamped_pdf(tmp_path / 'src.pdf', npages=4)
    out = tmp_path / 'out.pdf'
    res = W.clean_file(src, out, render=False)
    assert res and res['pages'] == 4, res

    from pypdf import PdfReader
    a, b = PdfReader(str(src)), PdfReader(str(out))
    assert len(a.pages) == len(b.pages) == 4
    for i, page in enumerate(b.pages):
        t = page.extract_text() or ''
        assert 'GETtheMANUALS' not in t, f'page {i + 1} kept the stamp'
        assert f'torque the bolt to {40 + i} Nm' in t, f'page {i + 1} lost its body text'


def test_a_url_cited_once_in_the_body_is_never_touched(tmp_path):
    """The control, and the whole safety property now that position is not a condition.

    A manual legitimately prints a URL — a parts site, a standards body. What it does not
    do is print the same one, character for character, from the same operator, on page
    after page. Repetition is the discriminator; if this ever fails, the tool has started
    deleting publisher content.
    """
    import pikepdf as _pk
    f = tmp_path / 'cited.pdf'
    pdf = _pk.Pdf.new()
    for k in range(6):
        page = pdf.add_blank_page(page_size=(612, 792))
        res = U._sub_dict(page.obj, '/Resources')
        U._add_font(pdf, res, '/NxF0')
        # Page 3 cites a URL mid-paragraph; page 5 cites a different one in the footer.
        cite = {2: 'see www.sae.org for the torque standard',
                4: 'www.mopar.com'}.get(k, f'Step {k + 1}: check the seal.')
        page.contents_add(_pk.Stream(pdf, (
            f' q BT /NxF0 11 Tf 1 0 0 1 60 {60 if k == 4 else 400} Tm '
            f'({cite}) Tj ET Q' + chr(10)).encode('latin1')), prepend=False)
    pdf.save(str(f))
    pdf.close()
    assert not _detect(f), 'a URL that appears once is a citation, not a stamp'


# ── the audit that replaced the margin band ───────────────────────────────────

def _audit(monkeypatch, boxes, spots):
    """Run the render audit over canned render results, so the judgement is tested
    without paying for Ghostscript."""
    monkeypatch.setattr(W, '_changed_box',
                        lambda src, out, i: (boxes.get(i), None))
    return W._render_audit('src', 'out', sorted(boxes), spots)


STAMP = (0.38, 0.95, 0.52, 0.99)          # a stamp-sized hole at the foot of the page
AT = [(0.40, 0.03)]                        # ry grows up, so 0.03 is 0.97 in image space


def test_a_stamp_removed_from_the_same_place_on_every_page_passes(monkeypatch):
    assert _audit(monkeypatch, {0: STAMP, 1: STAMP, 2: STAMP},
                  {0: AT, 1: AT, 2: AT}) == []


def test_ink_changing_where_no_operator_was_deleted_is_damage(monkeypatch):
    """Containment. The stamp was drawn at the foot, but the ink that vanished is a
    figure in the middle of the page — that is not our removal."""
    bad = _audit(monkeypatch, {0: (0.2, 0.3, 0.7, 0.6)}, {0: AT})
    assert bad and 'something else moved' in bad[0]


def test_one_stamp_cannot_leave_two_different_holes(monkeypatch):
    """Consistency, and the half a margin band could never do. Both pages change ink in
    the bottom band, so the OLD check passed them; page 2 lost something much bigger."""
    bad = _audit(monkeypatch, {0: STAMP, 1: (0.05, 0.80, 0.95, 0.99)},
                 {0: AT, 1: AT})
    assert bad and 'two different holes' in bad[0]


def test_a_stamp_removed_from_mid_page_is_fine(monkeypatch):
    """The regression the old rule caused: a stamp that really is mid-page used to FAIL
    the render audit, so the file was rejected rather than cleaned."""
    mid = (0.40, 0.45, 0.60, 0.52)
    assert _audit(monkeypatch, {0: mid, 1: mid}, {0: [(0.41, 0.52)], 1: [(0.41, 0.52)]}) == []


def test_a_page_whose_ink_did_not_change_is_not_damage(monkeypatch):
    """An invisible stamp (white on white, or clipped away) leaves no hole. Removing it
    changed nothing a reader can see, which is not a reason to fail the file."""
    assert _audit(monkeypatch, {0: STAMP, 1: None}, {0: AT, 1: AT}) == []
