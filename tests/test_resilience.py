"""Tests for the resilience / ease-of-use additions:
  - per-step timeout aborts a file gracefully (never hangs the batch)
  - output verification flags a wrong page count / unopenable output
  - dry-run preview_one predicts the action and writes nothing
  - config file, duplicate hashing, retry-CSV parsing, malformed-PDF repair
"""
import argparse
import os
import shutil
import subprocess
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
    res = U.owm.preview_one(str(src), 200, True, 10, 0.02, 150, 60,
                            0.25, 0.30, 0.6)
    assert res['action'] == 'born_digital' and res['err'] is None
    assert res['new'] == res['orig']                      # predicts no size change
    assert set(tmp_path.iterdir()) == before              # nothing written


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_preview_one_scanned_projects_smaller(tmp_path):
    pdfs = U.fixture_pdfs('line')
    if not pdfs:
        pytest.skip('no line fixtures')
    res = U.owm.preview_one(str(pdfs[0]), 200, True, 10, 0.02, 150, 60,
                            0.25, 0.30, 0.6)
    assert res['err'] is None
    assert res['action'] in ('compressed', 'kept_original')
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


def test_read_failed_files(tmp_path):
    """Only the FAILED rows, and the `file` value exactly as recorded — full paths in a current
    report, relative ones in an older one. Resolving them is `_retry_jobs`' job."""
    csv = tmp_path / 'r.csv'
    csv.write_text('file,action,orig_bytes,new_bytes,pct_of_orig,scan_frac,note,error\n'
                   'ok.pdf,compressed,10,1,10,,,\n'
                   r'C:\arch\bad.pdf,FAILED,10,0,,,,timed out' + '\n'
                   'sub/also_bad.pdf,FAILED,10,0,,,,render failed\n', encoding='utf-8')
    assert U.owm._read_failed_files(csv) == [r'C:\arch\bad.pdf', 'sub/also_bad.pdf']


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
                str(src), '--no-ocr'], capture_output=True, text=True, timeout=180)
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
def test_corrupt_pdf_not_byte_copied_when_keeping_the_original(tmp_path, monkeypatch):
    """The keep-original path passes the source through byte-for-byte, which faithfully
    reproduced a CORRUPT file (the copy rendered 0 pages). It must repair instead.
    Forced here by making the pre-check declare the file not worth compressing."""
    monkeypatch.setattr(U.owm, 'PRECHECK_MIN_PAGES', 1)
    monkeypatch.setattr(U.owm, 'sample_projection', lambda *a, **k: 1.0)
    out = tmp_path / 'out.pdf'
    res = U.owm.compress_one(str(CORRUPT_FIXTURE), str(out), 200, ocr=False)
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


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_self_audit_refuses_to_ship_a_degraded_file(tmp_path):
    """The tool must audit its OWN output against the source before overwriting anything.
    Damage and success look identical on file size — losing a page, a colour or the text
    layer all make the file smaller — so the check compares content, and a failure keeps
    the original instead of shipping it."""
    src = U.make_scan_pdf(tmp_path / 'src.pdf', npages=4, dpi=200)
    good = tmp_path / 'good.pdf'
    res = U.owm.compress_one(str(src), str(good), 200, ocr=False)
    assert res.get('err') is None, res

    # page loss is fatal
    fatal, _ = U.owm._audit_output(good, 99, src_p=src)
    assert fatal and 'pages' in fatal

    # an unopenable output is fatal
    junk = tmp_path / 'junk.pdf'
    junk.write_bytes(b'not a pdf')
    fatal, _ = U.owm._audit_output(junk, 4, src_p=src)
    assert fatal and 'open' in fatal

    # a healthy result passes
    fatal, _ = U.owm._audit_output(good, 4, src_p=src, colour_pages=set())
    assert fatal is None, fatal


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
@pytest.mark.skipif(_ocr_missing is not None, reason=str(_ocr_missing))
def test_self_audit_catches_lost_text_layer(tmp_path):
    """Losing the searchable text is invisible to every size metric — it must be caught
    by word recall against the source, not by character count (a re-OCR differs)."""
    src = U.make_scan_pdf(tmp_path / 'src.pdf', npages=3, dpi=200)
    ocred = tmp_path / 'ocred.pdf'
    res = U.owm.compress_one(str(src), str(ocred), 200, ocr=True, language='eng')
    assert res.get('err') is None, res
    textless = U.make_scan_pdf(tmp_path / 'textless.pdf', npages=3, dpi=150)
    fatal, _ = U.owm._audit_output(textless, 3, src_p=ocred, colour_pages=set())
    assert fatal and 'text' in fatal.lower(), fatal


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
@pytest.mark.skipif(_ocr_missing is not None, reason=str(_ocr_missing))
def test_in_place_keeps_the_ocr_layer_when_not_compressing(tmp_path, monkeypatch):
    """Regression: OCR runs on the SOURCE before the place step, so the place step is
    told not to OCR again. The in-place 'nothing changed, leave it alone' shortcut then
    judged the file unchanged and threw the fresh text layer away — 13 of 64 files in a
    sample run were OCR'd and shipped unsearchable."""
    from pypdf import PdfReader
    src = U.make_scan_pdf(tmp_path / 'src.pdf', npages=2, dpi=200)
    monkeypatch.setattr(U.owm, 'PRECHECK_MIN_PAGES', 1)
    monkeypatch.setattr(U.owm, 'sample_projection', lambda *a, **k: 1.0)
    res = U.owm.compress_one(str(src), str(src), 200, ocr=True, language='eng',
                             in_place=True)
    assert res.get('err') is None, res
    chars = len((PdfReader(str(src)).pages[0].extract_text() or '').strip())
    assert chars > 100, f'in-place OCR layer was discarded ({chars} chars)'


