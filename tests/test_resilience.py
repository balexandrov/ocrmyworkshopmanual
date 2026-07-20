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
    res = U.owm.preview_one(str(src), 200, True, 125, 10, False, True, 0.02, 150, 60,
                            0.25, True, 0.75, True, 0.30, True, 0.6, True, 0.5)
    assert res['action'] == 'born_digital' and res['err'] is None
    assert res['new'] == res['orig']                      # predicts no size change
    assert set(tmp_path.iterdir()) == before              # nothing written


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_preview_one_scanned_projects_smaller(tmp_path):
    pdfs = U.fixture_pdfs('line')
    if not pdfs:
        pytest.skip('no line fixtures')
    res = U.owm.preview_one(str(pdfs[0]), 200, True, 125, 10, False, True, 0.02, 150, 60,
                            0.25, True, 0.75, True, 0.30, True, 0.6, True, 0.5)
    assert res['err'] is None
    assert res['action'] in ('compressed', 'kept_original', 'ocr_only')
    assert res['new'] <= res['orig']


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_timeout_fails_gracefully(tmp_path):
    """A tiny timeout must abort the render as a clean FAILED result (no hang, no
    crash, no output file), so one pathological file never stalls a big batch."""
    pdfs = U.fixture_pdfs('photo_gray') or U.fixture_pdfs('line')
    if not pdfs:
        pytest.skip('no fixtures')
    dest = tmp_path / 'out' / 'x.pdf'
    res = U.owm.compress_one(str(pdfs[0]), str(dest), 200, ocr=False, precheck=False,
                             timeout=0.001)
    assert res.get('err') and 'timed out' in res['err'], res
    assert not dest.exists(), 'a timed-out file must not leave a dest output'


# ── config file / dedup / retry / repair ─────────────────────────────────────

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
