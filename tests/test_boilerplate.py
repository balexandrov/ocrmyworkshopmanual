"""Tests for the stamped-boilerplate discount — why a watermarked scan gets OCR'd.

The bug these guard, measured on a real file: `Supplement_A.pdf` (Mitsubishi Carisma
1996-2000 supplement, 21,273,852 bytes, 256 pages) carries exactly 66 chars on every page and
nothing else — `www.WorkshopManuals.co.uk` / `Purchased from www.WorkshopManuals.co.uk`. That
is 66 >= `has_text`'s min_chars of 40 on all 8 sampled pages, so `has_text` said the file was
already searchable, `_ship_original` returned OCR_KEPT, and ocrmypdf was never run.
`looks_born_digital` got the SAME file right (66 < its own min_chars of 100 -> scan_frac 1.0)
and lost the argument, because `_ship_original` does not ask it.

The cost is the point: that file's images are already CCITT G4 at 595.7 dpi, so the sample
projects 103% of original and it cannot be compressed at all. A text layer was the only thing
the tool had to offer it, and that is exactly what it silently withheld.
"""
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader

import _util as U

owm = U.owm
_missing = U.tools_missing()
_ocr_missing = U.ocr_missing()


# ── the discriminator ─────────────────────────────────────────────────────────

def test_a_repeated_line_is_boilerplate_and_a_varying_one_is_not():
    """A text layer says something different on every page. A stamp says the same thing.
    That is the whole rule, and it needs no watermark-specific knowledge."""
    stamp = {i: 'www.WorkshopManuals.co.uk\nPurchased from www.WorkshopManuals.co.uk'
             for i in range(6)}
    assert owm._boilerplate_lines(stamp) == frozenset(
        {'www.workshopmanuals.co.uk', 'purchased from www.workshopmanuals.co.uk'})

    real = {i: f'Section {i} removal and installation of the number {i} component'
            for i in range(6)}
    assert owm._boilerplate_lines(real) == frozenset()


def test_case_and_spacing_do_not_defeat_the_repeat_test():
    """A stamping tool draws the same words at different sizes from different Tm, and
    extraction spacing follows. Measured: the two real lines are 25pt and 12pt."""
    texts = {0: 'Purchased  from   WWW.Example.co.uk',
             1: 'purchased from www.example.co.uk',
             2: '  Purchased From www.Example.CO.UK  '}
    assert owm._boilerplate_lines(texts) == frozenset({'purchased from www.example.co.uk'})


def test_two_pages_are_not_enough_to_call_something_boilerplate():
    """`_BOILER_MIN_PAGES`. Two pages that say the same thing is a coincidence, not a
    repeat — and treating it as one would discount a two-page document's only text."""
    same = {0: 'IDENTICAL LINE OF TEXT', 1: 'IDENTICAL LINE OF TEXT'}
    assert owm._boilerplate_lines(same) == frozenset()
    same[2] = 'IDENTICAL LINE OF TEXT'
    assert owm._boilerplate_lines(same) == frozenset({'identical line of text'})


def test_one_blank_page_does_not_hide_a_stamp():
    """`_BOILER_COVERED` is 0.9, not 1.0: a blank or unreadable page in the sample must not
    be able to veto the whole detection. A page with no text at all is not counted against
    the coverage, since it says nothing either way."""
    texts = {i: 'Purchased from www.example.co.uk' for i in range(6)}
    texts[3] = ''
    assert owm._boilerplate_lines(texts) == frozenset({'purchased from www.example.co.uk'})


def test_a_page_of_identical_lines_is_a_template_not_a_stamp():
    """`_BOILER_MAX_LINES` / `_BOILER_MAX_CHARS`. A stamp is a line or two (measured: 2 lines,
    66 chars). Twenty identical lines is a form template, and a template is the document's
    content — discounting it would call a real form unsearchable."""
    form = {i: '\n'.join(f'Field {j} .....................' for j in range(20))
            for i in range(6)}
    assert owm._boilerplate_lines(form) == frozenset()


