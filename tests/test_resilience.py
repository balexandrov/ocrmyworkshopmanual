"""Tests for the resilience / ease-of-use additions:
  - per-step timeout aborts a file gracefully (never hangs the batch)
  - output verification flags a wrong page count / unopenable output
  - dry-run preview_one predicts the action and writes nothing
  - config file, duplicate hashing, retry-CSV parsing, malformed-PDF repair
"""
import argparse
import shutil
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader

import _util as U

_missing = U.tools_missing()
_ocr_missing = U.ocr_missing()


def test_verify_output_matches_and_mismatches(tmp_path):
    pdf = U.make_born_digital_pdf(tmp_path / 'p.pdf', npages=3)
    assert U.owm._verify_output(pdf, 3) == ''            # correct page count -> OK
    assert 'WARN' in U.owm._verify_output(pdf, 5)        # wrong count -> warned
    assert 'expected 5' in U.owm._verify_output(pdf, 5)


def test_verify_output_unopenable(tmp_path):
    junk = tmp_path / 'junk.pdf'
    junk.write_bytes(b'not a pdf at all')
    assert 'WARN' in U.owm._verify_output(junk, 1)


def test_preview_one_born_digital_writes_nothing(tmp_path):
    src = U.make_born_digital_pdf(tmp_path / 'src.pdf', npages=3)
    before = set(tmp_path.iterdir())
    res = U.owm.preview_one(str(src), 200, True, 10, False, 0.02, 150, 60,
                            0.25, 0.30, 0.6)
    assert res['action'] == 'born_digital' and res['err'] is None
    assert res['new'] == res['orig']                      # predicts no size change
    assert set(tmp_path.iterdir()) == before              # nothing written


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_preview_one_scanned_projects_smaller(tmp_path):
    pdfs = U.fixture_pdfs('line')
    if not pdfs:
        pytest.skip('no line fixtures')
    res = U.owm.preview_one(str(pdfs[0]), 200, True, 10, False, 0.02, 150, 60,
                            0.25, 0.30, 0.6)
    assert res['err'] is None
    assert res['action'] in ('compressed', 'kept_original', 'ocr_only')
    assert res['new'] <= res['orig']


def test_stall_watchdog_spares_slow_but_working_process(tmp_path):
    """A SLOW but progressing process must NOT be killed. A flat wall-clock budget kills
    healthy work purely for being big (a 6,855-page manual was failed at 2h while OCR'ing
    correctly); progress-based detection is size-independent."""
    import subprocess
    prog = tmp_path / 'out.txt'
    code = (f"import time\nf=open(r'{prog}','w')\n"
            "for i in range(6):\n    f.write('x'*500); f.flush(); time.sleep(0.5)\nf.close()\n")
    r = U.owm._run_stalled([sys.executable, '-c', code],
                           lambda: prog.stat().st_size if prog.exists() else 0,
                           2, poll=0.25)                 # 3s of work, 2s stall limit
    assert r.returncode == 0, 'a slow-but-progressing process was killed'


def test_stall_watchdog_kills_hung_process(tmp_path):
    """A process making NO progress is killed once the stall window passes."""
    import subprocess
    never = tmp_path / 'never.txt'
    with pytest.raises(subprocess.TimeoutExpired):
        U.owm._run_stalled([sys.executable, '-c', 'import time; time.sleep(30)'],
                           lambda: never.stat().st_size if never.exists() else 0,
                           2, poll=0.25)