# ── Duplicate object definitions (a download that stitched in a repeated chunk) ───
# The FSU fixture defines objects 626-665 TWICE (96032 bytes of duplication, exactly the
# gap between its linearization /L and the real file size), each copy damaged in different
# places. qpdf resolves duplicates by last-definition-wins and so picked corrupt copies:
# a /Widths array with 221 entries for a 119-slot range (every heading glyph mis-advanced,
# rendering "SECT I ON") and an unparseable footer Form XObject (footer gone from all 21
# pages) — while the intact copies sat in the same file. Page count, link count and text
# recall all passed on that output, which is why these assertions are structural.

@pytest.mark.skipif(not CORRUPT_FIXTURE.exists(), reason='corrupt fixture missing')
def test_dedupe_picks_the_sound_copy_of_each_duplicated_object():
    """Not "prefer the xref copy" and not "prefer the last" — either rule gets half of
    these wrong. 644/659 must come from copy 1 and 664 from copy 2."""
    work = U.workdir()
    try:
        out, stats = U.owm._dedupe_objects(CORRUPT_FIXTURE, work)
        assert out is not None, 'duplicates were not resolved'
        assert stats['dup'] == 4, f'expected 4 differing duplicates, got {stats}'
        assert set(stats['picked']) == {'644#1of2', '659#1of2', '664#2of2'}, stats['picked']
        assert out.stat().st_size == CORRUPT_FIXTURE.stat().st_size, \
            'the losing copy must be blanked in place so no byte offset shifts'
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
@pytest.mark.skipif(not CORRUPT_FIXTURE.exists(), reason='corrupt fixture missing')
def test_repair_keeps_the_footer_form_and_correct_font_metrics():
    """The repaired file must keep what the pages still draw, with usable metrics."""
    from pypdf import PdfReader
    work = U.workdir()
    try:
        stats = {}
        fixed = U.owm._repair_pdf(CORRUPT_FIXTURE, work, 21, 0, stats)
        assert fixed is not None
        page = PdfReader(str(fixed)).pages[0]
        forms = {n: o for n, o in U.owm._xobjects(page.get_inherited('/Resources')).items()
                 if o.get('/Subtype') == '/Form'}
        assert '/Fm0' in forms, 'the footer Form XObject was dropped again'
        assert [float(x) for x in forms['/Fm0'].get('/Matrix')] == [1, 0, 0, 1, 0, 0]
        assert len(forms['/Fm0'].get_data()) == 139, 'footer stream is not the intact one'
        assert U.owm._dangling_xobjects(page) == set()
        assert U.owm._bad_font_widths(page) == []
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.skipif(not CORRUPT_FIXTURE.exists(), reason='corrupt fixture missing')
def test_audit_rejects_output_that_dropped_something_still_drawn():
    """A plain qpdf rewrite (no duplicate resolution) drops /Fm0 while every page still
    ends `q /GS1 gs /Fm0 Do Q`. Page count, links and recall all pass on it — the audit
    must still refuse it, or the footer silently vanishes from 21 pages."""
    import pikepdf
    work = U.workdir()
    try:
        naive = work / 'naive.pdf'
        with pikepdf.open(str(CORRUPT_FIXTURE)) as p:
            p.save(str(naive))
        fatal, _warn = U.owm._audit_output(naive, 21, CORRUPT_FIXTURE)
        assert fatal, 'audit passed a file that dropped a drawn XObject'
        assert 'Fm0' in fatal or 'font metrics' in fatal, fatal
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.skipif(not CORRUPT_FIXTURE.exists(), reason='corrupt fixture missing')
def test_one_unreadable_page_does_not_blank_the_whole_text_sample():
    """`_sampled_text` used to wrap all six pages in one try: a single page whose content
    stream cannot be decoded returned '' for the entire sample, which silently disabled the
    text-survival check on the source side and faked 'searchable text lost' on the output
    side. Page index 2 of this fixture raises while the other 20 extract fine."""
    text, bad = U.owm._sampled_text(CORRUPT_FIXTURE, list(range(21)))
    assert bad == {2}, f'expected exactly page index 2 to be unreadable, got {bad}'
    assert len(text) == 20, f'the other 20 pages must still be returned, got {len(text)}'
    assert sum(len(t) for t in text.values()) > 20000, 'text was lost with the bad page'


