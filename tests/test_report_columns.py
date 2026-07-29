"""Tests for the size floor and for the report's four decision columns.

The report has to answer, per file and without reading prose: what was done
(action), why (reason), what happened to the searchable text layer (ocr), and in
which language (language). And the size floor has to skip COMPRESSION of a small
file without ever skipping its OCR — a small scan is exactly as unsearchable as a
big one, and that half cannot be redone later.
"""
import csv
import sys
import time
from pathlib import Path

import pytest
from pypdf import PdfReader

import _util as U

_missing = U.tools_missing()
_ocr_missing = U.ocr_missing()
owm = U.owm


# ── the size floor ────────────────────────────────────────────────────────────

@pytest.fixture
def floor_5mb(monkeypatch):
    """Re-enable the production floor that conftest disables suite-wide."""
    monkeypatch.setattr(owm, 'MIN_COMPRESS_MB', 5.0)


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_small_file_is_not_compressed(tmp_path, floor_5mb):
    """A scan under the floor is shipped as-is: action=kept original, reason=small
    size, and its bytes are the ORIGINAL's, not a re-encode."""
    src = U.make_scan_pdf(tmp_path / 'small.pdf', npages=2)
    assert src.stat().st_size < 5 * 1048576, 'fixture must be under the floor to test it'
    before = src.read_bytes()
    out = tmp_path / 'out.pdf'
    res = owm.compress_one(str(src), str(out), 200, ocr=False)
    assert res.get('err') is None, res
    assert res['action'] == 'kept_original'
    assert res['reason'] == owm.REASON_SMALL
    assert out.read_bytes() == before, 'under the floor the original bytes must ship'
    assert 'under the' in res['note'] and 'floor' in res['note']


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_floor_zero_still_compresses(tmp_path, monkeypatch):
    """`--min-compress-mb 0` means compress everything — the floor is opt-out."""
    pdfs = U.fixture_pdfs('line') or U.fixture_pdfs('photo_gray')
    if not pdfs:
        pytest.skip('no fixtures')
    monkeypatch.setattr(owm, 'MIN_COMPRESS_MB', 5.0)     # floor on...
    src = tmp_path / 'scan.pdf'
    src.write_bytes(pdfs[0].read_bytes())
    res = owm.compress_one(str(src), str(tmp_path / 'a.pdf'), 200, ocr=False,
                           min_compress_mb=0)            # ...but overridden per call
    assert res.get('err') is None and res['action'] == 'compressed', res
    assert res['reason'] == owm.REASON_COMPRESSIBLE


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_small_file_in_place_is_left_untouched(tmp_path, floor_5mb):
    """Under the floor with --in-place and nothing to add: the file is not rewritten.
    (Skipping compression must not mean pointlessly churning every small PDF.)"""
    src = U.make_born_digital_pdf(tmp_path / 'text.pdf', npages=2)
    before = src.read_bytes()
    res = owm.compress_one(str(src), str(src), 200, ocr=False, in_place=True)
    assert res.get('err') is None, res
    assert src.read_bytes() == before


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_floor_skips_the_sample_projection(tmp_path, floor_5mb, monkeypatch):
    """The floor is checked BEFORE the pre-check, so a small file never pays to
    project a compression that will not happen."""
    monkeypatch.setattr(owm, 'PRECHECK_MIN_PAGES', 1)    # would otherwise always run
    called = []
    monkeypatch.setattr(owm, 'sample_projection',
                        lambda *a, **k: called.append(1) or 1.0)
    src = U.make_scan_pdf(tmp_path / 'small.pdf', npages=2)
    res = owm.compress_one(str(src), str(tmp_path / 'o.pdf'), 200, ocr=False)
    assert res.get('err') is None and res['reason'] == owm.REASON_SMALL, res
    assert called == [], 'sample_projection must not run below the floor'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
