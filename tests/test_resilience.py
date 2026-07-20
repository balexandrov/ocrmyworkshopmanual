"""Tests for the resilience / ease-of-use additions:
  - per-step timeout aborts a file gracefully (never hangs the batch)
  - output verification flags a wrong page count / unopenable output
  - dry-run preview_one predicts the action and writes nothing
"""
import shutil

import pytest

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