def test_boilerplate_is_removed_line_wise_not_wholesale():
    """A page sharing ONE line with a stamp keeps everything else. This is what protects a
    running header, whose page has a text layer even though its header repeats."""
    boiler = frozenset({'confidential - do not copy'})
    txt = 'CONFIDENTIAL - DO NOT COPY\nTorque the bolt to 45 Nm.\nInspect the seal.'
    assert owm._minus_boilerplate(txt, boiler) == \
        'Torque the bolt to 45 Nm.\nInspect the seal.'
    assert owm._minus_boilerplate(txt, frozenset()) == txt


# ── the regression: a stamped scan is not a searchable file ───────────────────

def test_a_stamped_scan_is_not_read_as_a_text_layer(tmp_path):
    """THE BUG. Before the discount, 66 chars of watermark on every page made `has_text`
    return True, so the 256-page Carisma supplement shipped with no text layer at all."""
    f = U.make_stamped_scan_pdf(tmp_path / 'stamped.pdf', npages=6, dpi=150)
    ts = owm._text_sample(f)
    assert sorted(ts.boiler) == ['purchased from www.workshopmanuals.co.uk',
                                 'www.workshopmanuals.co.uk']
    assert all(len(ts.text[i]) == 66 for i in ts.idxs), 'the stamp is the only text'
    assert all(ts.net[i] == 0 for i in ts.idxs), 'and none of it is a text layer'

    assert owm.has_text(f) is False, 'was True — this is the reported bug'
    assert owm.has_any_text(f) is False, 'nor is there a stale layer to redo'
    assert owm.looks_born_digital(f)[0] is False, 'it is still a scan'


def test_the_dry_run_predicts_new_ocr_for_a_stamped_scan(tmp_path):
    """`--dry-run` must predict what the real run does, or the preview is worthless. It
    predicted `kept existing` for the measured file, matching the real run's own mistake."""
    f = U.make_stamped_scan_pdf(tmp_path / 'stamped.pdf', npages=6, dpi=150)
    state, _lang = owm._predict_ocr(f, tmp_path, ocr=True, language='eng')
    assert state == owm.OCR_NEW


def test_the_visible_text_test_ignores_a_stamp_but_not_real_type(tmp_path):
    """`_visible_text_chars` is a visibility GATE followed by a RAW count, so one visible
    stamp word used to make an entire page's text count as visible publisher type."""
    f = U.make_stamped_scan_pdf(tmp_path / 'stamped.pdf', npages=6, dpi=150)
    page = PdfReader(str(f)).pages[0]
    assert owm._visible_text_chars(page, 20) == 66, 'the stamp IS visible — it is drawn'
    assert owm._visible_text_chars(page, 20, owm._text_sample(f).boiler) == 0


# ── what must NOT change ──────────────────────────────────────────────────────

def test_a_genuine_per_page_ocr_layer_still_counts_as_a_text_layer(tmp_path):
    """The thing this discount must not break: a real OCR layer says something different on
    every page, so nothing is boilerplate and the file is still `kept existing`."""
    f = U.make_ocr_layer_pdf(tmp_path / 'ocred.pdf', npages=4, dpi=150)
    ts = owm._text_sample(f)
    assert ts.boiler == frozenset()
    assert owm.has_text(f) is True
    assert owm.has_any_text(f) is True
    assert owm.looks_born_digital(f)[0] is False, 'an OCR layer over a scan is still a scan'
    state, _lang = owm._predict_ocr(f, tmp_path, ocr=True, language='eng')
    assert state == owm.OCR_KEPT


def test_real_type_beside_a_stamp_is_still_real_type(tmp_path):
    """The nasty case, and the reason the discount is line-wise. A full-page scan carrying
    BOTH a repeated stamp and per-page publisher text: the stamp line is boilerplate, but
    rasterising the page would still destroy type that OCR cannot reproduce."""
    f = U.make_stamped_body_over_scan_pdf(tmp_path / 'body.pdf', npages=6, dpi=150)
    ts = owm._text_sample(f)
    assert ts.boiler == frozenset({'confidential - do not copy'})
    assert all(ts.net[i] > 500 for i in ts.idxs), 'the body text survives the discount'
    assert owm.has_text(f) is True
    page = PdfReader(str(f)).pages[0]
    assert owm._visible_text_chars(page, 100, ts.boiler) > 500


