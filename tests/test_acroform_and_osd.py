"""Tests for the three faults that a single 4-page Nissan file (R50/R51 Pathfinder
`fwd.pdf`) tripped in sequence, plus the OSD mis-vote that made its text useless:

  - the binarisation guard must not call an ALREADY-1-bit source page our own damage
  - the graft must survive a /Contents ARRAY (or it drops every link and bookmark)
  - a fillable form makes ocrmypdf refuse --redo-ocr, so the text arrives by graft
  - a marginal non-Latin OSD vote must not outvote the summed Latin evidence
"""
import shutil
import types
from pathlib import Path

import pikepdf
from pypdf import PdfReader

import _util as U

owm = U.owm


def _page_ops(page) -> bytes:
    """Everything the page's content stream(s) paint, whichever shape /Contents is in."""
    c = page.obj.Contents
    if isinstance(c, pikepdf.Array):
        return b''.join(bytes(s.read_bytes()) for s in c)
    return bytes(c.read_bytes())


# -- fillable-form detection -------------------------------------------------

def test_acroform_detection_matches_ocrmypdf_rule(tmp_path):
    withf = U.make_acroform_array_contents_pdf(tmp_path / 'form.pdf', widget=True)
    without = U.make_acroform_array_contents_pdf(tmp_path / 'plain.pdf', widget=False)
    assert owm._has_acroform(withf) is True
    assert owm._has_acroform(without) is False


def test_acroform_detection_ignores_an_empty_fields_array(tmp_path):
    """ocrmypdf requires a NON-EMPTY /Fields (or an /XFA); an empty one is not a form,
    and treating it as one would send files down the slower donor path for nothing."""
    p = U.make_acroform_array_contents_pdf(tmp_path / 'empty.pdf', widget=False)
    with pikepdf.open(str(p), allow_overwriting_input=True) as pdf:
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([]))
        pdf.save()
    assert owm._has_acroform(p) is False


def test_missing_or_unreadable_file_is_not_a_form(tmp_path):
    junk = tmp_path / 'junk.pdf'
    junk.write_bytes(b'not a pdf')
    assert owm._has_acroform(junk) is False
    assert owm._has_acroform(tmp_path / 'nope.pdf') is False


# -- the binarisation guard is DIFFERENTIAL ----------------------------------

def test_already_bilevel_colour_page_is_not_reported_as_binarised(tmp_path):
    """The page is classified COLOUR (its colour lives in a widget appearance stream) but
    its raster was 1-bit to begin with, so a 1-bit output cannot be our damage. This is
    the false FAIL that left the file both uncompressed AND unsearchable."""
    src = U.make_acroform_array_contents_pdf(tmp_path / 'src.pdf')
    out = tmp_path / 'out.pdf'
    shutil.copyfile(src, out)
    fatal, _warn = owm._audit_output(out, 2, src_p=src, colour_pages={0})
    assert not fatal


def test_genuinely_binarised_colour_page_is_still_fatal(tmp_path):
    """The guard must keep doing its real job: an 8-bit colour source flattened to 1-bit
    is the archive-wide failure it was written for."""
    src = U.make_color_pdf(tmp_path / 'colour.pdf')
    n = len(PdfReader(str(src)).pages)
    out = U.make_acroform_array_contents_pdf(tmp_path / 'bilevel.pdf', npages=n,
                                             widget=False, nbookmarks=0)
    fatal, _warn = owm._audit_output(out, n, src_p=src, colour_pages={0})
    assert fatal and 'binarised' in fatal


def test_mixed_source_page_is_still_checked(tmp_path):
    """Skipping is only right when EVERY source image on the page was already 1-bit. A
    page carrying both a 1-bit scan and an 8-bit photo still has colour to lose."""
    src = U.make_acroform_array_contents_pdf(tmp_path / 'mixed.pdf', nbookmarks=0)
    with pikepdf.open(str(src), allow_overwriting_input=True) as pdf:
        im = pikepdf.Stream(pdf, bytes([16, 32, 48]) * 16)
        im.Type, im.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
        im.Width, im.Height, im.BitsPerComponent = 4, 4, 8
        im.ColorSpace = pikepdf.Name.DeviceRGB
        pdf.pages[0].Resources.XObject.Photo = pdf.make_indirect(im)
        pdf.save()
    assert owm._page_image_bpcs(PdfReader(str(src)).pages[0]) == {1, 8}
    out = U.make_acroform_array_contents_pdf(tmp_path / 'flat.pdf', nbookmarks=0)
    fatal, _warn = owm._audit_output(out, 2, src_p=src, colour_pages={0})
    assert fatal and 'binarised' in fatal


