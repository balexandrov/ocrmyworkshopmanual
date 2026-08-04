"""Lossless-rewrite tests (born-digital PDFs).

The lane's whole promise is: the file gets smaller, and NOTHING a page draws changes. So
these tests check both halves — that a rewrite actually pays off, and that the guard blocks
the output when it would not be faithful. Written against the two things measured on the
7,376-page Subaru manual that motivated the lane: 254.6 MB of unfiltered XMP, in TWO
families (typed `/Type /Metadata`, and untyped with no `/Type` at all) that a one-family
implementation silently half-handles.
"""
import zlib

import pytest

import _util as U

owm = U.owm
pikepdf = pytest.importorskip('pikepdf')


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _add_private_xmp(path, n_typed=3, n_untyped=2):
    """Attach per-illustration XMP the way Distiller does: marked-content property
    dictionaries under /Resources/Properties, each carrying a /Metadata packet. Typed
    packets go in UNFILTERED (which is exactly why they dominate a real file); untyped ones
    go in Flate-compressed, matching the older placed-EPS family."""
    packet = (b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
              b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
              b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
              b'<rdf:Description rdf:about=""><xmp:CreatorTool>Adobe Illustrator CS6'
              b'</xmp:CreatorTool></rdf:Description></rdf:RDF></x:xmpmeta>'
              + b' ' * 3000 + b'\n<?xpacket end="w"?>')
    with pikepdf.open(str(path), allow_overwriting_input=True) as p:
        for pg in p.pages:
            props = pikepdf.Dictionary()
            for i in range(n_typed):
                st = p.make_stream(packet)
                st.stream_dict['/Type'] = pikepdf.Name('/Metadata')
                st.stream_dict['/Subtype'] = pikepdf.Name('/XML')
                props[f'/MC{i}'] = p.make_indirect(
                    pikepdf.Dictionary(Metadata=p.make_indirect(st)))
            for i in range(n_untyped):
                st = p.make_stream(zlib.compress(packet, 9))
                st.stream_dict['/Filter'] = pikepdf.Name('/FlateDecode')
                props[f'/MU{i}'] = p.make_indirect(pikepdf.Dictionary(
                    Title='art%d.eps' % i, Creator='Adobe Illustrator(R) X',
                    Metadata=p.make_indirect(st)))
            res = pg.obj.get('/Resources')
            if res is None:
                res = pg.obj['/Resources'] = pikepdf.Dictionary()
            res['/Properties'] = props
        with p.open_metadata() as m:
            m['dc:title'] = 'document level packet - must survive'
        p.save(str(path) + '.tmp')
    import os
    os.replace(str(path) + '.tmp', str(path))
    return path


@pytest.fixture
def born(tmp_path):
    return U.make_born_digital_pdf(tmp_path / 'born.pdf', npages=4)


@pytest.fixture
def born_xmp(tmp_path):
    return _add_private_xmp(U.make_born_digital_pdf(tmp_path / 'xmp.pdf', npages=4))


def _decoded_pages(path):
    """Every page's decoded content bytes — what must never change."""
    with pikepdf.open(str(path)) as p:
        out = []
        for pg in p.pages:
            c = pg.obj.get('/Contents')
            out.append(b''.join(s.read_bytes()
                                for s in (c if isinstance(c, pikepdf.Array) else [c])))
        return out


# ── It pays off, and it changes nothing ──────────────────────────────────────

def test_rewrite_shrinks_and_preserves_page_content(born_xmp, tmp_path):
    out = tmp_path / 'out.pdf'
    before = _decoded_pages(born_xmp)
    res = owm.lossless_rewrite(born_xmp, out, min_savings=0.01)
    assert res['ok'], res
    assert out.stat().st_size < born_xmp.stat().st_size
    assert _decoded_pages(out) == before, 'page content stream bytes changed'