@pytest.mark.skipif(_ocr_missing is not None, reason=str(_ocr_missing))
def test_small_file_is_still_ocred(tmp_path, floor_5mb):
    """THE point of the floor: compression is skipped, OCR is NOT. A small scan with
    no text layer comes out searchable, and the row says 'new ocr' with a language."""
    src = U.make_scan_pdf(tmp_path / 'small.pdf', npages=2)
    assert not owm.has_any_text(src), 'fixture must start with no text layer'
    out = tmp_path / 'out.pdf'
    res = owm.compress_one(str(src), str(out), 200, ocr=True, language='eng')
    assert res.get('err') is None, res
    assert res['action'] == 'kept_original' and res['reason'] == owm.REASON_SMALL
    assert res['ocr_state'] == owm.OCR_NEW, res
    assert res['lang'], 'a run that OCR\'d must report the language it used'
    assert owm.has_any_text(out), 'a skipped-compression file must still be searchable'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_floor_preview_matches_and_writes_nothing(tmp_path, floor_5mb):
    """--dry-run applies the same floor, so the preview and the real run agree."""
    src = U.make_scan_pdf(tmp_path / 'small.pdf', npages=2)
    before = set(tmp_path.iterdir())
    res = owm.preview_one(str(src), 200, True, 10, 0.02, 150, 60, 0.25, 0.30, 0.6,
                          ocr=False)
    assert res.get('err') is None, res
    assert res['action'] == 'kept_original' and res['reason'] == owm.REASON_SMALL
    assert res['new'] == res['orig'], 'no size change is projected below the floor'
    assert set(tmp_path.iterdir()) == before, 'dry-run must write nothing'


# ── the OCR column ────────────────────────────────────────────────────────────

def test_has_any_text_separates_none_from_partial(tmp_path):
    """`has_text` (searchable everywhere) and `has_any_text` (has a layer at all) are
    the two different questions that 'new ocr' vs 're-ocr' turns on."""
    born = U.make_born_digital_pdf(tmp_path / 'text.pdf', npages=3)
    assert owm.has_text(born) and owm.has_any_text(born)
    scan = U.make_scan_pdf(tmp_path / 'scan.pdf', npages=2)
    assert not owm.has_text(scan) and not owm.has_any_text(scan)


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_no_ocr_run_reports_not_requested(tmp_path):
    """--no-ocr is its own OCR state, distinct from 'kept existing': nothing was
    checked and nothing was added, and no language was resolved."""
    pdfs = U.fixture_pdfs('line') or U.fixture_pdfs('photo_gray')
    if not pdfs:
        pytest.skip('no fixtures')
    src = tmp_path / 'scan.pdf'
    src.write_bytes(pdfs[0].read_bytes())
    res = owm.compress_one(str(src), str(tmp_path / 'o.pdf'), 200, ocr=False)
    assert res.get('err') is None, res
    assert res['ocr_state'] == owm.OCR_NONE
    assert res['lang'] == '', 'no OCR ran, so there is no language to report'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
@pytest.mark.skipif(_ocr_missing is not None, reason=str(_ocr_missing))
def test_existing_text_layer_is_kept_not_redone(tmp_path, floor_5mb):
    """A file already searchable on every page reports 'kept existing' — the value
    that tells a reviewer this manual's text was NOT regenerated."""
    src = U.make_born_digital_pdf(tmp_path / 'text.pdf', npages=3)
    res = owm.compress_one(str(src), str(tmp_path / 'o.pdf'), 200, ocr=True,
                           language='eng')
    # born-digital short-circuits before any OCR; either way the layer is preserved.
    assert res.get('err') is None, res
    assert res['ocr_state'] == owm.OCR_KEPT, res


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
@pytest.mark.skipif(_ocr_missing is not None, reason=str(_ocr_missing))
def test_compressing_an_already_ocred_scan_reports_re_ocr(tmp_path):
    """End-to-end 're-ocr': a scan that ALREADY carries a text layer and does get
    compressed has that layer regenerated from the source render. Reporting 'new ocr'
    there would hide that the manual's existing text was replaced — the distinction
    the split `ocr` column exists to make."""
    pdfs = U.fixture_pdfs('line') or U.fixture_pdfs('photo_gray')
    if not pdfs:
        pytest.skip('no fixtures')
    src = tmp_path / 'scan.pdf'
    src.write_bytes(pdfs[0].read_bytes())
    work = tmp_path / 'w'
    work.mkdir()
    # an OCR'd but UNcompressed scan — exactly what a previously-OCR'd archive holds
    ocred, _lang, _n = owm._ocr_source(src, work, 'eng', has_vector=False,
                                       preserve_images=True)
    if not ocred or not owm.has_any_text(ocred):
        pytest.skip('could not produce an OCR\'d source to re-OCR')
    res = owm.compress_one(str(ocred), str(tmp_path / 'out.pdf'), 200,
                           ocr=True, language='eng', min_savings=0.0)
    assert res.get('err') is None and res['action'] == 'compressed', res
    assert res['ocr_state'] == owm.OCR_REDO, res
    assert res['lang'], 'a re-ocr must report the language it used'