def test_a_running_header_does_not_make_a_born_digital_file_unsearchable(tmp_path):
    """A born-digital manual with the same header on every page still has a text layer, and
    must not be re-OCR'd on the strength of that header repeating."""
    f = U.make_born_digital_pdf(tmp_path / 'bd.pdf', npages=6, lines_per_page=20,
                                header='CARISMA WORKSHOP MANUAL')
    assert owm.has_text(f) is True
    assert owm.looks_born_digital(f)[0] is True


# ── the worse failure the same bug causes one floor up ────────────────────────

def test_a_long_stamp_does_not_make_a_scanned_manual_look_born_digital(tmp_path):
    """Worse than skipping OCR: a stamp over 100 chars pushes `_visible_text_chars` past
    `looks_born_digital`'s floor, so every scanned page reads as a text page, scan_frac
    drops, and the file is copied through untouched — never compressed AND never OCR'd.

    Measured on this fixture: 129 chars of stamp, `_visible_text_chars(page, 100)` == 129
    without the discount and 0 with it, against a 150 dpi full-page raster."""
    stamp = ('Licensed to a single user only - redistribution prohibited by law and '
             'by the terms of purchase from www.example.co.uk order 12345')
    assert len(stamp) > 100, 'the fixture only tests anything above the 100-char floor'
    f = U.make_stamped_scan_pdf(tmp_path / 'long.pdf', npages=6, dpi=150, stamp=stamp)
    page = PdfReader(str(f)).pages[0]
    assert owm._visible_text_chars(page, 100) >= 100, 'this is the old behaviour'

    born, sig = owm.looks_born_digital(f)
    assert born is False, 'a scanned manual must not be skipped over a watermark'
    assert sig['scan_frac'] == 1.0 and sig['text_pages'] == 0
    assert owm.has_text(f) is False


def test_a_long_stamp_does_not_make_every_page_pass_through_unocred(tmp_path):
    """`classify_page` uses the same 100-char floor, and a PT_VECTOR page is passed through
    uncompressed AND dropped from the OCR render (`skip_pages`) — so a long stamp made every
    page silently un-OCR'd while the report row still claimed `new ocr`."""
    stamp = ('Licensed to a single user only - redistribution prohibited by law and '
             'by the terms of purchase from www.example.co.uk order 12345')
    f = U.make_stamped_scan_pdf(tmp_path / 'long.pdf', npages=3, dpi=150, stamp=stamp)
    page = PdfReader(str(f)).pages[0]
    boiler = owm._text_sample(f).boiler

    png = tmp_path / 'p1.png'
    assert U.render_page(f, 1, 150, png)
    assert owm.classify_page(png, 1, f, tmp_path, 150, True, 0.02, 150,
                             page=page).type == owm.PT_VECTOR, 'the old behaviour'
    assert owm.classify_page(png, 1, f, tmp_path, 150, True, 0.02, 150,
                             page=page, boiler=boiler).type != owm.PT_VECTOR


def test_the_discount_does_not_weaken_real_vector_protection(tmp_path):
    """The Subaru case, re-run WITH a boilerplate set in play: real visible type inside a
    Form XObject must still be seen, or the discount has traded one kind of damage for
    another (rasterising ~1000 chars/page of publisher type)."""
    f = U.make_form_wrapped_pdf(tmp_path / 'form.pdf', with_text=True)
    page = PdfReader(str(f)).pages[0]
    boiler = frozenset({'purchased from www.example.co.uk'})
    assert owm._visible_text_chars(page, 100) >= 100
    assert owm._visible_text_chars(page, 100, boiler) >= 100, \
        'a stamp elsewhere must not blind the tool to real type'


# ── the shared sample ─────────────────────────────────────────────────────────

