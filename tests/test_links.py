"""Tests for the browser link fix (`pdflinks`) and its wiring into the pipeline.

No browser follows `/GoToR` or `/Launch`, so a contents page built from them looks fine and
does nothing. These assert the rewrite is CORRECT rather than merely present: the URL has to
carry the spelling the file has on disk, the page the destination really points at, and
nothing at all when the target cannot be found beside the linking file.
"""
import sys
from pathlib import Path

import pikepdf
import pytest

import _util as U

import pdflinks

owm = U.owm
_missing = U.tools_missing()


def _uris(pdf_path: Path) -> list:
    with pikepdf.open(str(pdf_path)) as pdf:
        return pdflinks.written_uris(pdf)


# ── the rewrite itself ────────────────────────────────────────────────────────

def test_every_link_shape_is_rewritten_correctly(tmp_path):
    """One assertion per real-world shape — see `make_gotor_manual` for what each is."""
    contents = U.make_gotor_manual(tmp_path / 'man')
    stats, err = pdflinks.fix_links_file(contents)
    assert not err, err
    assert stats.converted == 6, stats
    assert stats.unresolved == 1, 'the target that exists nowhere must stay /GoToR'
    assert stats.named == 1, 'the symbolic /D (ANCHOR) must resolve to a real page'
    assert stats.clamped == 1, 'a page past the end of the target must be pulled back'
    assert stats.outline == 1, 'the bookmark tree is where the sidebar menu lives'

    urls = _uris(contents)
    # miscased name + 0-based remote page index -> disk spelling, 1-based page
    assert 'sec.pdf#page=2' in urls
    # /Launch carries no destination, so no fragment -- and the file is `fwd.pdf`
    assert 'fwd.pdf' in urls
    # the symbolic destination lives in the TARGET's name tree and points at page 3
    assert 'sec.pdf#page=3' in urls
    # /D [99] on a 4-page target
    assert 'sec.pdf#page=4' in urls
    # the link says GI.pdf; the file is gi-general_information.pdf
    assert 'gi-general_information.pdf' in urls
    # an existing /URI is left exactly as it was
    assert 'https://example.invalid/already' in urls
    assert not any('nowhere' in u for u in urls), 'must not invent a URL for a missing file'


def test_internal_goto_links_are_untouched(tmp_path):
    """`/GoTo` already works in every browser. Touching it would be pure risk."""
    contents = U.make_gotor_manual(tmp_path / 'man')

    def gotos(p):
        with pikepdf.open(str(p)) as pdf:
            return [str(h.get('/A').get('/S')) for h in pdflinks.action_holders(pdf)
                    if h.get('/A') is not None and hasattr(h.get('/A'), 'get')
                    and str(h.get('/A').get('/S')) == '/GoTo']

    before = gotos(contents)
    assert before == ['/GoTo'], 'fixture must carry exactly one internal link'
    pdflinks.fix_links_file(contents)
    assert gotos(contents) == before


def test_rerun_is_a_no_op(tmp_path):
    """Re-runnable by construction: the second pass finds nothing left to convert and
    writes nothing, so a tree can be swept twice without churning every file."""
    contents = U.make_gotor_manual(tmp_path / 'man')
    first, err = pdflinks.fix_links_file(contents)
    assert not err and first.converted
    before = contents.read_bytes()
    second, err = pdflinks.fix_links_file(contents)
    assert not err
    assert second.converted == 0
    assert contents.read_bytes() == before, 'a no-op pass must not rewrite the file'


def test_pages_and_link_count_survive(tmp_path):
    """The rewrite may change an action; it may never lose a page or a link."""
    contents = U.make_gotor_manual(tmp_path / 'man')
    with pikepdf.open(str(contents)) as pdf:
        pages_before = len(pdf.pages)
        links_before = sum(1 for _ in pdflinks.action_holders(pdf))
    pdflinks.fix_links_file(contents)
    with pikepdf.open(str(contents)) as pdf:
        assert len(pdf.pages) == pages_before
        assert sum(1 for _ in pdflinks.action_holders(pdf)) == links_before