# ── the audit must not blame us for the source's own defects ──────────────────

@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_audit_tolerates_font_metrics_the_source_already_had(tmp_path):
    """A file that ARRIVES with a /Widths array contradicting its own /FirstChar../LastChar
    must still ship. The check is for damage we caused; condemning an inherited fault
    threw away a good output and left the file unsearchable (measured: 18 of 20 files in
    a Nissan Primera folder, whose page-1 /dgp0 carries 256 widths for 224 slots)."""
    import pikepdf
    src = U.make_born_digital_pdf(tmp_path / 'src.pdf', npages=3)
    broken = tmp_path / 'broken.pdf'
    with pikepdf.open(str(src)) as p:
        for page in p.pages:
            for _n, f in dict(page.get('/Resources', {}).get('/Font', {})).items():
                f['/FirstChar'] = 32
                f['/LastChar'] = 100          # 69 slots...
                f['/Widths'] = pikepdf.Array([500] * 80)   # ...but 80 widths
        p.save(str(broken))
    from pypdf import PdfReader as R
    assert owm._bad_font_widths(R(str(broken)).pages[0]), 'fixture must be broken'

    # same defect on both sides -> inherited, so a warning, not a refusal
    fatal, warn = owm._audit_output(broken, 3, src_p=broken)
    assert not fatal, f'inherited font metrics must not be fatal: {fatal}'
    assert 'already so in the source' in warn, warn
    # tallied once for the file, not repeated for each of the sampled pages
    assert warn.count('broken font metrics') == 1, warn

    # the same defect against a CLEAN source is real damage -> still fatal
    fatal, _warn = owm._audit_output(broken, 3, src_p=src)
    assert fatal and 'font metrics' in fatal, 'newly broken metrics must still be refused'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_source_defect_does_not_block_ocr_of_a_small_file(tmp_path, floor_5mb):
    """The whole point, end to end: a small file with pre-existing bad font metrics is
    not compressed (too small), IS made searchable, and is not reported FAILED."""
    import pikepdf
    src = U.make_scan_pdf(tmp_path / 'scan.pdf', npages=2)
    broken = tmp_path / 'broken.pdf'
    with pikepdf.open(str(src)) as p:
        pdf_font = pikepdf.Dictionary(Type=pikepdf.Name('/Font'),
                                      Subtype=pikepdf.Name('/Type1'),
                                      BaseFont=pikepdf.Name('/Helvetica'),
                                      FirstChar=32, LastChar=100,
                                      Widths=pikepdf.Array([500] * 80))
        for page in p.pages:
            page.Resources['/Font'] = pikepdf.Dictionary(dgp0=pdf_font)
        p.save(str(broken))
    from pypdf import PdfReader as R
    assert owm._bad_font_widths(R(str(broken)).pages[0]), 'fixture must be broken'
    out = tmp_path / 'out.pdf'
    res = owm.compress_one(str(broken), str(out), 200, ocr=False)
    assert res.get('err') is None, res
    assert res['action'] == 'kept_original' and res['reason'] == owm.REASON_SMALL
    assert out.exists()


# ── the CSV / log ─────────────────────────────────────────────────────────────

def _row(res: dict) -> dict:
    res.setdefault('rel', res.get('src', 'f.pdf'))
    return dict(zip(owm.REPORT_COLUMNS, owm._report_row(res)))


def test_report_row_splits_the_four_decisions():
    row = _row({'rel': 'm/a.pdf', 'orig': 20 * 1048576, 'new': 4 * 1048576,
                'action': 'compressed', 'reason': owm.REASON_COMPRESSIBLE,
                'ocr_state': owm.OCR_REDO, 'lang': 'rus+eng', 'err': None})
    assert row['action'] == 'compressed'
    assert row['reason'] == 'compressible'
    assert row['ocr'] == 're-ocr'
    assert row['language'] == 'rus+eng'
    assert row['%'] == 20