# ── OCR thread budget at the tail of a run ───────────────────────────────────────
# Measured on a real 5,243-file run: the budget was computed once, before the pool, from the
# TOTAL job count — `6 // min(6, 5243)` = 1 — and never revisited. Its last file, a 57.9 MB
# Russian book, therefore OCR'd on ONE core for 20+ minutes while the other five sat idle
# with nothing left to do. `--jobs 1` was correct for the first 5,242 files and wrong for the
# one that decided when the run ended.

def _budget(cores, workers, remaining, monkeypatch):
    """`_ocr_jobs_now()` for a given machine and a given point in the run."""
    import multiprocessing as mp
    monkeypatch.setattr(U.owm, '_OCR_CORES', cores)
    monkeypatch.setattr(U.owm, '_OCR_WORKERS', workers)
    monkeypatch.setattr(U.owm, '_OCR_REMAINING', mp.Value('i', remaining))
    return U.owm._ocr_jobs_now()


def test_a_saturated_pool_still_gets_one_thread_each(monkeypatch):
    """The behaviour that must NOT change: while there is more work than workers, dividing
    the cores again would oversubscribe the machine."""
    for remaining in (5243, 100, 7, 6):
        assert _budget(6, 6, remaining, monkeypatch) == 1, remaining


def test_the_last_file_gets_the_idle_cores(monkeypatch):
    """The fix. With one file left there is nothing else to spend the machine on."""
    assert _budget(6, 6, 1, monkeypatch) == 6
    assert _budget(6, 6, 2, monkeypatch) == 3
    assert _budget(6, 6, 3, monkeypatch) == 2


def test_the_budget_is_never_zero_and_never_exceeds_the_cores(monkeypatch):
    """It is passed straight to `ocrmypdf --jobs`, so 0 or a negative would be an argument
    error on a file that was otherwise fine. `remaining` can legitimately reach 0 between the
    last completion and the pool shutting down."""
    for remaining in (0, -1, 1, 3, 999):
        got = _budget(6, 6, remaining, monkeypatch)
        assert 1 <= got <= 6, (remaining, got)


def test_no_shared_counter_falls_back_to_the_static_value(monkeypatch):
    """A worker started without one — and any direct call outside a pool — must still get a
    usable number. A thread-count hint may never be the thing that fails a file."""
    monkeypatch.setattr(U.owm, '_OCR_REMAINING', None)
    monkeypatch.setattr(U.owm, 'OCR_JOBS', 3)
    assert U.owm._ocr_jobs_now() == 3
    monkeypatch.setattr(U.owm, '_OCR_CORES', 0)     # cores unknown
    assert U.owm._ocr_jobs_now() == 3


def test_an_unreadable_counter_falls_back_instead_of_raising(monkeypatch):
    """Same rule, for a counter that is present but broken."""
    class Boom:
        @property
        def value(self):
            raise RuntimeError('shared memory gone')
    monkeypatch.setattr(U.owm, '_OCR_CORES', 6)
    monkeypatch.setattr(U.owm, '_OCR_WORKERS', 6)
    monkeypatch.setattr(U.owm, '_OCR_REMAINING', Boom())
    monkeypatch.setattr(U.owm, 'OCR_JOBS', 1)
    assert U.owm._ocr_jobs_now() == 1