def test_both_xmp_families_are_removed_and_document_xmp_kept(born_xmp, tmp_path):
    """The bug this exists to prevent: handling only the typed family and silently
    leaving (or silently losing) the untyped one."""
    out = tmp_path / 'out.pdf'
    res = owm.lossless_rewrite(born_xmp, out, min_savings=0.01)
    assert res['ok'], res
    x = res['stats']['xmp']
    assert x['typed'] == 12 and x['untyped'] == 8, x      # 4 pages x (3 typed + 2 untyped)
    with pikepdf.open(str(out)) as p:
        assert p.Root.get('/Metadata') is not None, 'document-level XMP was dropped'
        with p.open_metadata() as m:
            assert 'document level packet' in str(m.get('dc:title', ''))
        for pg in p.pages:
            props = pg.obj['/Resources'].get('/Properties')
            assert props is not None, 'marked-content properties were dropped'
            # The CARRIER dicts must survive — the content stream names them via BDC.
            assert len(props.keys()) == 5, list(props.keys())
            for k in props.keys():
                assert '/Metadata' not in props[k].keys(), f'{k} kept its XMP'


def test_carrier_dict_keys_other_than_metadata_survive(born_xmp, tmp_path):
    """Only the /Metadata key is deleted, never the rest of the carrier."""
    out = tmp_path / 'out.pdf'
    assert owm.lossless_rewrite(born_xmp, out, min_savings=0.01)['ok']
    with pikepdf.open(str(out)) as p:
        props = p.pages[0].obj['/Resources']['/Properties']
        assert str(props['/MU0']['/Title']) == 'art0.eps'
        assert str(props['/MU0']['/Creator']) == 'Adobe Illustrator(R) X'


def test_keep_xmp_leaves_the_packets_in_place(born_xmp, tmp_path):
    out = tmp_path / 'out.pdf'
    res = owm.lossless_rewrite(born_xmp, out, strip_xmp=False, min_savings=0.01)
    assert res['ok'], res
    assert 'xmp' not in res['stats']
    with pikepdf.open(str(out)) as p:
        props = p.pages[0].obj['/Resources']['/Properties']
        assert '/Metadata' in props['/MC0'].keys(), 'XMP was stripped despite strip_xmp=False'


# ── The guard ────────────────────────────────────────────────────────────────

def test_rewrite_is_discarded_when_it_does_not_pay_off(born_xmp, tmp_path):
    """Not a failure — the expected outcome on a file with nothing to gain. Nothing may
    be written, so the caller's byte-for-byte copy is what lands."""
    out = tmp_path / 'out.pdf'
    res = owm.lossless_rewrite(born_xmp, out, min_savings=0.999)
    assert res['ok'] is False
    assert 'smaller' in res['skip'], res
    assert not out.exists(), 'a rejected rewrite still wrote its output'


def test_verify_catches_a_changed_document(tmp_path):
    """The fingerprint must actually discriminate — a guard that passes everything is
    worse than no guard, because it reads as proof."""
    a = U.make_born_digital_pdf(tmp_path / 'a.pdf', npages=4)
    b = U.make_born_digital_pdf(tmp_path / 'b.pdf', npages=5)
    base = owm._lossless_fingerprint(a)
    assert base is not None
    assert owm._lossless_verify(base, a) == ''
    bad = owm._lossless_verify(base, b)
    assert bad and 'page count' in bad, bad


def test_verify_catches_altered_page_content(born, tmp_path):
    """Same page count, same everything countable — different drawing. Must be caught."""
    base = owm._lossless_fingerprint(born)
    tampered = tmp_path / 'tampered.pdf'
    with pikepdf.open(str(born)) as p:
        pg = p.pages[0]
        pg.Contents = p.make_stream(pg.obj.Contents.read_bytes() + b'\nBT /F1 9 Tf 10 10 Td (x) Tj ET\n')
        p.save(str(tampered))
    bad = owm._lossless_verify(base, tampered)
    assert bad, 'a changed content stream passed verification'