def test_retry_recovers_crash_but_never_retries_a_stall():
    """Transient crashes (non-zero rc) are retried; a stall is not — a hung or genuinely
    slow step behaves the same way next time, so retrying only burns the time again."""
    import subprocess
    calls = [0]

    def flaky():
        calls[0] += 1
        return subprocess.run([sys.executable, '-c',
                               f'import sys; sys.exit(0 if {calls[0]} >= 3 else 1)'])
    r, tries = U.owm._run_retry(flaky, attempts=3, backoff=0.05)
    assert r.returncode == 0 and tries == 3

    stalls = [0]

    def stalling():
        stalls[0] += 1
        raise subprocess.TimeoutExpired('cmd', 1)
    with pytest.raises(subprocess.TimeoutExpired):
        U.owm._run_retry(stalling, attempts=3, backoff=0.05)
    assert stalls[0] == 1, 'a stall must never be retried'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_stalled_file_fails_gracefully(tmp_path):
    """A step that makes no progress within the stall window aborts as a clean FAILED
    result (no hang, no crash, no output file), so one pathological file never wedges a
    big batch. Simulated by making the progress probe report no progress at all."""
    import subprocess
    pdfs = U.fixture_pdfs('photo_gray') or U.fixture_pdfs('line')
    if not pdfs:
        pytest.skip('no fixtures')
    real = U.owm._run_stalled

    def frozen(cmd, progress, stall, **kw):              # simulate a hung external tool
        raise subprocess.TimeoutExpired(cmd, stall)
    U.owm._run_stalled = frozen
    try:
        dest = tmp_path / 'out' / 'x.pdf'
        res = U.owm.compress_one(str(pdfs[0]), str(dest), 200, ocr=False, timeout=1)
    finally:
        U.owm._run_stalled = real
    assert res.get('err') and 'stalled' in res['err'], res
    assert not dest.exists(), 'a stalled file must not leave a dest output'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_bright_colour_page_not_classified_blank(tmp_path):
    """Regression: a bright-colour page (orange, no dark pixels -> grayscale
    luminance all >= 100) must NOT be called BLANK (which would destroy it as
    bitonal). The blank test requires low photo-coverage, so it routes to colour."""
    pdf = U.make_color_pdf(tmp_path / 'orange.pdf')
    work = U.workdir()
    try:
        t, sig = U.classify(pdf, 1, 200, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    assert t != U.owm.PT_BLANK, f'bright colour page wrongly classified blank: {sig}'
    assert t == U.owm.PT_PHOTO_COLOR, f'expected photo_color, got {t}: {sig}'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_colour_line_art_routes_to_color_line(tmp_path):
    """Regression: a flat-COLOUR line-art page (colour wiring diagram — coloured wires +
    black text, continuous-tone coverage ~0) must route to PT_COLOR_LINE, NOT the
    bitonal path. The colour test used to be gated behind photo-coverage, so low-
    coverage colour line art fell through to PT_LINE and was binarized to b&w,
    destroying the wire colours (the real DODGE NEON diagram this reproduces)."""
    pdf = U.make_color_line_pdf(tmp_path / 'wires.pdf')
    work = U.workdir()
    try:
        t, sig = U.classify(pdf, 1, 200, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    assert t == U.owm.PT_COLOR_LINE, f'colour line art misrouted to {t!r}: {sig}'
    assert t not in U.owm._PT_BITONAL, 'colour line art must never take the bitonal path'
    assert sig['photo_cov'] <= 0.02, f'should be the LOW-coverage path, cov={sig["photo_cov"]}'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_colour_survives_compression_round_trip(tmp_path):
    """End-to-end: compressing a colour line-art page keeps its colour (lossless source-
    page pass-through), where the old bitonal path flattened it to 1-bit b&w."""
    import subprocess

    import numpy as np
    from PIL import Image
    pdf = U.make_color_line_pdf(tmp_path / 'wires.pdf')
    out = tmp_path / 'out.pdf'
    res = U.owm.compress_one(str(pdf), str(out), 200, ocr=False)
    assert res.get('err') is None, res
    assert out.exists(), 'no output produced'
    assert len(PdfReader(str(out)).pages) == 1
    cp = tmp_path / 'c.png'
    subprocess.run([U.owm.GS, '-sDEVICE=png16m', '-r150', '-dFirstPage=1', '-dLastPage=1',
                    '-dNOPAUSE', '-dBATCH', '-dQUIET', '-sOutputFile=' + str(cp),
                    U.owm.win_long(out)], capture_output=True)
    a = np.asarray(Image.open(cp).convert('RGB')).astype(np.int16)
    assert U.owm._is_color(a), 'colour was lost in the round trip (page was binarized to b&w)'


def test_is_color_detects_small_amount_of_saturated_ink():
    """Regression: colour judged by SHARE of ink alone missed a diagram that is mostly a
    black text table with a few vivid wires (a real Chrysler diagram: 5.5% coloured ink
    vs a 6% bar -> binarized, wire colours destroyed). A small but strongly saturated
    amount of ink now counts as colour, while neutral/sepia ink still does not."""
    import numpy as np
    h = w = 400
    a = np.full((h, w, 3), 255, np.int16)
    a[:, :, :][20:380, 20:380] = 255
    a[100:300, 100:300] = 40                       # a big black block (dominant ink)
    assert U.owm._is_color(a) is False, 'plain black-on-white must not be colour'
    a2 = a.copy()
    a2[150:155, 100:300] = (220, 20, 20)           # a few vivid red "wires" (small share)
    a2[170:174, 100:300] = (20, 40, 220)           # blue
    frac = (9 * 200) / float((a2.min(2) < 200).sum())
    assert frac < 0.06, f'test setup: coloured share {frac:.3f} must be under the old bar'
    assert U.owm._is_color(a2) is True, 'small amount of saturated colour must count'
    a3 = a.copy()                                  # sepia/yellowed cast must stay non-colour
    a3 = (a3 * np.array([1.0, 0.94, 0.82])).astype(np.int16)
    assert U.owm._is_color(a3) is False, 'a uniform sepia cast must not read as colour'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_vector_page_passed_through_in_mixed_pdf(tmp_path):
    """A born-digital VECTOR page (TOC/nav: vector text + colour + links, no full-page
    raster) inside an otherwise-scanned PDF must be passed through losslessly, not
    rasterized to b&w. Regression for the mixed-PDF case (the 4Runner owner's manual:
    page 1 lost its blue links + colour when the whole file was rasterized)."""
    from pypdf import PdfReader, PdfWriter
    vec = U.make_born_digital_pdf(tmp_path / 'vec.pdf', npages=1)
    scans = U.make_scan_pdf(tmp_path / 'scans.pdf', npages=5, dpi=200)
    wr = PdfWriter(); wr.append(str(vec)); wr.append(str(scans))
    mixed = tmp_path / 'mixed.pdf'
    with open(mixed, 'wb') as f:
        wr.write(f)
    out = tmp_path / 'out.pdf'
    res = U.owm.compress_one(str(mixed), str(out), 200, ocr=False)
    assert res.get('err') is None and res.get('action') == 'compressed', res
    r = PdfReader(str(out))
    assert len(r.pages) == 6
    # page 0 (vector) keeps its vector text and has NO raster image (not rasterized)
    assert len((r.pages[0].extract_text() or '').strip()) > 50, 'vector page lost its text'
    res0 = r.pages[0].get('/Resources')
    xo0 = res0.get_object().get('/XObject') if res0 else None
    assert not xo0, 'vector page was rasterized (has an image xobject)'
    # a scan page IS compressed to JBIG2
    assert b'/JBIG2Decode' in out.read_bytes(), 'scan pages were not JBIG2-compressed'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_hires_scan_rendered_at_native_dpi(tmp_path):
    """A 300-dpi scan must NOT be downsampled to the fixed 200-dpi render (a visible
    quality loss). The output image should keep ~native resolution."""
    from pypdf import PdfReader
    scans = U.make_scan_pdf(tmp_path / 'hires.pdf', npages=3, dpi=300)
    out = tmp_path / 'out.pdf'
    res = U.owm.compress_one(str(scans), str(out), 200, ocr=False)
    assert res.get('err') is None, res
    r = PdfReader(str(out))
    xo = r.pages[0]['/Resources']['/XObject'].get_object()
    img = next(o.get_object() for o in xo.values() if o.get_object().get('/Subtype') == '/Image')
    w = int(img['/Width'])
    assert w >= 2400, f'scan downsampled: width {w}px (expected ~2550 at native 300dpi, not 1700 at 200)'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_bookmarks_preserved_through_compression(tmp_path):
    """Bookmarks (document outline) must survive the rasterize-and-rebuild, remapped 1:1
    by page. Rebuilding from scratch used to drop them (the 4Runner lost 23 bookmarks)."""
    from pypdf import PdfReader, PdfWriter
    scans = U.make_scan_pdf(tmp_path / 'scans.pdf', npages=4, dpi=200)
    wr = PdfWriter(clone_from=str(scans))
    wr.add_outline_item('Chapter 1', 0)
    wr.add_outline_item('Chapter 2', 2)
    src = tmp_path / 'bm.pdf'
    with open(src, 'wb') as f:
        wr.write(f)
    out = tmp_path / 'out.pdf'
    res = U.owm.compress_one(str(src), str(out), 200, ocr=False)
    assert res.get('err') is None and res.get('action') == 'compressed', res

    def cnt(items):
        c = 0
        for it in items:
            c += cnt(it) if isinstance(it, list) else 1
        return c
    assert cnt(PdfReader(str(out)).outline) == 2, 'bookmarks were lost through compression'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
@pytest.mark.skipif(_ocr_missing is not None, reason=str(_ocr_missing))
def test_mixed_pdf_scan_pages_still_get_ocr(tmp_path):
    """Regression: has_text() summed characters over the first few pages, so ONE
    text-rich vector TOC page made a mixed manual look 'already has text' and OCR was
    skipped for every SCANNED page — leaving the actual content unsearchable. It is now
    judged per page across the whole file, so the scan pages get their text layer."""
    from pypdf import PdfReader, PdfWriter
    vec = U.make_born_digital_pdf(tmp_path / 'vec.pdf', npages=1)
    scans = U.make_scan_pdf(tmp_path / 'sc.pdf', npages=4, dpi=200)
    wr = PdfWriter(); wr.append(str(vec)); wr.append(str(scans))
    mixed = tmp_path / 'mixed.pdf'
    with open(mixed, 'wb') as f:
        wr.write(f)
    assert U.owm.has_text(mixed) is False, 'mixed file wrongly reported as fully texted'
    assert U.owm.has_text(vec) is True, 'an all-vector file should still skip OCR'
    assert U.owm.has_text(scans) is False, 'a pure scan has no text'
    out = tmp_path / 'out.pdf'
    res = U.owm.compress_one(str(mixed), str(out), 200, ocr=True, language='eng')
    assert res.get('err') is None, res
    r = PdfReader(str(out))
    for pi in range(1, len(r.pages)):          # every SCAN page must now carry OCR text
        assert len((r.pages[pi].extract_text() or '').strip()) > 100, (
            f'scan page {pi} got no OCR text layer')


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_page_loss_fails_instead_of_shipping_a_truncated_file(tmp_path):
    """Regression (silent data loss): the output used to be verified against the RENDERED
    page count, so when a corrupt PDF rendered (or repaired) to fewer pages the check
    passed and a 21-page manual was replaced by a 1-page file. Verification is now
    against the SOURCE page count, so page loss fails the file and keeps the original.
    Simulated by dropping a rendered page, exactly as a partial render would."""
    src = U.make_scan_pdf(tmp_path / 'many.pdf', npages=5, dpi=150)
    real = U.owm._run_stalled

    def lose_a_page(cmd, progress, stall, **kw):
        r = real(cmd, progress, stall, **kw)
        out = next((a for a in cmd if str(a).startswith('-sOutputFile=')), '')
        work = Path(out.split('=', 1)[1]).parent if out else None
        if work:
            pngs = sorted(work.glob('p*.png'))
            if len(pngs) > 1:
                pngs[-1].unlink(missing_ok=True)     # a page vanishes from the render
        return r
    U.owm._run_stalled = lose_a_page
    try:
        dest = tmp_path / 'out.pdf'
        res = U.owm.compress_one(str(src), str(dest), 150, ocr=False)
    finally:
        U.owm._run_stalled = real
    assert res.get('err') and 'page loss' in res['err'], res
    assert not dest.exists(), 'a page-losing run must not write an output'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_links_and_bookmarks_survive_compression(tmp_path):
    """Rebuilding a PDF from rendered pages drops link annotations, bookmarks and named
    destinations (measured: 5 of 249 links kept, 248 bookmarks lost). The compressed
    pages are now grafted back into the ORIGINAL document, so all of it is inherited."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject
    src0 = U.make_scan_pdf(tmp_path / 'src.pdf', npages=4, dpi=200)
    wr = PdfWriter(clone_from=str(src0))
    for i, pg in enumerate(wr.pages):            # a URI link on every page
        ann = DictionaryObject({
            NameObject('/Type'): NameObject('/Annot'),
            NameObject('/Subtype'): NameObject('/Link'),
            NameObject('/Rect'): ArrayObject([NumberObject(x) for x in (10, 10, 100, 30)]),
            NameObject('/Border'): ArrayObject([NumberObject(0)] * 3),
            NameObject('/A'): DictionaryObject({
                NameObject('/S'): NameObject('/URI'),
                NameObject('/URI'): TextStringObject(f'https://example.invalid/{i}')}),
        })
        pg[NameObject('/Annots')] = ArrayObject([wr._add_object(ann)])
    wr.add_outline_item('Chapter 1', 0)
    wr.add_outline_item('Chapter 2', 2)
    src = tmp_path / 'withmeta.pdf'
    with open(src, 'wb') as f:
        wr.write(f)

    out = tmp_path / 'out.pdf'
    res = U.owm.compress_one(str(src), str(out), 200, ocr=False)
    assert res.get('err') is None and res.get('action') == 'compressed', res
    r = PdfReader(str(out))
    links = 0
    for p in r.pages:
        a = p.get('/Annots')
        if a:
            links += sum(1 for x in a.get_object()
                         if x.get_object().get('/Subtype') == '/Link')

    def cnt(items):
        n = 0
        for it in items:
            n += cnt(it) if isinstance(it, list) else 1
        return n
    assert len(r.pages) == 4
    assert links == 4, f'link annotations lost: {links}/4'
    assert cnt(r.outline) == 2, 'bookmarks lost'
    assert out.stat().st_size < src.stat().st_size, 'should still compress'


def test_available_ocr_lang_degrades_to_installed(monkeypatch):
    """A detected/requested language whose pack is NOT installed must degrade to an
    installed one (never fail OCR and drop the whole text layer — the rus-missing bug)."""
    monkeypatch.setattr(U.owm, '_INSTALLED_LANGS', {'eng', 'deu', 'rus'})
    assert U.owm._available_ocr_lang('rus+eng') == 'rus+eng'   # both installed -> unchanged
    assert U.owm._available_ocr_lang('kor+eng') == 'eng'       # kor absent -> keep eng
    assert U.owm._available_ocr_lang('ara') == 'eng'           # none installed -> eng fallback
    assert U.owm._available_ocr_lang('fra+deu') == 'deu'       # keep only the installed pack
    monkeypatch.setattr(U.owm, '_INSTALLED_LANGS', {'spa', 'ita'})
    assert U.owm._available_ocr_lang('kor') == 'ita'           # no eng -> first installed (sorted)


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
@pytest.mark.skipif(_ocr_missing is not None, reason=str(_ocr_missing))
def test_sparse_english_not_mislabelled_cyrillic(tmp_path):
    """Regression: sparse English pages (wiring diagrams) make Tesseract OSD emit a
    low-confidence, often spurious 'Cyrillic', which used to yield rus+eng (slow and
    lower-quality OCR). The per-page confidence floor must keep them 'eng'. Exercised on
    the real DODGE NEON diagram — the exact page that misdetected as rus+eng."""
    fx = U.fixture_pdfs('color_line')
    if not fx:
        pytest.skip('no color_line fixture')
    work = U.workdir()
    try:
        lang = U.owm._detect_language(fx[0], work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    assert 'rus' not in lang, f'sparse English mislabelled as {lang!r}'
    assert lang == 'eng', f'expected eng, got {lang!r}'


# ── config file / dedup / retry / repair ─────────────────────────────────────

@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_multipage_bitonal_via_stdin_preserves_pages(tmp_path):
    """A multi-page bitonal scan is JBIG2-wrapped by feeding the page list to the wrapper
    over STDIN (`-s -`) rather than as argv — one call, any length, so a multi-thousand-
    page manual can't overflow the OS command-line limit (the WinError-206 bug). Verify a
    real multi-page scan round-trips with every page intact and compresses."""
    from pypdf import PdfReader, PdfWriter
    line = U.fixture_pdfs('line')
    if not line:
        pytest.skip('no line fixtures')
    src = tmp_path / 'multi.pdf'                 # a 5-page scan (a real scanned page x5)
    w = PdfWriter()
    for _ in range(5):
        w.append(str(line[0]))
    with open(src, 'wb') as f:
        w.write(f)
    n_in = len(PdfReader(str(src)).pages)
    res = U.owm.compress_one(str(src), str(src), 200, ocr=False, in_place=True)
    assert res.get('err') is None, res
    assert len(PdfReader(str(src)).pages) == n_in, 'stdin-wrapped merge lost/added pages'


def test_wrapper_reads_page_list_from_stdin(tmp_path):
    """Direct check of the wrapper's `-s -` stdin mode: given a couple of tiny fake
    'page' files via stdin, it emits a %PDF with one page each (no jbig2 binary needed —
    the wrapper only reads the files' JBIG2 header bytes for width/height). The wrapper's
    PDF output is BINARY (latin1), so capture it as bytes — decoding as text would choke
    on high bytes under a UTF-8 locale."""
    import struct, subprocess, sys
    # minimal jbig2 generic-region page: bytes[11:27] = width,height,xres,yres (big-endian)
    def fake_page(p, w, h):
        p.write_bytes(b'\x00' * 11 + struct.pack('>IIII', w, h, 200, 200) + b'\x00' * 8)
    fake_page(tmp_path / 'a.jb2', 100, 120)
    fake_page(tmp_path / 'b.jb2', 100, 120)
    r = subprocess.run([sys.executable, str(U.REPO_ROOT / 'tools' / 'jbig2topdf.py'), '-s', '-'],
                       input=b'a.jb2\nb.jb2\n', capture_output=True, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr[-300:]
    assert r.stdout.startswith(b'%PDF'), r.stdout[:50]
    assert r.stdout.count(b'/MediaBox') == 2, f'expected 2 pages, got {r.stdout.count(b"/MediaBox")}'


def test_say_survives_broken_stdout(monkeypatch, capsys):
    """_say must never raise when stdout is broken (the closed-pipe case that a
    `| head` reader triggers) — a dropped progress line can't be allowed to abort
    a long run."""
    import io

    class BrokenOut(io.StringIO):
        def write(self, *a, **k):
            raise BrokenPipeError(32, 'broken pipe')
    monkeypatch.setattr(sys, 'stdout', BrokenOut())
    U.owm._say('this write would raise')   # must NOT propagate


def test_sweep_stale_scratch_removes_old_keeps_fresh(tmp_path, monkeypatch):
    """Startup sweep removes render-scratch left by killed runs (mtime older than the
    cutoff) but never touches a fresh dir (an actively-rendering concurrent run)."""
    import os
    monkeypatch.setattr(U.owm.tempfile, 'gettempdir', lambda: str(tmp_path))
    old = tmp_path / 'jb_old'; old.mkdir(); (old / 'p.png').write_bytes(b'x')
    fresh = tmp_path / 'jb_fresh'; fresh.mkdir(); (fresh / 'p.png').write_bytes(b'x')
    prev = tmp_path / 'jbprev_old'; prev.mkdir()
    old_t = __import__('time').time() - 8 * 3600         # 8h old
    os.utime(old, (old_t, old_t)); os.utime(prev, (old_t, old_t))
    U.owm._sweep_stale_scratch(max_age_h=6.0)
    assert not old.exists() and not prev.exists(), 'stale scratch not swept'
    assert fresh.exists(), 'fresh (active) scratch wrongly swept'


def test_file_hash_identical_and_different(tmp_path):
    a = U.make_born_digital_pdf(tmp_path / 'a.pdf', npages=2)
    b = tmp_path / 'b.pdf'; b.write_bytes(a.read_bytes())      # exact copy
    c = U.make_born_digital_pdf(tmp_path / 'c.pdf', npages=3)  # different
    assert U.owm._file_hash(a) == U.owm._file_hash(b)
    assert U.owm._file_hash(a) != U.owm._file_hash(c)


def test_flag_duplicates_annotates_but_keeps_all():
    """Duplicates are FLAGGED, never skipped — every result stays, twins get a note
    (they may legitimately belong to different manuals)."""
    results = [
        {'rel': 'a.pdf', 'hash': 'H1', 'note': ''},
        {'rel': 'm/b.pdf', 'hash': 'H1', 'note': ' [1 photo]'},
        {'rel': 'c.pdf', 'hash': 'H2', 'note': ''},
        {'rel': 'd.pdf'},   # no hash (duplicate check was off / unreadable)
    ]
    sets = U.owm._flag_duplicates(results)
    assert sets == 1                              # one duplicate group (H1)
    assert len(results) == 4                      # nothing removed
    assert 'DUPLICATE' in results[0]['note'] and 'm/b.pdf' in results[0]['note']
    assert 'DUPLICATE' in results[1]['note'] and 'a.pdf' in results[1]['note']
    assert results[1]['note'].startswith(' [1 photo]')   # original note preserved
    assert results[0]['duplicate_of'] == 'm/b.pdf'
    assert 'duplicate_of' not in results[2]       # unique file untouched


def test_read_failed_rels(tmp_path):
    csv = tmp_path / 'r.csv'
    csv.write_text('file,action,orig_bytes,new_bytes,pct_of_orig,scan_frac,note,error\n'
                   'ok.pdf,compressed,10,1,10,,,\n'
                   'bad.pdf,FAILED,10,0,,,,timed out\n'
                   'sub/also_bad.pdf,FAILED,10,0,,,,render failed\n', encoding='utf-8')
    assert U.owm._read_failed_rels(csv) == ['bad.pdf', 'sub/also_bad.pdf']


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason='TOML config support needs Python 3.11+ (stdlib tomllib)')
def test_config_defaults_applied(tmp_path, monkeypatch):
    cfg = tmp_path / 'c.toml'
    cfg.write_text('dpi = 321\nno_ocr = true\nsauvola_k = 0.22\n', encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['prog', '--config', str(cfg)])
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', type=Path)
    ap.add_argument('--dpi', type=int, default=200)
    ap.add_argument('--sauvola-k', type=float, default=0.30)
    ap.add_argument('--no-ocr', action='store_true')
    U.owm._apply_config_defaults(ap)
    args = ap.parse_args([])           # no CLI flags -> config values become the defaults
    assert args.dpi == 321
    assert args.no_ocr is True
    assert abs(args.sauvola_k - 0.22) < 1e-9


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason='TOML config support needs Python 3.11+ (stdlib tomllib)')
def test_cli_overrides_config(tmp_path, monkeypatch):
    cfg = tmp_path / 'c.toml'
    cfg.write_text('dpi = 321\n', encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['prog', '--config', str(cfg), '--dpi', '150'])
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', type=Path)
    ap.add_argument('--dpi', type=int, default=200)
    U.owm._apply_config_defaults(ap)
    args = ap.parse_args(['--dpi', '150'])
    assert args.dpi == 150             # explicit CLI wins over the config default


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_in_place_overwrites_scan_leaves_others(tmp_path):
    """--in-place (dest == src): a scanned PDF is overwritten with its smaller
    compressed self; a born-digital PDF is left byte-for-byte untouched."""
    scan = tmp_path / 'scan.pdf'
    pdfs = U.fixture_pdfs('line') or U.fixture_pdfs('photo_gray')
    if not pdfs:
        pytest.skip('no fixtures')
    scan.write_bytes(pdfs[0].read_bytes())
    orig = scan.stat().st_size
    res = U.owm.compress_one(str(scan), str(scan), 200, ocr=False, in_place=True)
    assert res.get('err') is None and res.get('action') == 'compressed', res
    assert scan.exists() and scan.stat().st_size < orig, 'in-place should shrink the scan'
    assert len(PdfReader(str(scan)).pages) >= 1

    born = tmp_path / 'born.pdf'
    U.make_born_digital_pdf(born, npages=2)
    before = born.read_bytes()
    res = U.owm.compress_one(str(born), str(born), 200, ocr=False, in_place=True)
    assert res.get('action') == 'born_digital', res
    assert born.read_bytes() == before, 'born-digital must be left byte-identical in place'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_single_file_cli(tmp_path):
    """`src` may be a single .pdf: the CLI processes just that file and writes a
    sibling '<name> (COMPRESSED).pdf' by default."""
    import subprocess as sp
    pdfs = U.fixture_pdfs('line') or U.fixture_pdfs('photo_gray')
    if not pdfs:
        pytest.skip('no fixtures')
    src = tmp_path / 'x.pdf'
    src.write_bytes(pdfs[0].read_bytes())
    r = sp.run([sys.executable, str(U.REPO_ROOT / 'ocrmyworkshopmanual.py'),
                str(src), '--no-ocr', '--no-log'], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-500:]
    assert (tmp_path / 'x (COMPRESSED).pdf').exists(), f'no sibling output; stdout:\n{r.stdout}'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_gs_repair_recovers_truncated(tmp_path):
    pdfs = U.fixture_pdfs('line') or U.fixture_pdfs('photo_gray')
    if not pdfs:
        pytest.skip('no fixtures')
    broken = tmp_path / 'broken.pdf'
    broken.write_bytes(pdfs[0].read_bytes()[:-900])   # drop trailer/xref -> malformed
    work = U.workdir()
    try:
        fixed = U.owm._gs_repair(broken, work)
        assert fixed is not None and fixed.exists(), 'repair should recover a truncated PDF'
        assert len(PdfReader(str(fixed)).pages) >= 1, 'repaired PDF should open with pages'
    finally:
        shutil.rmtree(work, ignore_errors=True)


CORRUPT_FIXTURE = U.FIXTURES_DIR / 'corrupt' / 'nissan_rogue_2009_FSU_corrupt.pdf'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
@pytest.mark.skipif(not CORRUPT_FIXTURE.exists(), reason='corrupt fixture missing')
def test_real_corrupt_pdf_is_recovered_whole(tmp_path):
    """A REAL corrupt manual (Nissan Rogue FSM, corrupt at source: garbage bytes inside
    the content streams, page tree intact). Ghostscript renders 0 pages and its pdfwrite
    repair salvages only 1 of 21 — which used to be shipped as a 1-page file. It must now
    be recovered WHOLE: every page, with its links and bookmarks."""
    from pypdf import PdfReader
    out = tmp_path / 'out.pdf'
    res = U.owm.compress_one(str(CORRUPT_FIXTURE), str(out), 200, ocr=False)
    assert res.get('err') is None, res
    assert 'repaired' in (res.get('note') or ''), f'repair should be reported: {res}'
    r = PdfReader(str(out))
    assert len(r.pages) == 21, f'page loss: {len(r.pages)}/21'
    links = 0
    for p in r.pages:
        a = p.get('/Annots')
        if a:
            links += sum(1 for x in a.get_object()
                         if x.get_object().get('/Subtype') == '/Link')

    def cnt(items):
        n = 0
        for it in items:
            n += cnt(it) if isinstance(it, list) else 1
        return n
    assert links > 100, f'links lost: {links}'
    assert cnt(r.outline) > 50, 'bookmarks lost'
    assert out.stat().st_size < CORRUPT_FIXTURE.stat().st_size, 'should also compress'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
@pytest.mark.skipif(not CORRUPT_FIXTURE.exists(), reason='corrupt fixture missing')
def test_corrupt_pdf_not_byte_copied_on_the_ocr_only_path(tmp_path):
    """The OCR-only path copies the source through byte-for-byte, which faithfully
    reproduced a CORRUPT file (the copy rendered 0 pages). It must repair instead."""
    out = tmp_path / 'out.pdf'
    res = U.owm.compress_one(str(CORRUPT_FIXTURE), str(out), 200, ocr=False, ocr_only=True)
    assert res.get('err') is None, res
    assert out.exists()
    assert out.read_bytes() != CORRUPT_FIXTURE.read_bytes(), 'shipped the corrupt bytes'
    assert U.owm._renders_ok(out), 'output still does not render'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_repair_prefers_the_engine_that_keeps_every_page(tmp_path):
    """Repair must not accept a PARTIAL salvage. Ghostscript's pdfwrite recovered only
    1 of 21 pages from a real corrupt manual while qpdf recovered all 21, so a repair
    returning fewer pages than the source is rejected and the next engine is tried."""
    src = U.make_scan_pdf(tmp_path / 'multi.pdf', npages=4, dpi=150)
    broken = tmp_path / 'broken.pdf'
    broken.write_bytes(src.read_bytes()[:-900])       # damage the trailer/xref
    work = U.workdir()
    try:
        fixed = U.owm._repair_pdf(broken, work, expect_pages=4)
        assert fixed is not None, 'a recoverable PDF should be repaired'
        assert len(PdfReader(str(fixed)).pages) >= 4, 'repair must keep every page'
        # a repair that cannot reach the expected page count is refused outright
        assert U.owm._repair_pdf(broken, work, expect_pages=999) is None
    finally:
        shutil.rmtree(work, ignore_errors=True)