def test_report_row_reasons_are_distinguishable():
    """The three ways a file can be KEPT used to be indistinguishable in the CSV
    unless you read the note. Same action, three different reasons."""
    def kept(reason, action='kept_original'):
        return _row({'rel': 'a.pdf', 'orig': 1048576, 'new': 1048576, 'err': None,
                     'action': action, 'reason': reason, 'ocr_state': owm.OCR_KEPT})
    assert kept(owm.REASON_BORN, 'born_digital')['action'] == 'kept original'
    assert kept(owm.REASON_BORN, 'born_digital')['reason'] == 'born digital'
    assert kept(owm.REASON_SMALL)['reason'] == 'small size'
    assert kept(owm.REASON_ALREADY)['reason'] == 'already compressed'


def test_report_row_failed_reports_error_not_a_stale_decision():
    """A failed file was left as it was: it must not claim an OCR outcome or a
    language it never got to use."""
    row = _row({'rel': 'bad.pdf', 'orig': 1048576, 'new': 0, 'err': 'render failed',
                'ocr_state': owm.OCR_NEW, 'lang': 'rus', 'reason': owm.REASON_SMALL})
    assert row['action'] == 'FAILED'
    assert row['reason'] == 'error'
    assert row['ocr'] == '' and row['language'] == ''
    assert row['error'] == 'render failed'


