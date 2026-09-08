"""Tests for decrypting encrypted PDFs on the way in (default on, --no-decrypt opts out).

The interesting case is NOT the one you would guess. Lanes that re-save the PDF through
qpdf or ocrmypdf already drop the encryption as a side effect, so they were never broken.
The BYTE-COPY paths preserved it, and shipped a file whose text extraction and
accessibility were still withheld -- which is what these tests pin down.
"""
import shutil

import pikepdf
import pytest

import _util as U

owm = U.owm
_missing = U.tools_missing()


def _enc_state(path):
    """(is_encrypted, extraction_allowed) for a PDF."""
    with pikepdf.open(str(path)) as pdf:
        return pdf.is_encrypted, bool(pdf.allow.extract)


# -- the fixture really is shaped like the real publications ------------------

def test_fixture_is_openable_but_restricted(tmp_path):
    """RC4-128 /V 2 /R 3, empty user password: it opens with no password at all, and the
    only thing the encryption does is withhold extraction. That is the real shape."""
    p = U.make_born_digital_pdf(tmp_path / 'v.pdf', npages=2)
    U.encrypt_pdf_in_place(p)
    with pikepdf.open(str(p)) as pdf:          # no password argument -> opens
        assert pdf.is_encrypted
        assert pdf.encryption.R == 3 and pdf.encryption.V == 2
        assert not pdf.allow.extract and not pdf.allow.accessibility


# -- the unit ----------------------------------------------------------------

def test_decrypt_source_leaves_a_plain_file_alone(tmp_path):
    """Must be free on the normal path and safe to re-run over an already-done tree."""
    p = U.make_born_digital_pdf(tmp_path / 'plain.pdf', npages=2)
    out, did, err = owm._decrypt_source(p, tmp_path / 'work')
    assert out == p and did is False and err is None


def test_decrypt_source_decrypts_and_keeps_the_name(tmp_path):
    """The copy keeps the original filename, so every report row still reads correctly."""
    p = U.make_born_digital_pdf(tmp_path / 'locked.pdf', npages=3)
    U.encrypt_pdf_in_place(p)
    work = tmp_path / 'work'
    work.mkdir()
    out, did, err = owm._decrypt_source(p, work)
    assert did is True and err is None
    assert out != p and out.name == 'locked.pdf'
    assert _enc_state(out) == (False, True)
    with pikepdf.open(str(out)) as pdf:        # page count guard held
        assert len(pdf.pages) == 3
    assert _enc_state(p) == (True, False)      # the SOURCE is untouched


def test_decrypt_source_reports_an_unknown_password(tmp_path):
    """A genuinely locked file must be failed with a reason, not silently mangled."""
    p = U.make_born_digital_pdf(tmp_path / 'sealed.pdf', npages=2)
    U.encrypt_pdf_in_place(p, user='a-password-we-do-not-have')
    work = tmp_path / 'work'
    work.mkdir()
    out, did, err = owm._decrypt_source(p, work)
    assert did is False and out == p
    assert err and 'password' in err


def test_unreadable_file_is_left_to_the_repair_path(tmp_path):
    """Not this function's job to diagnose corruption -- it must not claim a decrypt error."""
    junk = tmp_path / 'junk.pdf'
    junk.write_bytes(b'not a pdf at all')
    out, did, err = owm._decrypt_source(junk, tmp_path / 'work')
    assert out == junk and did is False and err is None


# -- end to end: the byte-copy path is the one that was leaking ---------------

@pytest.mark.skipif(bool(_missing), reason=_missing or '')
def test_bytecopy_path_ships_decrypted_by_default(tmp_path):
    """A born-digital PDF is copied through untouched, which used to carry the encryption
    with it: the shipped file stayed restricted. Now it ships decrypted, and says so."""
    src = tmp_path / 'in'
    src.mkdir()
    p = U.make_born_digital_pdf(src / 'vector.pdf', npages=4)
    U.encrypt_pdf_in_place(p)
    dest = tmp_path / 'out'
    res = owm.compress_one(str(p), str(dest / 'vector.pdf'), 200, ocr=False)
    assert not res.get('err')
    assert _enc_state(dest / 'vector.pdf') == (False, True)
    assert 'decrypted' in res['note']          # never a silent semantic change


@pytest.mark.skipif(bool(_missing), reason=_missing or '')
def test_no_decrypt_preserves_the_encryption(tmp_path):
    """The opt-out has to actually opt out -- and this is the pre-fix behaviour."""
    src = tmp_path / 'in'
    src.mkdir()
    p = U.make_born_digital_pdf(src / 'vector.pdf', npages=4)
    U.encrypt_pdf_in_place(p)
    dest = tmp_path / 'out'
    res = owm.compress_one(str(p), str(dest / 'vector.pdf'), 200, ocr=False, decrypt=False)
    assert not res.get('err')
    assert _enc_state(dest / 'vector.pdf') == (True, False)
    assert 'decrypted' not in res['note']


@pytest.mark.skipif(bool(_missing), reason=_missing or '')
def test_a_plain_file_is_not_annotated_as_decrypted(tmp_path):
    """The note must appear only on files that really were encrypted."""
    src = tmp_path / 'in'
    src.mkdir()
    p = U.make_born_digital_pdf(src / 'plain.pdf', npages=3)
    dest = tmp_path / 'out'
    res = owm.compress_one(str(p), str(dest / 'plain.pdf'), 200, ocr=False)
    assert not res.get('err')
    assert 'decrypted' not in res['note']


@pytest.mark.skipif(bool(_missing) or bool(U.ocr_missing()),
                    reason=_missing or U.ocr_missing() or '')
def test_encrypted_scan_still_gets_its_text_layer(tmp_path):
    """The scan lanes were already fine (their re-save drops encryption); this pins that
    decrypting up front did not break them, and that the result is unrestricted."""
    src = tmp_path / 'in'
    src.mkdir()
    p = U.make_scan_pdf(src / 'scan.pdf', npages=2)
    U.encrypt_pdf_in_place(p)
    dest = tmp_path / 'out'
    res = owm.compress_one(str(p), str(dest / 'scan.pdf'), 200, ocr=True)
    assert not res.get('err')
    out = dest / 'scan.pdf'
    assert _enc_state(out) == (False, True)
    from pypdf import PdfReader
    r = PdfReader(str(out))
    assert len(r.pages) == 2
    assert sum(len(pg.extract_text() or '') for pg in r.pages) > 50


@pytest.mark.skipif(bool(_missing), reason=_missing or '')
def test_page_count_and_content_survive_decryption(tmp_path):
    """Decrypting drops permission flags and nothing else: a flag cannot change what a
    page draws, and the page count guard is what proves the save was not a rebuild."""
    src = tmp_path / 'in'
    src.mkdir()
    p = U.make_born_digital_pdf(src / 'v.pdf', npages=5)
    plain = tmp_path / 'reference.pdf'
    shutil.copyfile(p, plain)
    U.encrypt_pdf_in_place(p)
    dest = tmp_path / 'out'
    res = owm.compress_one(str(p), str(dest / 'v.pdf'), 200, ocr=False)
    assert not res.get('err')
    from pypdf import PdfReader
    before, after = PdfReader(str(plain)), PdfReader(str(dest / 'v.pdf'))
    assert len(after.pages) == len(before.pages) == 5
    assert (after.pages[0].extract_text() or '') == (before.pages[0].extract_text() or '')