def test_the_counter_reaches_the_workers_and_tracks_the_parent():
    """The mechanism, end to end through a real pool. A `multiprocessing.Value` survives
    Windows spawn only because it is passed through process CREATION — `initargs` — so this
    pins the one property the whole fix rests on: workers see the parent's later writes, not
    a snapshot taken when the pool was built."""
    import concurrent.futures as cf
    import multiprocessing as mp
    remaining = mp.Value('i', 8)
    with cf.ProcessPoolExecutor(
            max_workers=2, initializer=U.owm._init_worker,
            initargs=(1, 6, 6, remaining)) as ex:
        saturated = list(ex.map(_read_budget, range(2)))
        remaining.value = 1                      # the batch drains
        tail = list(ex.map(_read_budget, range(2)))
    assert saturated == [1, 1], saturated
    assert tail == [6, 6], tail


def _read_budget(_i):
    """Module-level so it is picklable for the pool above."""
    return U.owm._ocr_jobs_now()


# ── OCR language ─────────────────────────────────────────────────────────────────
# --language defaulted to 'eng', so a 532-page Cyrillic Suzuki manual was re-OCR'd as
# English: that does not read it worse, it REPLACES real text with Latin noise. It scored
# word recall 0.00 against its own source and the audit had to discard the whole file.

def test_text_script_needs_a_real_share_of_letters():
    """A stray Cyrillic glyph in an English manual must not swing the verdict."""
    assert U.owm._text_script('руководство по ремонту и обслуживанию автомобиля ' * 3) == 'Cyrillic'
    assert U.owm._text_script('front suspension removal installation inspection ' * 3) == ''
    assert U.owm._text_script('front suspension removal inspection disposal ' * 3 + 'абв') == ''
    assert U.owm._text_script('абв') == '', 'too little text to judge'


def test_language_guard_adds_the_script_its_text_layer_proves(monkeypatch, tmp_path):
    """Whatever the language came from, a source whose existing text layer is Cyrillic must
    get a Cyrillic pack added. OSD cannot cover this case: it reads rendered images, so it
    ignores a text layer, and it falls back to 'eng' whenever no page clears its confidence
    floor. The guard only ADDS, so an explicit --language is still honoured."""
    pdf = U.make_born_digital_pdf(tmp_path / 'src.pdf', npages=2)
    monkeypatch.setattr(U.owm, '_installed_langs', lambda: {'eng', 'rus', 'osd'})
    monkeypatch.setattr(U.owm, '_sampled_text',
                        lambda *a, **k: ({0: 'руководство по ремонту автомобиля ' * 4}, set()))
    lang, note = U.owm._resolve_language(pdf, U.workdir(), 'eng', 0)
    assert set(lang.split('+')) == {'eng', 'rus'}, lang
    assert 'Cyrillic' in note, note


def test_language_guard_leaves_a_latin_source_alone(monkeypatch, tmp_path):
    """The inverse: an English text layer must not attract extra packs (slower, worse)."""
    pdf = U.make_born_digital_pdf(tmp_path / 'src2.pdf', npages=2)
    monkeypatch.setattr(U.owm, '_installed_langs', lambda: {'eng', 'rus', 'osd'})
    lang, note = U.owm._resolve_language(pdf, U.workdir(), 'eng', 0)
    assert lang == 'eng', lang
    assert note == '', note


def test_osd_never_votes_on_a_stale_render(tmp_path):
    """`_detect_language` treated `png.exists()` as proof the render worked, so a leftover
    file from an earlier call in the same work dir passed it and OSD voted on the WRONG
    image — measured: a PDF Ghostscript cannot open at all was labelled Cyrillic from a
    different manual's page. An unopenable file must yield the safe default."""
    broken = tmp_path / 'broken.pdf'
    broken.write_bytes(b'%PDF-1.4\nnot a pdf at all\n')
    work = U.workdir()
    try:
        (work / 'osd_1.png').write_bytes((U.FIXTURES_DIR / 'stale.png').read_bytes()
                                         if (U.FIXTURES_DIR / 'stale.png').exists()
                                         else b'\x89PNG\r\n\x1a\n' + b'\x00' * 64)
        assert U.owm._detect_language(broken, work, 0) == 'eng'
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ── Link / bookmark preservation through the compress path ───────────────────────
# No corpus file can cover this: every archive file carrying links is born-digital, so it is
# copied untouched and the compress path never sees one. Rebuilding a PDF from rendered pages
# drops everything that is not page content — measured, a rebuild kept 5 of 249 links on one
# manual and lost all 248 bookmarks on another — so `_graft_into_source` puts the compressed
# pages back INTO the original document instead. These tests hold that behaviour in place.