def test_the_sample_is_cached_per_file_content(tmp_path):
    """One sample serves has_text, has_any_text and looks_born_digital, which used to open
    the file and extract the same 8 pages independently. Keyed on size+mtime as well as the
    path, so a file rewritten in place cannot be served a stale answer."""
    f = U.make_stamped_scan_pdf(tmp_path / 'stamped.pdf', npages=6, dpi=150)
    assert owm._text_sample(f) is owm._text_sample(f), 'same file -> cached object'

    g = U.make_ocr_layer_pdf(tmp_path / 'other.pdf', npages=4, dpi=150)
    before = owm._text_sample(g)
    assert before.boiler == frozenset()
    U.make_stamped_scan_pdf(g, npages=6, dpi=150)          # same path, new content
    assert owm._text_sample(g).boiler, 'a rewritten file must be re-read'


def test_an_unreadable_file_samples_to_nothing_and_claims_no_text(tmp_path):
    """Every gate must fail safe on a file it cannot read: no text claimed, so OCR is
    attempted rather than skipped, and the normal path gets to error-report it."""
    bad = tmp_path / 'bad.pdf'
    bad.write_bytes(b'%PDF-1.4\nnot really a pdf')
    ts = owm._text_sample(bad)
    assert ts.idxs == () and ts.err
    assert owm.has_text(bad) is False
    assert owm.looks_born_digital(bad)[0] is False


# ── the stamp is reported, not silently handled ───────────────────────────────

def test_the_stamp_is_named_in_the_scan_signals_column():
    """A row whose `ocr` cell changed from `kept existing` to `new ocr` has to say why.
    Reported as the stamp's SHAPE next to the raw char count, so `chars=528` and
    `boiler=2ln/65c` over 8 pages together tell the whole story."""
    sig = {'scan_frac': 1.0, 'sampled': 8, 'scan_pages': 8, 'text_pages': 0, 'chars': 528,
           'boiler': ['www.workshopmanuals.co.uk',
                      'purchased from www.workshopmanuals.co.uk']}
    text = owm._signals_text(sig)
    assert 'boiler=2ln/65c' in text, text
    assert 'scan_frac=1.0' in text and 'chars=528' in text


def test_a_file_with_no_stamp_leaves_the_signals_cell_alone():
    """Every existing row in a normal archive must be byte-for-byte what it is today."""
    sig = {'scan_frac': 0.0, 'sampled': 8, 'scan_pages': 0, 'text_pages': 8, 'chars': 9000}
    assert 'boiler' not in owm._signals_text(sig)


# ── end to end: the branch the real file takes ───────────────────────────────

@pytest.mark.skipif(_missing is not None, reason=str(_missing))
@pytest.mark.skipif(_ocr_missing is not None, reason=str(_ocr_missing))
def test_the_precheck_skip_path_still_gets_a_text_layer(tmp_path, monkeypatch):
    """THE TEST THAT PROVES THE BUG IS FIXED; the rest only prove the parts.

    Reproduces the measured branch: a file whose sample projects too high to be worth
    compressing goes down `_ship_original`, which is the only path that consults `has_text`.
    On the real 256-page file the projection was 103% (its images are already CCITT G4 at
    595.7 dpi), so a text layer was all it could be given — and `has_text` refused it."""
    monkeypatch.setattr(owm, 'PRECHECK_MIN_PAGES', 1)
    monkeypatch.setattr(owm, 'PRECHECK_SKIP_RATIO', 0.0)   # force the ship-original branch
    src = U.make_stamped_scan_pdf(tmp_path / 'stamped.pdf', npages=4, dpi=150)
    out = tmp_path / 'out.pdf'
    res = owm.compress_one(str(src), str(out), 150, ocr=True, language='eng')

    assert res['err'] is None, res
    assert res['action'] == 'kept_original' and res['reason'] == owm.REASON_ALREADY
    assert res['ocr_state'] == owm.OCR_NEW, 'was OCR_KEPT — the bug'
    assert 'boilerplate' in (res.get('note') or ''), res.get('note')
    assert out.is_file()
    r = PdfReader(str(out))
    assert len(r.pages) == 4, 'no page may be lost'
    assert owm.has_any_text(out), 'a real text layer must now be present'