def test_a_target_in_another_folder_is_never_searched_for(tmp_path):
    """The measured reason there is no tree walk.

    Section names repeat across a collection — one brand carries 436 files called `gi.pdf` —
    so a same-named file found by walking up is usually another vehicle. Measured over 370
    real links: walking up resolved 0 correctly and 5 into a different car's manual. A
    wrong-car link points at a file that really exists, so no later check can catch it; the
    only defence is not to guess. Here `sec.pdf` exists in a SIBLING manual and the link
    must still be left alone."""
    U.make_gotor_manual(tmp_path / 'other_car')      # has a sec.pdf of its own
    folder = tmp_path / 'this_car'
    contents = U.make_gotor_manual(folder)
    (folder / 'sec.pdf').unlink()                    # now only the other car has one
    stats, err = pdflinks.fix_links_file(contents)
    assert not err, err
    assert not any('sec.pdf' in u for u in _uris(contents)), \
        'a target outside the linking file\'s own folder must not be linked'
    assert stats.unresolved >= 3, stats


def test_a_miscased_url_is_refused_by_the_verifier(tmp_path):
    """The guard that makes this safe to ship: a URL is re-read off the directory listing
    case-sensitively, because one that opens on Windows and 404s on a Linux origin is worse
    than the inert link it replaced."""
    folder = tmp_path / 'man'
    U.make_gotor_manual(folder)
    assert pdflinks.verify_uris(folder / 'contents.pdf', folder) == '', 'source has no URIs yet'
    bad = folder / 'bad.pdf'
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(300, 400))
    page.obj['/Annots'] = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link,
        Rect=pikepdf.Array([0, 0, 10, 10]),
        A=pikepdf.Dictionary(Type=pikepdf.Name.Action, S=pikepdf.Name.URI,
                             URI=pikepdf.String('SEC.pdf#page=1'))))])
    pdf.save(str(bad))
    pdf.close()
    why = pdflinks.verify_uris(bad, folder)
    assert 'case-sensitively' in why, why


def test_a_page_past_the_end_is_refused_by_the_verifier(tmp_path):
    """`#page=` beyond the target's length sends Chrome to page 1 — the wrong end of the
    document. Clamping prevents it; this proves the verifier would catch it anyway."""
    folder = tmp_path / 'man'
    U.make_gotor_manual(folder)
    bad = folder / 'bad.pdf'
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(300, 400))
    page.obj['/Annots'] = pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Link,
        Rect=pikepdf.Array([0, 0, 10, 10]),
        A=pikepdf.Dictionary(Type=pikepdf.Name.Action, S=pikepdf.Name.URI,
                             URI=pikepdf.String('sec.pdf#page=99'))))])
    pdf.save(str(bad))
    pdf.close()
    why = pdflinks.verify_uris(bad, folder)
    assert 'past the end' in why, why


def test_an_unreadable_file_is_reported_not_raised(tmp_path):
    """A corrupt file in a 10,000-file run must cost that file's links and nothing else."""
    junk = tmp_path / 'notapdf.pdf'
    junk.write_bytes(b'<html>404 not found</html>')   # a failed download saved as .pdf
    stats, err = pdflinks.fix_links_file(junk)
    assert stats.converted == 0
    assert err and 'cannot open' in err
    assert junk.read_bytes() == b'<html>404 not found</html>', 'the file must be untouched'


def test_predict_writes_nothing(tmp_path):
    """--dry-run must be able to count what a pass would do without doing any of it."""
    contents = U.make_gotor_manual(tmp_path / 'man')
    before = contents.read_bytes()
    stats, err = pdflinks.predict(contents)
    assert not err and stats.converted == 6
    assert contents.read_bytes() == before


def test_section_code_matches_in_both_directions():
    """`GI.pdf` -> `gi-general_information.pdf` was the Nissan case. Mitsubishi CD manuals
    do the reverse: the link says `GR00005000-52B.pdf` (document id + group code) and the
    folder holds the bare `52B.pdf`. A different document id with the same code must NOT
    be taken for it -- the name cannot say whether it is the same document."""
    renamed = ['gi-general_information.pdf', 'em-engine_mechanical.pdf', 'ecu.pdf']
    assert pdflinks.by_section_code(renamed, 'GI.pdf') == 'gi-general_information.pdf'
    assert pdflinks.by_section_code(renamed, 'EC.pdf') is None, 'EC must not match ECU'

    bare = ['00.pdf', '52B.pdf', '13A.pdf']
    assert pdflinks.by_section_code(bare, 'GR00005000-52B.pdf') == '52B.pdf'
    assert pdflinks.by_section_code(bare, 'GR00004300D-13A.pdf') == '13A.pdf'
    assert pdflinks.by_section_code(bare, 'GR00009999-54A.pdf') is None

    other_id = ['GR00001300-52B.pdf']
    assert pdflinks.by_section_code(other_id, 'GR00005200-52B.pdf') is None, \
        'a different document id with the same group code is not the same document'