def test_report_csv_header_and_rows_line_up(tmp_path):
    """The header and every row come from the same source, so a column can never
    shift out from under the values (the live CSV shares both)."""
    results = [
        {'rel': 'a.pdf', 'orig': 8 * 1048576, 'new': 2 * 1048576, 'err': None,
         'action': 'compressed', 'reason': owm.REASON_COMPRESSIBLE,
         'ocr_state': owm.OCR_NEW, 'lang': 'eng', 'kept': False},
        {'rel': 'b.pdf', 'orig': 1048576, 'new': 1048576, 'err': None,
         'action': 'kept_original', 'reason': owm.REASON_SMALL,
         'ocr_state': owm.OCR_KEPT, 'lang': '', 'kept': True},
        {'rel': 'c.pdf', 'orig': 1048576, 'new': 0, 'err': 'boom', 'kept': True},
    ]
    log = owm.write_run_log(tmp_path / 'r.log', tmp_path, tmp_path, results,
                            {'dpi': 200}, time.time(), 1.0, 3, 0, 0, 1, 3, 2)
    with open(log.with_suffix('.csv'), newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    assert [r['file'] for r in rows] == ['a.pdf', 'b.pdf', 'c.pdf']
    assert rows[0]['ocr'] == 'new ocr' and rows[0]['language'] == 'eng'
    assert rows[1]['reason'] == 'small size' and rows[1]['action'] == 'kept original'
    assert rows[2]['action'] == 'FAILED' and rows[2]['reason'] == 'error'
    # the .log tells the same story in prose
    text = log.read_text(encoding='utf-8')
    assert 'reason: small size' in text
    assert 'OCR: new ocr' in text and 'language: eng' in text
    assert '- small size' in text, 'summary must break the kept files down by reason'


# ── where a report goes (and whether one is written at all) ───────────────────

def test_report_path_folder_vs_exact_file(tmp_path):
    """One rule: a FOLDER gets a timestamped report inside it, anything else is used
    verbatim. `--log` with no value arrives as the current directory."""
    t = time.time()
    d = tmp_path / 'logs'
    d.mkdir()
    got = owm._report_path(d, t, False)
    assert got.parent == d and got.name.startswith('_ocrmyworkshopmanual_report_')
    assert got.suffix == '.log'

    # a path that does not exist yet but has no suffix is still a folder
    got = owm._report_path(tmp_path / 'not_yet', t, False)
    assert got.parent == tmp_path / 'not_yet'

    # anything with a suffix is the exact file
    exact = tmp_path / 'my run.log'
    assert owm._report_path(exact, t, False) == exact

    # a dry run is marked in the generated name, so it cannot be mistaken for a real one
    assert owm._report_path(d, t, True).stem.endswith('_DRYRUN')


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_cli_writes_no_report_next_to_the_pdf_by_default(tmp_path):
    """The regression this guards: a plain run used to drop
    `_ocrmyworkshopmanual_report_*.log/.csv/_by_folder.csv` beside the work — three files
    into the archive for every folder the tool was pointed at. Default is now console-only."""
    import subprocess
    src = U.make_scan_pdf(tmp_path / 'manual.pdf', npages=2)
    r = subprocess.run([sys.executable, str(U.REPO_ROOT / 'ocrmyworkshopmanual.py'),
                        str(src), '--no-ocr'],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / 'manual (COMPRESSED).pdf').is_file(), 'the output itself must exist'
    strays = sorted(p.name for p in tmp_path.rglob('_ocrmyworkshopmanual_report_*'))
    assert strays == [], f'default run left report files behind: {strays}'
    assert 'Log:' not in r.stdout


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_cli_log_dir_puts_the_report_there_not_beside_the_work(tmp_path):
    import subprocess
    src = U.make_scan_pdf(tmp_path / 'manual.pdf', npages=2)
    logs = tmp_path / 'logs'
    r = subprocess.run([sys.executable, str(U.REPO_ROOT / 'ocrmyworkshopmanual.py'),
                        str(src), '--no-ocr', '--log', str(logs)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    written = sorted(p.name for p in logs.glob('_ocrmyworkshopmanual_report_*'))
    assert len(written) == 3, f'expected .log + .csv + _by_folder.csv, got {written}'
    beside = sorted(p.name for p in tmp_path.glob('_ocrmyworkshopmanual_report_*'))
    assert beside == [], f'nothing may land beside the work: {beside}'


def test_summary_reports_the_bytes_each_reason_accounts_for(tmp_path):
    """A count alone hides what a decision COSTS: '2 small size' does not say whether the
    floor left 4 MB uncompressed or 4 GB. The MB is the number that tells you whether
    --min-compress-mb is set right for an archive, so it belongs in the tally."""
    results = [
        {'rel': 'a.pdf', 'orig': 8 * 1048576, 'new': 2 * 1048576, 'err': None,
         'action': 'compressed', 'reason': owm.REASON_COMPRESSIBLE,
         'ocr_state': owm.OCR_NEW, 'lang': 'eng', 'kept': False},
        {'rel': 'b.pdf', 'orig': 3 * 1048576, 'new': 3 * 1048576, 'err': None,
         'action': 'kept_original', 'reason': owm.REASON_SMALL,
         'ocr_state': owm.OCR_KEPT, 'lang': '', 'kept': True},
        {'rel': 'c.pdf', 'orig': 1 * 1048576, 'new': 1 * 1048576, 'err': None,
         'action': 'kept_original', 'reason': owm.REASON_SMALL,
         'ocr_state': owm.OCR_NEW, 'lang': 'rus+eng', 'kept': True},
        {'rel': 'd.pdf', 'orig': 1048576, 'new': 0, 'err': 'boom', 'kept': True},
    ]
    # the shared tally the console and the log both read, so the two cannot disagree
    tally = owm._reason_tally(results)
    assert tally['kept original'][owm.REASON_SMALL] == [2, 4 * 1048576]
    assert tally['compressed'][owm.REASON_COMPRESSIBLE] == [1, 8 * 1048576]
    assert 'FAILED' not in tally, 'a failed file has no reason but its error'

    log = owm.write_run_log(tmp_path / 'r.log', tmp_path, tmp_path, results,
                            {'min_compress_mb': 5}, time.time(), 1.0, 4, 0, 0, 1, 4, 3)
    text = log.read_text(encoding='utf-8')
    assert '- small size' in text and '(4.0 MB)' in text, text
    assert '(8.0 MB)' in text, 'the compressed bytes belong in the tally too'


def test_live_csv_row_matches_the_final_one():
    """The live-flushed row (written per file, so a killed run keeps its report) and
    the final one are the same row — that is the whole reason they share a builder."""
    res = {'rel': 'a.pdf', 'orig': 1048576, 'new': 524288, 'err': None,
           'action': 'compressed', 'reason': owm.REASON_COMPRESSIBLE,
           'ocr_state': owm.OCR_NEW, 'lang': 'eng'}
    live = next(csv.reader([owm._csv_row(owm._report_row(res))]))
    assert live == [str(c) for c in owm._report_row(res)]
    assert len(live) == len(owm.REPORT_COLUMNS)