@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_internal_links_survive_compression(tmp_path):
    """A TOC page of internal /GoTo links, pointing at pages the compressor rewrites."""
    src = U.make_linked_toc_pdf(tmp_path / 'toc.pdf', npages=4, dpi=200)
    before = U.link_report(src)
    assert before['goto'] == 4 and before['uri'] == 0, before
    assert before['goto_targets'] == [1, 2, 3, 4], before['goto_targets']

    out = tmp_path / 'out.pdf'
    res = U.owm.compress_one(str(src), str(out), 200, ocr=False, min_savings=0.0)
    assert res.get('err') is None, res
    note = res.get('note') or ''
    # 'rebuilt' means the graft failed and links were dropped — the thing under test
    assert 'rebuilt' not in note, f'graft did not run: {note}'

    after = U.link_report(out)
    assert after['pages'] == before['pages']
    assert after['goto'] == before['goto'], f"links lost: {before['goto']} -> {after['goto']}"
    assert after['bookmarks'] >= before['bookmarks']
    assert after['unresolved'] == 0
    # THE assertion: destinations must resolve to the same pages, in order. Surviving link
    # objects with dangling targets would satisfy every count above.
    assert after['goto_targets'] == before['goto_targets'], after['goto_targets']
    # ...and the targets must really have been recompressed, or the test proves nothing:
    # the TOC page stays vector (no images) while the scanned pages become JBIG2.
    assert after['toc_filters'] == [], after['toc_filters']
    assert any('JBIG2' in f for f in after['last_filters']), after['last_filters']


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_a_failed_graft_names_its_reason_and_is_refused(tmp_path, monkeypatch):
    """The failure branch. It has never fired in production — 0 times across 18 run reports
    and 318 files that took the compress path — which is exactly why it needs a test: an
    unexercised silent fallback is where link loss would hide. Two things must hold: the
    reason reaches the note, and the audit refuses a result that actually lost links."""
    src = U.make_linked_toc_pdf(tmp_path / 'toc2.pdf', npages=4, dpi=200)

    def boom(*a, **k):
        raise U.owm.GraftFailed('page count 5 vs compressed 4')
    monkeypatch.setattr(U.owm, '_graft_into_source', boom)

    out = tmp_path / 'out2.pdf'
    res = U.owm.compress_one(str(src), str(out), 200, ocr=False, min_savings=0.0)
    err, note = res.get('err') or '', res.get('note') or ''
    # 1. the CAUSE is named, not discarded into a bare False
    assert 'page count 5 vs compressed 4' in note, f'reason not reported: {note!r}'
    assert 'rebuilt' in note, note
    # 2. the CONSEQUENCE is refused. Assert the audit's own wording and the counts, not just
    #    the substring 'link' — 'links/bookmarks not carried over' already contains that, so a
    #    looser check would pass even if the file shipped.
    assert 'link annotations lost (4->0)' in err, f'audit did not refuse it: {err!r}'
    assert 'original kept' in err, err
    # 3. and nothing degraded was written
    assert not out.exists() or out.read_bytes() == src.read_bytes(), \
        'shipped a link-less rebuild'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_a_file_with_no_links_still_compresses(tmp_path):
    """The other half: making link loss fatal must not refuse files with nothing to lose.
    `la < lb` cannot fire when lb == 0, so a plain scan is unaffected — asserted, because it
    would be an easy thing to break by adding a well-meaning guard."""
    src = U.make_scan_pdf(tmp_path / 'plain.pdf', npages=3, dpi=200)
    assert U.link_report(src)['goto'] == 0
    out = tmp_path / 'plain_out.pdf'
    res = U.owm.compress_one(str(src), str(out), 200, ocr=False)
    assert res.get('err') is None, res
    assert out.exists() and out.stat().st_size < src.stat().st_size


# ── core allocation: a huge file must get the whole box, deterministically ─────────────

