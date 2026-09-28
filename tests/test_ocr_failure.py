"""A file whose OCR produced no text layer FAILS — original kept, cause and options named.

It used to ship anyway. Measured: the OCR engine crashed (0xC0000005) on a 138-page
Japanese manual, and the run wrote the compressed file with no text layer and reported
"processed 1 ... failed 0" — only the `ocr` column said `failed`.
"""
import pytest

import _util as U

owm = U.owm
_missing = U.tools_missing()

CRASH_NOTE = ' (lang:eng) (OCR FAILED - exit 0xc0000005 (access violation: the OCR engine crashed) (no output file))'


def _broken_ocr(*a, **k):
    return None, 'eng', CRASH_NOTE


def _assert_failed(res, out):
    err = res.get('err') or ''
    assert err.startswith('OCR failed'), res
    assert 'access violation' in err, 'the cause must be in the row'
    assert 'original kept' in err
    assert '--retry-failed' in err and '--no-ocr' in err, 'the row must offer the way out'
    assert not out.exists(), 'nothing may be written for a file whose OCR failed'


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_compress_path_fails_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(owm, '_ocr_source', _broken_ocr)
    src = U.make_scan_pdf(tmp_path / 'src.pdf', npages=2)
    out = tmp_path / 'out.pdf'
    res = owm.compress_one(str(src), str(out), 200, ocr=True, language='eng')
    _assert_failed(res, out)


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_keep_original_path_fails_the_file(tmp_path, monkeypatch):
    """Under the size floor the original images ship — or did, without a text layer."""
    monkeypatch.setattr(owm, 'MIN_COMPRESS_MB', 5.0)
    monkeypatch.setattr(owm, '_ocr_source', _broken_ocr)
    src = U.make_scan_pdf(tmp_path / 'small.pdf', npages=1)
    out = tmp_path / 'out.pdf'
    res = owm.compress_one(str(src), str(out), 200, ocr=True, language='eng')
    _assert_failed(res, out)


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_in_place_leaves_the_original_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setattr(owm, '_ocr_source', _broken_ocr)
    src = U.make_scan_pdf(tmp_path / 'src.pdf', npages=2)
    before = src.read_bytes()
    res = owm.compress_one(str(src), str(src), 200, ocr=True, language='eng', in_place=True)
    assert (res.get('err') or '').startswith('OCR failed'), res
    assert src.read_bytes() == before


@pytest.mark.skipif(_missing is not None, reason=str(_missing))
def test_no_ocr_is_not_a_failure(tmp_path, monkeypatch):
    """--no-ocr is the offered way out, so it must still work on the same file."""
    monkeypatch.setattr(owm, '_ocr_source', _broken_ocr)
    src = U.make_scan_pdf(tmp_path / 'src.pdf', npages=2)
    res = owm.compress_one(str(src), str(tmp_path / 'out.pdf'), 200, ocr=False)
    assert res.get('err') is None and res['ocr_state'] == owm.OCR_NONE, res


def test_the_paddle_engine_offers_tesseract(monkeypatch):
    monkeypatch.setattr(owm, 'OCR_ENGINE', 'paddle')
    assert '--ocr-engine tesseract' in owm._ocr_failed_err(CRASH_NOTE)
    monkeypatch.setattr(owm, 'OCR_ENGINE', 'tesseract')
    assert '--ocr-engine tesseract' not in owm._ocr_failed_err(CRASH_NOTE)


def test_exit_codes_are_explained():
    assert '0xc0000005' in owm._exit_meaning(3221225477)
    assert 'access violation' in owm._exit_meaning(3221225477)
    assert 'access violation' in owm._exit_meaning(-1073741819)   # same code, signed
    assert 'child process' in owm._exit_meaning(7)
    assert 'SIGSEGV' in owm._exit_meaning(-11)
    assert owm._exit_meaning(42) == 'exit 42'