def test_unreadable_baseline_skips_rather_than_passes(tmp_path):
    """A baseline that cannot be captured must SKIP the rewrite. It must never be treated
    as 'nothing to compare, so it passed' — that is how the text-survival guard became a
    silent no-op on sources pypdf could not open."""
    junk = tmp_path / 'junk.pdf'
    junk.write_bytes(b'%PDF-1.4\nnot really a pdf at all\n')
    assert owm._lossless_fingerprint(junk) is None
    res = owm.lossless_rewrite(junk, tmp_path / 'out.pdf')
    assert res['ok'] is False and 'baseline' in res['skip'], res
    assert not (tmp_path / 'out.pdf').exists()


def test_empty_user_password_pdf_is_decrypted_not_skipped(born, tmp_path):
    """An owner-permissions wrapper is not a lock. Measured across the archive's encrypted
    manuals (RC4-128 from Distiller 4): every one opens with an empty user password, and the
    encryption carries only extract/modify flags. Refusing them left ~450-650 MB unreachable
    for no safety gain, so the lane now opens them and writes the output in the clear."""
    enc = tmp_path / 'enc.pdf'
    out = tmp_path / 'out.pdf'
    with pikepdf.open(str(born)) as p:
        p.save(str(enc), encryption=pikepdf.Encryption(
            owner='o', user='', allow=pikepdf.Permissions(extract=False, modify_other=False)))
    before = _decoded_pages(enc)
    res = owm.lossless_rewrite(enc, out, min_savings=0.0)
    assert res['ok'] is True, res
    assert res['stats'].get('decrypted') is True, res
    assert 'DECRYPTED' in res['note'], res['note']
    with pikepdf.open(str(out)) as p:          # no password argument at all
        assert p.is_encrypted is False, 'output is still encrypted'
        assert p.allow.extract is True, 'permission flags were carried over, not dropped'
    assert _decoded_pages(out) == before, 'decrypting changed what a page draws'


def test_decrypted_rewrite_is_kept_even_when_barely_smaller(born, tmp_path):
    """The size bar does not apply to a file that shed its encryption. Half the archive's
    encrypted files come back under the 3% default; on the size bar alone they would stay
    encrypted over a rounding error. Bar for these is only 'not bigger than the source'."""
    enc = tmp_path / 'enc.pdf'
    out = tmp_path / 'out.pdf'
    with pikepdf.open(str(born)) as p:
        p.save(str(enc), encryption=pikepdf.Encryption(owner='o', user=''))
    res = owm.lossless_rewrite(enc, out, min_savings=0.99)   # no rewrite can beat 99%
    assert res['ok'] is True, res
    with pikepdf.open(str(out)) as p:
        assert p.is_encrypted is False
    # ...while an UNencrypted file under the same bar is still discarded.
    plain_out = tmp_path / 'plain_out.pdf'
    res2 = owm.lossless_rewrite(born, plain_out, min_savings=0.99)
    assert res2['ok'] is False and 'smaller' in res2['skip'], res2
    assert not plain_out.exists()


def test_unknown_password_is_reported_as_encrypted_not_as_corrupt(born, tmp_path):
    """A genuinely locked file must not be filed under 'baseline unreadable' — that reads as
    damage and sends you hunting for a corrupt scan that does not exist."""
    enc = tmp_path / 'locked.pdf'
    with pikepdf.open(str(born)) as p:
        p.save(str(enc), encryption=pikepdf.Encryption(owner='o', user='not-in-our-list'))
    res = owm.lossless_rewrite(enc, tmp_path / 'out.pdf', min_savings=0.0)
    assert res['ok'] is False, res
    assert 'encrypted' in res['skip'] and 'password' in res['skip'], res['skip']
    assert not (tmp_path / 'out.pdf').exists()