def test_mrc_page_passed_through_is_not_called_binarised(tmp_path):
    """An MRC page is 8-bit tiles PLUS a 1-bit mask, so bpcs are {8, 1} in the source AND
    the output. Asking "is there a 1-bit image here" failed the file and cost it its OCR --
    measured on 2008 OpticBook Nissan 300ZX scans, where PT_COLOR_LINE correctly passed the
    original page through and the guard read our own passed-through mask as damage."""
    src = U.make_mrc_pdf(tmp_path / 'mrc.pdf', npages=2)
    out = tmp_path / 'out.pdf'
    shutil.copyfile(src, out)
    assert owm._page_image_bpcs(PdfReader(str(src)).pages[0]) == {8, 1}
    fatal, _warn = owm._audit_output(out, 2, src_p=src, colour_pages={0})
    assert not fatal


def test_binarising_an_mrc_page_is_still_fatal(tmp_path):
    """The relaxation must not go one step further. Flattening an MRC page takes {8, 1} to
    {1}, which is real colour loss and must still fail -- which is why the test is COLLAPSE
    (`out == {1}` while `src != {1}`) and not `1 in bpcs(src)`."""
    src = U.make_mrc_pdf(tmp_path / 'mrc.pdf', npages=2)
    flat = U.make_acroform_array_contents_pdf(tmp_path / 'flat.pdf', npages=2,
                                              widget=False, nbookmarks=0)
    assert owm._page_image_bpcs(PdfReader(str(flat)).pages[0]) == {1}
    fatal, _warn = owm._audit_output(flat, 2, src_p=src, colour_pages={0})
    assert fatal and 'binarised' in fatal


# -- the graft survives a /Contents ARRAY -------------------------------------

def _add_ocr_layer(src: Path, out: Path) -> Path:
    """Stand in for ocrmypdf's output: an /OCR-* Form XObject on every page."""
    shutil.copyfile(src, out)
    with pikepdf.open(str(out), allow_overwriting_input=True) as pdf:
        for k, page in enumerate(pdf.pages):
            st = pikepdf.Stream(pdf, b'BT 3 Tr /F1 10 Tf 40 700 Td (grafted text)Tj ET')
            st.Type, st.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
            st.BBox = pikepdf.Array([0, 0, 612, 792])
            st.Resources = page.Resources
            page.Resources.XObject[pikepdf.Name('/OCR-' + str(k))] = pdf.make_indirect(st)
        pdf.save()
    return out


def test_graft_survives_array_contents_and_keeps_navigation(tmp_path):
    """`read_bytes()` on a /Contents ARRAY raised, the graft aborted, and the caller then
    shipped a rebuild carrying no bookmarks at all (measured 25 -> 0)."""
    src = U.make_acroform_array_contents_pdf(tmp_path / 'src.pdf', nbookmarks=4)
    donor = _add_ocr_layer(src, tmp_path / 'donor.pdf')
    shipped = tmp_path / 'shipped.pdf'
    shutil.copyfile(src, shipped)

    with pikepdf.open(str(src)) as check:
        assert isinstance(check.pages[0].obj.get('/Contents'), pikepdf.Array)
    n = len(PdfReader(str(src)).pages)
    assert owm._graft_into_source(src, shipped, donor, {k: k for k in range(n)})

    before, after = PdfReader(str(src)), PdfReader(str(shipped))
    assert len(after.pages) == n
    assert len(after.outline) == len(before.outline) == 4
    # the OCR layer is both PRESENT and actually PAINTED - a resource nothing draws is
    # invisible to a reader, which would make the text layer silently useless
    with pikepdf.open(str(shipped)) as got:
        page = got.pages[0]
        assert any(str(nm).startswith('/OCR') for nm in page.obj.Resources.XObject.keys())
        assert b'/OCR-0 Do' in _page_ops(page)
    # and the original 1-bit raster is untouched: this path exists to preserve the images
    assert owm._page_image_bpcs(after.pages[0]) == owm._page_image_bpcs(before.pages[0])