def test_ocr_jobs_scale_with_page_count(monkeypatch):
    """The budget is a function of the FILE, not of the queue. Measured on the 2026-07-31
    Mazda run: a 2910-page manual launched ocrmypdf while ~50 files were queued, got
    `6 // 6` = 1 thread and held it for two hours while five of six cores idled."""
    monkeypatch.setattr(U.owm, '_OCR_CORES', 6)
    monkeypatch.setattr(U.owm, '_OCR_REMAINING', None)
    assert U.owm._ocr_jobs_for(2910) == 6        # large -> the whole box
    assert U.owm._ocr_jobs_for(800) == 6         # boundary is inclusive
    assert U.owm._ocr_jobs_for(400) == 3         # medium -> half
    assert U.owm._ocr_jobs_for(80) == 2
    assert U.owm._ocr_jobs_for(10) == 1          # small -> file-level parallelism is enough
    # never exceeds the physical-core cap, whatever the page count
    monkeypatch.setattr(U.owm, '_OCR_CORES', 2)
    assert U.owm._ocr_jobs_for(5000) == 2


def test_ocr_jobs_fall_back_to_size_when_pages_unreadable(monkeypatch):
    """Some sources cannot be opened to be counted at all — measured: pypdf raises
    AttributeError('NullObject') on the 1990 RX7 section manuals. A thread-count hint must
    degrade to bytes, never raise and never fail the file."""
    monkeypatch.setattr(U.owm, '_OCR_CORES', 6)
    monkeypatch.setattr(U.owm, '_OCR_REMAINING', None)
    assert U.owm._ocr_jobs_for(0, 143 * 1024 * 1024) == 6
    assert U.owm._ocr_jobs_for(0, 20 * 1024 * 1024) == 3
    assert U.owm._ocr_jobs_for(0, 900 * 1024) == 1
    assert U.owm._ocr_jobs_for(0, 0) == 1        # nothing known at all -> narrowest


def test_ocr_jobs_budget_survives_an_unopenable_file(tmp_path, monkeypatch):
    """End-to-end of the fallback: a file pypdf cannot parse still yields a usable budget."""
    monkeypatch.setattr(U.owm, '_OCR_CORES', 6)
    monkeypatch.setattr(U.owm, '_OCR_REMAINING', None)
    junk = tmp_path / 'broken.pdf'
    junk.write_bytes(b'\x7e\xa0\x90\x11M' + b'\x00' * 4096)   # no %PDF header, like the real ones
    jobs = U.owm._ocr_jobs_budget(junk)
    assert 1 <= jobs <= 6


def test_ocr_jobs_budget_keeps_the_draining_queue_floor(monkeypatch):
    """The old queue-drain rule stays live as a FLOOR: one small file left on a 6-core box
    should still get the idle cores, which is the case `_ocr_jobs_now` was written for."""
    import multiprocessing
    monkeypatch.setattr(U.owm, '_OCR_CORES', 6)
    monkeypatch.setattr(U.owm, '_OCR_WORKERS', 6)
    monkeypatch.setattr(U.owm, '_OCR_REMAINING', multiprocessing.Value('i', 1))
    monkeypatch.setattr(U.owm, '_OCR_TOKENS', None)
    assert U.owm._ocr_jobs_budget(pages=3) == 6      # tiny file, empty queue -> all of it


def test_thread_claims_clamp_at_oversubscribe_and_never_block(monkeypatch):
    """Concurrent files derive budgets independently, so the shared tally is what stops six
    workers each claiming the whole machine. It CLAMPS: a step that finds the budget spent
    runs narrow rather than waiting, because a step that never starts produces no progress
    for the stall watchdog to see."""
    import multiprocessing
    monkeypatch.setattr(U.owm, '_OCR_CORES', 6)
    monkeypatch.setattr(U.owm, '_OCR_TOKENS', multiprocessing.Value('i', 0))
    monkeypatch.setattr(U.owm, 'OVERSUBSCRIBE', 1.5)
    cap = int(6 * 1.5)
    assert U.owm._claim_threads(6) == 6
    assert U.owm._claim_threads(6) == cap - 6         # only what is left
    assert U.owm._claim_threads(6) == 1               # spent -> narrowest, still runs
    U.owm._release_threads(cap)
    assert U.owm._claim_threads(4) == 4               # released, budget available again