def test_vector_password_is_tried(born, tmp_path):
    """'vector' is the one non-empty password known to be used in this archive."""
    enc = tmp_path / 'vec.pdf'
    out = tmp_path / 'out.pdf'
    with pikepdf.open(str(born)) as p:
        p.save(str(enc), encryption=pikepdf.Encryption(owner='vector', user='vector'))
    res = owm.lossless_rewrite(enc, out, min_savings=0.0)
    assert res['ok'] is True, res
    with pikepdf.open(str(out)) as p:
        assert p.is_encrypted is False


def test_lossless_floor_label_does_not_round_a_fractional_floor_to_zero(born, tmp_path):
    """The gate was right and the label was wrong: a 0.1 MB floor printed as "under the 0 MB
    lossless floor", which reads as a broken comparison rather than a rounded number."""
    res = owm.compress_one(str(born), str(tmp_path / 'out.pdf'), 200, ocr=False,
                           min_compress_mb=5.0, lossless_min_mb=0.1)
    assert '0.1 MB lossless floor' in res['note'], res['note']


# ── Zopfli tier ──────────────────────────────────────────────────────────────

@pytest.mark.skipif(not owm._zopfli_available(), reason='zopfli not installed')
def test_zopfli_preserves_every_stream_exactly(born_xmp, tmp_path):
    out = tmp_path / 'out.pdf'
    before = _decoded_pages(born_xmp)
    res = owm.lossless_rewrite(born_xmp, out, zopfli=True, workers=2, min_savings=0.01)
    assert res['ok'], res
    assert _decoded_pages(out) == before
    z = res['stats']['zopfli']
    assert z['streams'] > 0 and z['shrunk'] + z['kept'] == z['streams'], z


# ── Wiring into a run ────────────────────────────────────────────────────────

def test_compress_one_rewrites_born_digital(born_xmp, tmp_path):
    dest = tmp_path / 'out' / 'x.pdf'
    before = _decoded_pages(born_xmp)
    res = owm.compress_one(str(born_xmp), str(dest), 200, ocr=False,
                           lossless_min_savings=0.01)
    assert res.get('err') is None, res
    assert res['action'] == 'compressed', res
    assert res['reason'] == owm.REASON_LOSSLESS, res
    assert dest.stat().st_size < born_xmp.stat().st_size
    assert _decoded_pages(dest) == before, 'a run changed page content'


def test_compress_one_falls_back_to_a_byte_copy(born_xmp, tmp_path):
    """When the rewrite is not kept, the file must still arrive — byte-for-byte — and the
    row must say why the rewrite was dropped."""
    dest = tmp_path / 'out' / 'x.pdf'
    res = owm.compress_one(str(born_xmp), str(dest), 200, ocr=False,
                           lossless_min_savings=0.999)
    assert res['action'] == 'born_digital', res
    assert res['reason'] == owm.REASON_BORN, res
    assert dest.read_bytes() == born_xmp.read_bytes()
    assert 'lossless rewrite not kept' in res['note'], res['note']


def test_in_place_rewrites_the_original_and_keeps_page_content(born_xmp):
    """In place is the one case where this path overwrites an original, so it has to be
    exactly as verified as the --dest case — and the file must still draw the same pages."""
    before_size = born_xmp.stat().st_size
    before = _decoded_pages(born_xmp)
    res = owm.compress_one(str(born_xmp), str(born_xmp), 200, ocr=False, in_place=True,
                           lossless_min_savings=0.01)
    assert res.get('err') is None, res
    assert res['action'] == 'compressed' and res['reason'] == owm.REASON_LOSSLESS, res
    assert born_xmp.stat().st_size < before_size
    assert _decoded_pages(born_xmp) == before, 'in-place rewrite changed page content'
    assert 'in place' in res['note'], res['note']


def test_in_place_leaves_the_original_byte_identical_when_not_kept(born_xmp):
    """The failure mode that matters most: a rewrite that is not adopted must leave the
    original untouched, not half-written."""
    before = born_xmp.read_bytes()
    res = owm.compress_one(str(born_xmp), str(born_xmp), 200, ocr=False, in_place=True,
                           lossless_min_savings=0.999)
    assert born_xmp.read_bytes() == before, 'a rejected in-place rewrite modified the original'
    assert res['action'] == 'born_digital', res
    assert 'lossless rewrite not kept' in res['note'], res['note']
    assert not list(born_xmp.parent.glob('*.lossless')), 'temp file left behind'