def test_graft_still_works_on_a_single_contents_stream(tmp_path):
    """The previously-working shape must keep working - `contents_add` handles both."""
    src = U.make_scan_pdf(tmp_path / 'scan.pdf', npages=2)
    with pikepdf.open(str(src)) as check:
        assert not isinstance(check.pages[0].obj.get('/Contents'), pikepdf.Array)
    donor = _add_ocr_layer(src, tmp_path / 'donor.pdf')
    shipped = tmp_path / 'shipped.pdf'
    shutil.copyfile(src, shipped)
    assert owm._graft_into_source(src, shipped, donor, {0: 0, 1: 1})
    with pikepdf.open(str(shipped)) as got:
        assert b'/OCR-0 Do' in _page_ops(got.pages[0])


# -- OSD script voting -------------------------------------------------------

def _fake_osd(monkeypatch, votes: dict):
    """Drive `_detect_language` with canned Tesseract OSD verdicts, {page: (script, conf)}."""
    monkeypatch.setattr(owm, 'GS', 'gs')
    monkeypatch.setattr(owm, 'TESS', 'tess')
    monkeypatch.setattr(owm, '_available_ocr_lang', lambda lang: lang)

    def run(cmd, *a, **kw):
        cmd = [str(x) for x in cmd]
        outs = [x for x in cmd if x.startswith('-sOutputFile=')]
        if outs:                                    # the Ghostscript render
            Path(outs[0].split('=', 1)[1]).write_bytes(b'png-stand-in')
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')
        page = int(''.join(c for c in Path(cmd[1]).stem if c.isdigit()))
        script, conf = votes[page]
        return types.SimpleNamespace(returncode=0, stderr='', stdout=(
            'Script: ' + script + chr(10) + 'Script confidence: ' + str(conf) + chr(10)))
    monkeypatch.setattr(owm.subprocess, 'run', run)


def test_marginal_nonlatin_does_not_outvote_the_summed_latin_evidence(tmp_path,
                                                                      monkeypatch):
    """The measured mis-vote: two Latin pages under the floor were DISCARDED, so a single
    Katakana page at 3.21 elected jpn+eng for an English manual."""
    pdf = U.make_acroform_array_contents_pdf(tmp_path / 'p.pdf', npages=4, nbookmarks=0)
    _fake_osd(monkeypatch, {1: ('Latin', 2.19), 2: ('Latin', 1.54),
                            3: ('Katakana', 3.21), 4: ('Cyrillic', 0.70)})
    assert owm._detect_language(pdf, tmp_path, 0) == 'eng'


def test_a_confidently_nonlatin_document_still_wins(tmp_path, monkeypatch):
    """A genuine Cyrillic manual scores ~15-20 on dense pages and must still be detected -
    the counter-evidence rule must not quietly make the tool English-only."""
    pdf = U.make_acroform_array_contents_pdf(tmp_path / 'p.pdf', npages=4, nbookmarks=0)
    _fake_osd(monkeypatch, {1: ('Cyrillic', 17.0), 2: ('Cyrillic', 15.5),
                            3: ('Latin', 2.0), 4: ('Cyrillic', 16.2)})
    assert owm._detect_language(pdf, tmp_path, 0) == 'rus+eng'


def test_subfloor_noise_alone_elects_nothing(tmp_path, monkeypatch):
    """Sub-floor votes are counter-evidence only: they can defeat a marginal non-Latin
    winner, but must never elect a language themselves."""
    pdf = U.make_acroform_array_contents_pdf(tmp_path / 'p.pdf', npages=4, nbookmarks=0)
    _fake_osd(monkeypatch, {1: ('Cyrillic', 0.6), 2: ('Cyrillic', 1.3),
                            3: ('Cyrillic', 0.9), 4: ('Cyrillic', 1.1)})
    assert owm._detect_language(pdf, tmp_path, 0) == 'eng'