def test_thread_grant_is_released_even_when_the_step_raises(monkeypatch):
    """A crashed, stalled or killed step must not leak its share — otherwise every later file
    in that run silently drops to one thread."""
    import multiprocessing
    monkeypatch.setattr(U.owm, '_OCR_CORES', 6)
    monkeypatch.setattr(U.owm, '_OCR_TOKENS', multiprocessing.Value('i', 0))
    with pytest.raises(RuntimeError):
        with U.owm._threads(4) as got:
            assert got == 4
            raise RuntimeError('step blew up')
    assert U.owm._OCR_TOKENS.value == 0, 'threads leaked after a failing step'


def test_render_bands_cover_every_page_exactly_once():
    """The sharding correctness property: whatever the split, the bands must reproduce the
    page sequence exactly — no gap, no overlap, no page rendered at another page's dpi.
    A silent off-by-one here would ship a manual with duplicated or missing pages."""
    for n in (1, 7, 49, 50, 51, 120, 401, 2910):
        for shards in (1, 2, 4, 6):
            for dpis in ([200] * n, [200] * (n // 2) + [300] * (n - n // 2)):
                bands = U.owm._render_bands(dpis, shards)
                pages = [p for f, l, _d in bands for p in range(f, l + 1)]
                assert pages == list(range(1, n + 1)), (n, shards, bands[:4])
                for f, l, d in bands:
                    assert all(dpis[p - 1] == d for p in range(f, l + 1)), (n, shards, f, l)


def test_render_bands_leave_small_and_medium_files_as_one_call():
    """Only what is worth splitting gets split: below two shards' worth of pages, the extra
    process starts cost more than they save, and the ~350 small files in a real run must not
    change behaviour at all."""
    assert U.owm._render_bands([200] * 30, 6) == [(1, 30, 200)]
    assert U.owm._render_bands([200] * 99, 6) == [(1, 99, 200)]
    assert len(U.owm._render_bands([200] * 100, 6)) == 2
    # shards=1 must reproduce the original, unsharded bands exactly
    mixed = [200] * 60 + [300] * 60
    assert U.owm._render_bands(mixed, 1) == [(1, 60, 200), (61, 120, 300)]


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_sharded_render_matches_sequential_page_for_page(tmp_path):
    """Sharding must be invisible in the output: the same pages, in the same order, with the
    same pixels. This is the test that catches a page-ordering regression, which would
    otherwise surface only as a scrambled 2900-page manual."""
    src = U.make_scan_pdf(tmp_path / 'many.pdf', npages=6, dpi=150)
    dpis = U.owm._page_render_dpis(src, 150)

    seq = tmp_path / 'seq'; seq.mkdir()
    assert U.owm._render_all(src, seq, dpis, 150, 0, shards=1)
    sha = tmp_path / 'sha'; sha.mkdir()
    # force a split regardless of the page-count floor by sharding the band list directly
    bands = [(1, 3, dpis[0]), (4, 6, dpis[0])]
    U.owm._render_bands_orig = U.owm._render_bands
    try:
        U.owm._render_bands = lambda d, s: bands
        assert U.owm._render_all(src, sha, dpis, 150, 0, shards=2)
    finally:
        U.owm._render_bands = U.owm._render_bands_orig

    a = sorted(p.name for p in seq.glob('p*.png'))
    b = sorted(p.name for p in sha.glob('p*.png'))
    assert a == b and len(a) == 6, (a, b)
    for name in a:
        assert (seq / name).read_bytes() == (sha / name).read_bytes(), \
            f'{name} differs between sequential and sharded render'


@pytest.mark.skipif(os.name != 'nt', reason='Win32 priority classes')
def test_below_normal_priority_actually_takes_effect_and_children_inherit():
    """It must be OBSERVED, not just called. The original used ctypes without declaring the
    Win32 types, so `GetCurrentProcess`'s pseudo-handle came back as a 32-bit -1, reached the
    64-bit HANDLE parameter as 0x00000000FFFFFFFF, and SetPriorityClass failed with
    GetLastError() 6 (ERROR_INVALID_HANDLE) — silently, behind `except: pass`. Every run stayed
    at NORMAL. Asserted in a SUBPROCESS so the test does not renice the test runner, and the
    child's own class is checked too, since gs/tesseract inherit rather than call this."""
    probe = (
        "import ctypes;from ctypes import wintypes;import ocrmyworkshopmanual as owm;"
        "ok=owm.set_below_normal_priority();"
        "k=ctypes.WinDLL('kernel32');k.GetCurrentProcess.restype=wintypes.HANDLE;"
        "k.GetPriorityClass.argtypes=[wintypes.HANDLE];k.GetPriorityClass.restype=wintypes.DWORD;"
        "cls=k.GetPriorityClass(k.GetCurrentProcess());"
        "import subprocess,sys;"
        "child=subprocess.run([sys.executable,'-c',"
        "\"import ctypes;from ctypes import wintypes;k=ctypes.WinDLL('kernel32');\""
        "\"k.GetCurrentProcess.restype=wintypes.HANDLE;\""
        "\"k.GetPriorityClass.argtypes=[wintypes.HANDLE];k.GetPriorityClass.restype=wintypes.DWORD;\""
        "\"print(k.GetPriorityClass(k.GetCurrentProcess()))\"],capture_output=True,text=True);"
        "print(ok, cls, child.stdout.strip())"
    )
    r = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True,
                       cwd=str(Path(U.owm.__file__).parent))
    assert r.returncode == 0, r.stderr
    ok, cls, child = r.stdout.split()
    assert ok == 'True', 'set_below_normal_priority reported failure'
    assert int(cls) == 0x4000, f'process not BELOW_NORMAL: {hex(int(cls))}'
    assert int(child) == 0x4000, f'child did not inherit BELOW_NORMAL: {hex(int(child))}'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_every_dpi_band_renders_even_when_sharding_is_off(tmp_path):
    """A mixed-resolution document has one band PER resolution run, and ALL of them must
    render — including when the file is under the shard floor and runs single-threaded.

    Regression: the unsharded path rendered only `runs[0]`, so `Suzuki Vitara - Manual
    usuario.pdf` (43 pages, 16 dpi bands, first band a single page) rendered 1 page of 43. The
    page-loss guard caught it and kept the original, but the file failed instead of compressing.
    Asserted with shards=1 BECAUSE that is the branch that was wrong; a single-band fixture
    cannot see it."""
    src = U.make_scan_pdf(tmp_path / 'mixed.pdf', npages=6, dpi=150)
    pages = len(PdfReader(str(src)).pages)
    # alternate the resolution so every page is its own band, the shape that broke
    dpis = [150 if i % 2 == 0 else 200 for i in range(pages)]
    assert len(U.owm._render_bands(dpis, 1)) > 1, 'fixture must produce multiple bands'

    work = tmp_path / 'w'; work.mkdir()
    assert U.owm._render_all(src, work, dpis, 200, 0, shards=1)
    got = sorted(p.name for p in work.glob('p*.png'))
    assert len(got) == pages, f'rendered {len(got)} of {pages} pages: {got}'
    # and the page numbering must be the global 1..N, with no gap
    assert got == [f'p{i:04d}.png' for i in range(1, pages + 1)], got


def test_language_is_resolved_from_the_source_not_our_render(tmp_path, monkeypatch):
    """On the compress path `_ocr_source` is handed OUR OWN render, which is text-free by
    construction — so the text-layer script guard in `_resolve_language` must be pointed at the
    real source instead, or it reads nothing and adds nothing.

    Measured cost of getting this wrong: a 173-page Japanese owner's manual (1653 CJK letters of
    1718 sampled, `jpn` installed) OCR'd as `eng`, scored word recall 0.01 against its own
    source, and was thrown away by the audit."""
    seen = {}

    def fake_resolve(p, work, language, timeout=0):
        seen['path'] = Path(p)
        return language, ''

    monkeypatch.setattr(U.owm, '_resolve_language', fake_resolve)
    monkeypatch.setattr(U.owm, '_available_ocr_lang', lambda l: l)
    # make the ocrmypdf call a no-op failure: we are only asserting WHICH file was inspected
    monkeypatch.setattr(U.owm, '_run_retry', lambda fn, **kw: (None, 1))

    source = tmp_path / 'the_real_source.pdf'
    source.write_bytes(b'%PDF-1.4\n')
    render = tmp_path / 'our_render.pdf'
    render.write_bytes(b'%PDF-1.4\n')
    work = tmp_path / 'w'; work.mkdir()

    U.owm._ocr_source(render, work, 'auto', has_vector=True, lang_src=source)
    assert seen['path'] == source, f'language read from {seen["path"].name}, not the source'

    # and with no lang_src (the ship-original path) it still uses the file it was given
    seen.clear()
    U.owm._ocr_source(source, work, 'auto', has_vector=False, preserve_images=True)
    assert seen['path'] == source