def test_in_place_with_no_lossless_leaves_the_original_alone(born_xmp):
    before = born_xmp.read_bytes()
    owm.compress_one(str(born_xmp), str(born_xmp), 200, ocr=False, in_place=True,
                     lossless=False)
    assert born_xmp.read_bytes() == before


def test_no_lossless_flag_disables_the_lane(born_xmp, tmp_path):
    dest = tmp_path / 'out' / 'x.pdf'
    res = owm.compress_one(str(born_xmp), str(dest), 200, ocr=False, lossless=False)
    assert res['action'] == 'born_digital', res
    assert dest.read_bytes() == born_xmp.read_bytes()
    assert 'lossless' not in res['note'], res['note']


# ── Preview ──────────────────────────────────────────────────────────────────

def test_signature_measures_what_a_rewrite_would_feed_on(born_xmp):
    sig = owm.lossless_signature(born_xmp)
    assert 'error' not in sig, sig
    assert sig['unfiltered'] > 0, sig       # the typed packets went in unfiltered
    assert sig['xmp'] > 0, sig
    assert sig['streams'] > 0, sig


def test_preview_predicts_a_rewrite(born_xmp):
    res = owm.preview_one(str(born_xmp), 200, True, 10, 0.02, 150, 60, 0.25, 0.30, 0.6,
                          ocr=False)
    assert res['action'] == 'born_digital', res
    assert 'lossless rewrite' in res['note'], res['note']


# ── --from-list --dest (the mode a born-digital pass needs) ──────────────────
#
# --from-list forced in-place and silently ignored --dest. Since the rewrite is deliberately
# never applied in place, an in-place run over a list of born-digital PDFs did nothing at all.