def test_one_bad_destination_does_not_lose_the_whole_name_tree(tmp_path):
    """The failure mode of the old `'/D' in <Array>` test: one entry raising inside the
    tree walk threw away every name in the target. Here one destination is garbage and
    the other must still resolve."""
    target = tmp_path / 'sec.pdf'
    pdf = pikepdf.Pdf.new()
    for _ in range(3):
        pdf.add_blank_page(page_size=(300, 400))
    good = pikepdf.Array([pdf.pages[2].obj, pikepdf.Name.Fit])
    pdf.Root.Names = pikepdf.Dictionary(Dests=pikepdf.Dictionary(Names=pikepdf.Array([
        pikepdf.String('BAD'), pikepdf.Dictionary(D=pikepdf.Name('/Nonsense')),
        pikepdf.String('GOOD'), good,
    ])))
    pdf.save(str(target))
    pdf.close()
    d = pdflinks.Destinations()
    assert d.page(target, 'GOOD') == 3
    assert d.page(target, 'BAD') is None
    assert d.count(target) == 3


# ── wiring into the pipeline ──────────────────────────────────────────────────

@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_born_digital_file_still_gets_its_links_fixed(tmp_path):
    """The integration point that matters.

    Every archive file carrying these links is born-digital, so it takes the lane that
    copies bytes or rewrites losslessly and never goes near the raster pipeline. A link fix
    wired into the compress path alone would be a no-op on exactly the files that need it.
    """
    src = tmp_path / 'src'
    U.make_gotor_manual(src)
    dest = tmp_path / 'out'
    res = owm.compress_one(str(src / 'contents.pdf'), str(dest / 'contents.pdf'),
                           200, ocr=False)
    assert not res.get('err'), res
    assert res['links'].converted == 6, res
    urls = _uris(dest / 'contents.pdf')
    assert 'sec.pdf#page=3' in urls
    # and the source is left exactly as it was
    assert any('GoToR' in str(h.get('/A').get('/S'))
               for h in pdflinks.action_holders(pikepdf.open(str(src / 'contents.pdf')))
               if h.get('/A') is not None and hasattr(h.get('/A'), 'get'))


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_no_fix_links_leaves_the_file_alone(tmp_path):
    """The opt-out has to reach the worker, not just the argument parser."""
    src = tmp_path / 'src'
    U.make_gotor_manual(src)
    dest = tmp_path / 'out'
    res = owm.compress_one(str(src / 'contents.pdf'), str(dest / 'contents.pdf'),
                           200, ocr=False, fix_links=False)
    assert not res.get('err'), res
    assert 'links' not in res
    assert not any(u.endswith('.pdf') or '#page=' in u for u in _uris(dest / 'contents.pdf'))


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_in_place_row_reports_the_link_fix_and_the_new_size(tmp_path):
    """An in-place file that needed no compression used to report `unchanged; left in
    place`. Once its links are rewritten that row would be a lie, and its `new size` would
    be the old one."""
    folder = tmp_path / 'man'
    contents = U.make_gotor_manual(folder)
    res = owm.compress_one(str(contents), str(contents), 200, ocr=False, in_place=True)
    assert not res.get('err'), res
    assert res['links'].converted == 6
    assert 'unchanged; left in place' not in res['note'], res['note']
    assert res['new'] == contents.stat().st_size
    assert 'sec.pdf#page=3' in _uris(contents)


def test_report_carries_the_link_columns(tmp_path):
    """Two columns, and blank rather than 0 on a file that has no such links at all."""
    stats = pdflinks.LinkFix(converted=6, named=1, clamped=1, outline=1,
                             unresolved=2, noname=0)
    row = owm._report_row({'path': 'a.pdf', 'orig': 1, 'new': 1, 'links': stats})
    cols = dict(zip(owm.REPORT_COLUMNS, row))
    assert cols['links fixed'] == 6
    assert cols['links left'] == 2
    blank = dict(zip(owm.REPORT_COLUMNS, owm._report_row({'path': 'b.pdf', 'orig': 1})))
    assert blank['links fixed'] == '' and blank['links left'] == ''
