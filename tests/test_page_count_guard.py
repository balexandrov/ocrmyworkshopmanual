"""The page-count guard must not be able to switch itself off.

THE BUG. Every downstream check is verified against the SOURCE page count, and that count
came from pypdf alone. On a 1,904-page ABBYY FineReader 9.0 manual (1987 Wrangler YJ)
pypdf raises AttributeError("'NullObject' object has no attribute 'get'") while pikepdf
reads it fine. The zero that produced took the guards down with it: the render page-loss
check was `if src_pages and ...`, `expect_pages` fell back to the RENDERED page count, and
`_audit_output` then compared a collapsed 1-page output against a 1 derived from the same
collapsed render. The run destroyed a 58.7 MB manual and reported "1 compressed, failed 0,
saved 56 MB".

Two things have to hold, and neither is about that one file:
  * the count is asked of both readers, so one library's intolerance cannot blind the tool
  * a count that genuinely cannot be determined FAILS the file -- unverifiable is not fine
"""
import pytest

import ocrmyworkshopmanual as owm
import _util as U


def test_the_count_falls_back_to_the_other_reader(tmp_path, monkeypatch):
    """pypdf gives up; the answer must still be right, not zero."""
    f = U.make_scan_pdf(tmp_path / 'scan.pdf', npages=5, dpi=72)
    assert owm._page_count(f) == 5

    class Dead:
        def __init__(self, *a, **k):
            raise AttributeError("'NullObject' object has no attribute 'get'")

    monkeypatch.setattr(owm, 'PdfReader', Dead)
    assert owm._page_count(f) == 5, 'pikepdf must answer when pypdf cannot'


def test_a_file_no_reader_can_open_counts_as_unknown(tmp_path):
    bad = tmp_path / 'junk.pdf'
    bad.write_bytes(b'%PDF-1.4\nthis is not a pdf at all\n')
    assert owm._page_count(bad) == 0


def test_an_unknown_expected_count_fails_the_output(tmp_path):
    """FAIL CLOSED. This is the assertion that would have saved the YJ manual: with no
    trustworthy expectation, shipping is not an option, however healthy the output looks.
    """
    good = U.make_scan_pdf(tmp_path / 'out.pdf', npages=3, dpi=72)
    fatal, _warn = owm._audit_output(good, 0)
    assert fatal and 'unknown' in fatal


def test_a_short_output_is_still_caught_when_the_count_is_known(tmp_path):
    short = U.make_scan_pdf(tmp_path / 'short.pdf', npages=1, dpi=72)
    fatal, _warn = owm._audit_output(short, 1904)
    assert fatal and '1904' in fatal


def test_a_matching_output_passes(tmp_path):
    ok = U.make_scan_pdf(tmp_path / 'ok.pdf', npages=4, dpi=72)
    fatal, _warn = owm._audit_output(ok, 4)
    assert not fatal