def test_from_list_with_dest_writes_a_mirror_tree(tmp_path):
    import subprocess
    import sys
    tree = tmp_path / 'src'
    (tree / 'a' / 'b').mkdir(parents=True)
    p1 = _add_private_xmp(U.make_born_digital_pdf(tree / 'a' / 'one.pdf', npages=3))
    p2 = _add_private_xmp(U.make_born_digital_pdf(tree / 'a' / 'b' / 'two.pdf', npages=3))
    lst = tmp_path / 'list.txt'
    lst.write_text(f'{p1}\n{p2}\n', encoding='utf-8')
    out = tmp_path / 'out'
    r = subprocess.run([sys.executable, str(U.REPO_ROOT / 'ocrmyworkshopmanual.py'),
                        '--from-list', str(lst), '--dest', str(out), '--no-ocr',
                        '--min-compress-mb', '0', '--lossless-min-savings', '0.01'],
                       capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr
    # originals untouched, results mirrored under --dest
    assert p1.exists() and p2.exists()
    # The mirror is keyed off the common base of the LISTED paths — here tree/a, since
    # both files are under it — not off any tree root the caller may have in mind.
    written = sorted(q.relative_to(out).as_posix() for q in out.rglob('*.pdf'))
    assert written == ['b/two.pdf', 'one.pdf'], written
    for q in out.rglob('*.pdf'):
        assert q.stat().st_size > 0


def test_from_list_in_place_rewrites_the_listed_originals(tmp_path):
    """The exact shape of the archive pass: a list of paths, no --dest, files replaced where
    they sit. Runs the real CLI so the from-list wiring is covered, not just compress_one."""
    import subprocess
    import sys
    tree = tmp_path / 'src'
    tree.mkdir()
    p1 = _add_private_xmp(U.make_born_digital_pdf(tree / 'one.pdf', npages=3))
    p2 = _add_private_xmp(U.make_born_digital_pdf(tree / 'two.pdf', npages=3))
    sizes = {p: p.stat().st_size for p in (p1, p2)}
    pages = {p: _decoded_pages(p) for p in (p1, p2)}
    lst = tmp_path / 'list.txt'
    lst.write_text(f'{p1}\n{p2}\n', encoding='utf-8')
    r = subprocess.run([sys.executable, str(U.REPO_ROOT / 'ocrmyworkshopmanual.py'),
                        '--from-list', str(lst), '--no-ocr',
                        '--min-compress-mb', '0', '--lossless-min-savings', '0.01'],
                       capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr
    for p in (p1, p2):
        assert p.stat().st_size < sizes[p], f'{p.name} was not rewritten'
        assert _decoded_pages(p) == pages[p], f'{p.name} page content changed'
    assert not list(tree.glob('*.lossless')) and not list(tree.glob('*.part'))


def test_size_floor_governs_the_lane_too(born_xmp, monkeypatch):
    """--min-compress-mb is where the user says "below this, don't bother", and it applies
    here as well: a rewrite saves a PERCENTAGE, so on a small file the absolute win is tens of
    KB — not worth rewriting (and under --in-place churning) 289,368 small archive files."""
    before = born_xmp.read_bytes()
    res = owm.compress_one(str(born_xmp), str(born_xmp), 200, ocr=False, in_place=True,
                           min_compress_mb=5.0, lossless_min_savings=0.01)
    assert born_xmp.read_bytes() == before, 'a file under the floor was rewritten anyway'
    assert res['action'] == 'born_digital', res
    assert 'under the 5 MB lossless floor' in res['note'], res['note']


def test_unparseable_document_xmp_is_reported_not_hidden(born, tmp_path):
    """Measured on 2014_Forester.pdf: /Root /Metadata held 3,360 bytes of binary with no
    xpacket marker, so pikepdf discarded it and wrote a valid empty packet. Comparing PARSED
    fields cannot see that (both sides yield zero fields), so it has to reach the note or the
    only change to the file nobody authorised goes unmentioned."""
    junk = tmp_path / 'junkxmp.pdf'
    with pikepdf.open(str(born)) as p:
        st = p.make_stream(bytes(range(256)) * 12)          # binary, definitely not XMP
        st.stream_dict['/Type'] = pikepdf.Name('/Metadata')
        st.stream_dict['/Subtype'] = pikepdf.Name('/XML')
        p.Root['/Metadata'] = p.make_indirect(st)
        # fix_metadata_version=False is what makes this fixture possible: pikepdf's DEFAULT
        # save already replaces an unparseable packet, which is precisely the behaviour under
        # test, so building the fixture with defaults would silently produce a valid one.
        p.save(str(junk), fix_metadata_version=False)
    base = owm._lossless_fingerprint(junk)
    assert base['xmp_ok'] is False, 'binary garbage was accepted as valid XMP'
    res = owm.lossless_rewrite(junk, tmp_path / 'out.pdf', min_savings=0.01)
    assert res['ok'], res
    assert res['stats'].get('xmp_unparseable') is True, res['stats']
    assert 'not valid XMP' in res['note'], res['note']
    # and a normal file must NOT carry that note
    ok = owm._lossless_fingerprint(born)
    assert ok['xmp_ok'] is True


def test_lossless_min_mb_lowers_only_the_lossless_floor(born_xmp):
    """The point of a separate floor: sweep small born-digital files WITHOUT lowering
    --min-compress-mb, which would also let the raster path re-image a small scan."""
    before = born_xmp.read_bytes()
    res = owm.compress_one(str(born_xmp), str(born_xmp), 200, ocr=False, in_place=True,
                           min_compress_mb=5.0, lossless_min_mb=0.0,
                           lossless_min_savings=0.01)
    assert res['reason'] == owm.REASON_LOSSLESS, res
    assert born_xmp.stat().st_size < len(before), 'the lowered lossless floor had no effect'
