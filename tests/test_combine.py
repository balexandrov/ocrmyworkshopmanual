"""Tests for combining a split manual into one PDF per section.

These guard an IRREVERSIBLE step: helpers/combine_sections.py deletes the source folder
once the combined PDF passes verification, so a bug here loses pages permanently. Every
check below corresponds to something that actually went wrong on the real archive.
"""
import shutil
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

import _util as U

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'helpers'))
import combine_manual as CM          # noqa: E402
import combine_sections as CS        # noqa: E402


def _pdf(path: Path, npages: int = 1) -> Path:
    """A tiny valid PDF with `npages` pages, each carrying text."""
    return U.make_born_digital_pdf(path, npages=npages, lines_per_page=3)


# ── ordering ──────────────────────────────────────────────────────────────────

def test_recursive_order_interleaves_files_and_subfolders(tmp_path):
    """At every level, files and subfolders are ordered by ONE natural key — so a
    subfolder takes its pages' place in the sequence instead of being appended last.
    This is the real shape of GENERAL INFORMATION SECTION: numbered intro pages, then a
    PERIODIC MAINTENANCE subfolder that continues them."""
    sec = tmp_path / 'GENERAL INFORMATION SECTION'
    sec.mkdir()
    for n in (1, 2, 10):                       # 10 must sort after 2, not after 1
        _pdf(sec / f'{n}. Intro.pdf')
    sub = sec / 'PERIODIC MAINTENANCE SERVICES PM'
    sub.mkdir()
    for n in (1, 2, 9, 10):
        _pdf(sub / f'{n}. Topic.pdf')
    _pdf(sec / '0. Cover.pdf')

    rel = [str(p.relative_to(sec)) for p in CM.collect(sec, recursive=True)]
    assert rel == ['0. Cover.pdf', '1. Intro.pdf', '2. Intro.pdf', '10. Intro.pdf',
                   str(Path('PERIODIC MAINTENANCE SERVICES PM') / '1. Topic.pdf'),
                   str(Path('PERIODIC MAINTENANCE SERVICES PM') / '2. Topic.pdf'),
                   str(Path('PERIODIC MAINTENANCE SERVICES PM') / '9. Topic.pdf'),
                   str(Path('PERIODIC MAINTENANCE SERVICES PM') / '10. Topic.pdf')], rel


def test_non_recursive_still_ignores_subfolders(tmp_path):
    """The default must not change: only direct files, so an existing caller that points
    at a folder with a `*_files` HTML asset dir keeps behaving the same."""
    d = tmp_path / 'f'
    d.mkdir()
    _pdf(d / 'a.pdf')
    (d / 'a_files').mkdir()
    _pdf(d / 'a_files' / 'junk.pdf')
    assert [p.name for p in CM.collect(d)] == ['a.pdf']
    assert len(CM.collect(d, recursive=True)) == 2


# ── a PDF is a PDF whatever it is called ──────────────────────────────────────

def test_extensionless_pdf_is_collected(tmp_path):
    """A real page in this archive is a 1-page PDF stored as a file named `null`, with
    stray newlines before %PDF. Keying off the extension dropped it from its section —
    and the folder then gets deleted, so the page would have been gone for good."""
    d = tmp_path / 'Wiring Diagram Section'
    d.mkdir()
    real = _pdf(tmp_path / 'src.pdf')
    (d / 'null').write_bytes(b'\n\n\n\n\n' + real.read_bytes())
    _pdf(d / '1. Page.pdf')

    assert CM.looks_like_pdf(d / 'null')
    assert CM.is_pdf(d / 'null')
    names = [p.name for p in CM.collect(d, recursive=True)]
    assert 'null' in names, names
    # and it is counted as a PDF's worth of pages, not as one image page
    want, bad = CM.expected_pages(CM.collect(d, recursive=True))
    assert not bad and want == 2 * CM.page_count(real)


def test_text_mentioning_pdf_is_not_collected(tmp_path):
    """The sniff must be a HEADER check, not a substring search — an FTP log or readme
    that merely contains '%PDF' is not a page."""
    d = tmp_path / 's'
    d.mkdir()
    (d / 'WS_FTP.LOG').write_bytes(b'transferred %PDF-1.4 files ok\n')
    assert not CM.looks_like_pdf(d / 'WS_FTP.LOG')
    assert CM.collect(d, recursive=True) == []


# ── verification is what makes deleting the source safe ───────────────────────

def test_combine_verifies_page_count(tmp_path):
    d = tmp_path / 's'
    d.mkdir()
    a, b = _pdf(d / '1.pdf', npages=3), _pdf(d / '2.pdf', npages=2)
    out = tmp_path / 'out.pdf'
    pages = CM.combine(CM.collect(d), out)
    assert pages == 5 == len(PdfReader(str(out)).pages)
    assert CM.page_count(a) + CM.page_count(b) == 5


def test_combine_refuses_unreadable_input_and_leaves_no_output(tmp_path):
    """An unreadable part must abort the merge, not be skipped: a short PDF looks exactly
    like a complete one, and the source folder would then be deleted on its strength."""
    d = tmp_path / 's'
    d.mkdir()
    _pdf(d / '1.pdf', npages=2)
    (d / '2.pdf').write_bytes(b'this is not a pdf at all')
    out = tmp_path / 'out.pdf'
    with pytest.raises(CM.CombineFailed):
        CM.combine(CM.collect(d), out)
    assert not out.exists(), 'a refused merge must leave no output behind'
    assert not list(tmp_path.glob('*.part')), 'no half-written staging file either'


def test_combine_detects_page_loss(tmp_path, monkeypatch):
    """If the merge itself silently drops a page, the count check catches it. Simulated by
    lying about the expected total — the same arithmetic that catches a real short merge."""
    d = tmp_path / 's'
    d.mkdir()
    _pdf(d / '1.pdf', npages=2)
    files = CM.collect(d)
    monkeypatch.setattr(CM, 'expected_pages', lambda fs: (99, []))
    with pytest.raises(CM.CombineFailed, match='page loss'):
        CM.combine(files, tmp_path / 'out.pdf')
    assert not (tmp_path / 'out.pdf').exists()


# ── publisher order for a printed-from-the-web manual ─────────────────────────

def _printed(path: Path, group: str, docid: str, title: str = 'Topic') -> Path:
    """A part that looks like a browser print-to-PDF capture: the page-1 header carries
    the source URL, which is the only surviving record of the publisher's order."""
    return U.make_born_digital_pdf(
        path, npages=2, lines_per_page=3,
        header=f'1/6/23, 10:57 PM {title}\n'
               f'https://example.com/data/DG/2022/{group}/HTML/{docid}.htm 1/2')


def test_docid_key_reads_the_print_header(tmp_path):
    p = _printed(tmp_path / 'DTC Index.pdf', '06', 'N5060302G0000900USA')
    assert CM.docid_key(p) == ('0006', 'N5060302G0000900USA')
    assert CM.docid_key(_pdf(tmp_path / 'plain.pdf')) is None


def test_docid_order_beats_alphabetical(tmp_path):
    """The real case: parts named by topic, so a filename sort puts the diagnostics tooling
    ahead of the DTC index. The doc ids restore the publisher's sequence."""
    d = tmp_path / 'AT'
    d.mkdir()
    _printed(d / 'CONSULT Function.pdf', '04', 'N5040208L0000500USA')
    _printed(d / 'Component Description.pdf', '04', 'N5040200P0000300USA')
    _printed(d / 'DTC Index.pdf', '04', 'N5040201F0000500USA')
    _printed(d / 'Reference Value.pdf', '04', 'N5040208N0000500USA')

    alpha = [p.name for p in CM.collect(d)]
    assert alpha == ['Component Description.pdf', 'CONSULT Function.pdf',
                     'DTC Index.pdf', 'Reference Value.pdf'], alpha

    ordered, missing = CM.order_by_docid(CM.collect(d))
    assert missing == 0
    assert [p.name for p in ordered] == ['Component Description.pdf', 'DTC Index.pdf',
                                         'CONSULT Function.pdf', 'Reference Value.pdf']


def test_docid_order_is_all_or_nothing(tmp_path):
    """One part without a doc id must NOT be shuffled to the end — the whole section keeps
    natural order instead. A half-publisher, half-alphabetical sequence cannot be reviewed,
    and nothing in the result would say which half you are looking at."""
    d = tmp_path / 'S'
    d.mkdir()
    _printed(d / 'b.pdf', '04', 'N5040201F0000500USA')
    _printed(d / 'a.pdf', '04', 'N5040208N0000500USA')   # doc id would sort it AFTER b
    _pdf(d / 'c.pdf')                                    # no doc id at all

    natural = CM.collect(d)
    ordered, missing = CM.order_by_docid(natural)
    assert missing == 1
    assert ordered == natural, 'a partial doc-id set must leave the order untouched'


def test_section_records_which_order_it_used(tmp_path):
    """The row has to say whether publisher order was applied, or a reviewer cannot tell a
    correctly-ordered manual from a fallback."""
    root = tmp_path / 'M'
    sec = root / 'AT'
    sec.mkdir(parents=True)
    _printed(sec / 'z_later.pdf', '04', 'N5040200P0000300USA')   # doc id puts it FIRST
    _printed(sec / 'a_first.pdf', '04', 'N5040208N0000500USA')

    row = CS.process_section(sec, root, delete=False, dry=False, order='docid')
    assert row['status'] == 'OK', row
    assert row['order'] == 'docid' and row['docid_missing'] == 0
    # the pages really came out in doc-id order, not filename order: each part carries its
    # own URL on its first page, so the combined page 1 identifies which part led
    out = PdfReader(str(root / 'AT.pdf'))
    assert len(out.pages) == 4
    first = out.pages[0].extract_text() or ''
    assert 'N5040200P0000300USA' in first, (
        f'expected z_later.pdf (lowest doc id) first, got: {first[:200]!r}')

    # a section with a bare part falls back, and says so
    sec2 = root / 'BR'
    sec2.mkdir()
    _printed(sec2 / 'a.pdf', '04', 'N5040200P0000300USA')
    _pdf(sec2 / 'b.pdf')
    row2 = CS.process_section(sec2, root, delete=False, dry=False, order='docid')
    assert row2['status'] == 'OK', row2
    assert row2['order'] == 'natural' and row2['docid_missing'] == 1
    assert 'doc-id order was NOT applied' in row2['detail']


def test_default_order_is_unchanged(tmp_path):
    """`--order natural` (the default) must behave exactly as before."""
    d = tmp_path / 'S'
    d.mkdir()
    for n in (1, 2, 10):
        _printed(d / f'{n}. Part.pdf', '04', f'N504020{n}F0000500USA')
    root = d.parent
    row = CS.process_section(d, root, delete=False, dry=False)      # no order= given
    assert row['order'] == 'natural' and row['docid_missing'] == ''
    assert [p.name for p in CM.collect(d)] == ['1. Part.pdf', '2. Part.pdf', '10. Part.pdf']


# ── the size ratio is a proxy; word recall is the real check ──────────────────

def test_word_recall_is_one_for_a_faithful_merge(tmp_path):
    d = tmp_path / 's'
    d.mkdir()
    a, b = _pdf(d / '1.pdf', npages=2), _pdf(d / '2.pdf', npages=3)
    out = tmp_path / 'out.pdf'
    CM.combine(CM.collect(d), out)
    assert CS.word_recall([a, b], out) == pytest.approx(1.0)


def test_a_small_merge_ships_when_every_word_survives(tmp_path, monkeypatch):
    """A manual captured by PRINTING an online one merges to ~0.83 of its input bytes with
    nothing missing — each part carries its own catalogue/metadata/xref and pypdf writes the
    merge compactly. Measured on a real Outlander section: ratio 0.829, word recall 1.0000,
    all 60 images and 13 font programs intact. Refusing that on size alone threw away three
    perfect merges, so the size proxy must defer to the content check."""
    root = tmp_path / 'M'
    sec = root / 'ABS'
    sec.mkdir(parents=True)
    _pdf(sec / '1.pdf', npages=2)
    _pdf(sec / '2.pdf', npages=2)
    # force the proxy to trip, exactly as the real print-captures do
    monkeypatch.setattr(CS, 'MIN_SIZE_RATIO', 1.5)

    row = CS.process_section(sec, root, delete=True, dry=False)
    assert row['status'] == 'OK', row
    assert row['word_recall'] == pytest.approx(1.0)
    assert 'nothing lost' in row['detail']
    assert row['deleted'] == 'yes' and not sec.exists()


def test_a_small_merge_with_missing_words_still_fails(tmp_path, monkeypatch):
    """The other direction: when the bytes are short AND the words are gone, it is real loss
    and the folder must survive."""
    root = tmp_path / 'M'
    sec = root / 'S'
    sec.mkdir(parents=True)
    _pdf(sec / '1.pdf', npages=2)
    _pdf(sec / '2.pdf', npages=2)
    monkeypatch.setattr(CS, 'MIN_SIZE_RATIO', 1.5)
    monkeypatch.setattr(CS, 'word_recall', lambda inputs, out: 0.40)

    row = CS.process_section(sec, root, delete=True, dry=False)
    assert row['status'] == 'FAILED', row
    assert 'content is missing' in row['detail']
    assert sec.exists() and row['deleted'] == 'no'
    assert not (root / 'S.pdf').exists(), 'a refused merge leaves no output'


# ── a repair must not be taken at face value ──────────────────────────────────

def test_garbage_is_not_repaired_into_an_invented_page(tmp_path):
    """Ghostscript emits a one-page PDF from `%PDF-1.4 but truncated garbage`. There are no
    `/Type /Page` objects in those bytes and no object streams to hide them in, so the count
    of 0 is trustworthy: nothing survives to recover, and accepting the salvage would append
    an INVENTED page to the manual. Must be reported unrecoverable instead."""
    p = tmp_path / 'junk.pdf'
    p.write_bytes(b'%PDF-1.4 but truncated garbage')
    assert CM.page_count(p) < 0
    assert CM.raw_pages(p) == (0, True), 'a 0 count with no /ObjStm is trustworthy'

    _files, reps = CM.repair_inputs([p], tmp_path / 'w', base=tmp_path)
    assert [r.status for r in reps] == ['failed'], reps


def test_objstm_pdf_keeps_the_lenient_path(tmp_path):
    """A PDF that stores its page dicts in compressed object streams shows none in the
    clear, so a 0 count there means 'cannot tell' — it must NOT be treated as 'no pages',
    or a repairable file would be refused."""
    p = tmp_path / 'objstm.pdf'
    p.write_bytes(b'%PDF-1.5\n5 0 obj << /Type /ObjStm /N 3 >> stream\n...\nendstream\n')
    assert CM.raw_pages(p) == (0, False), 'a 0 count with /ObjStm is not trustworthy'


def test_pages_in_raw_bytes_counts_page_objects_not_the_tree(tmp_path):
    """`/Type /Pages` is the tree node, not a page — counting it would inflate the
    expectation and make the partial-salvage check reject sound repairs."""
    p = tmp_path / 'x.pdf'
    p.write_bytes(b'%PDF-1.4\n/Type /Pages /Count 3\n/Type /Page\n/Type/Page\n'
                  b'/Type /Page\n')
    assert CM.pages_in_raw_bytes(p) == 3


def test_partial_salvage_is_refused_not_silently_accepted(tmp_path, monkeypatch):
    """The hole this closes: `expected_pages` runs on the files handed to the merge, so a
    repair that salvaged 1 page of 9 agrees with itself and passes — and the source folder
    is then deleted. Measured on a real Baja section: three truncated parts holding 3, 9
    and 6 page objects, one page salvaged from each, ~15 pages lost silently.

    A repair that cannot recover the raw-byte page count must leave the part unreadable so
    the merge refuses the whole section."""
    root = tmp_path / 'M'
    sec = root / 'Transmission Section'
    sec.mkdir(parents=True)
    _pdf(sec / '1. Good.pdf', npages=2)
    # a part that looks like it holds 9 pages but cannot be parsed
    (sec / '2. Truncated.pdf').write_bytes(
        b'%PDF-1.4\n' + b'/Type /Page\n' * 9 + b'\x00\x01truncated mid-stream')
    assert CM.page_count(sec / '2. Truncated.pdf') < 0
    assert CM.pages_in_raw_bytes(sec / '2. Truncated.pdf') == 9

    # a repair tool that salvages only ONE page must not be believed
    import ocrmyworkshopmanual as owm

    def fake_repair(src, work, expect_pages=0, timeout=0, stats=None):
        out = Path(work) / 'salvaged.pdf'
        _pdf(out, npages=1)
        if expect_pages and 1 < expect_pages:
            return None          # this is what the real _repair_pdf does
        return out

    monkeypatch.setattr(owm, '_repair_pdf', fake_repair)
    row = CS.process_section(sec, root, delete=True, dry=False)
    assert row['status'] == 'FAILED', row
    assert sec.exists() and (sec / '1. Good.pdf').exists(), 'folder must be kept'
    assert not (root / 'Transmission Section.pdf').exists()


def test_full_recovery_is_accepted(tmp_path, monkeypatch):
    """The other direction: a repair that recovers every page lets the section through, so
    one malformed part does not strand a manual as un-combinable forever."""
    root = tmp_path / 'M'
    sec = root / 'S'
    sec.mkdir(parents=True)
    _pdf(sec / '1. Good.pdf', npages=2)
    (sec / '2. Broken.pdf').write_bytes(b'%PDF-1.4\n/Type /Page\n\x00 broken')
    assert CM.pages_in_raw_bytes(sec / '2. Broken.pdf') == 1

    import ocrmyworkshopmanual as owm

    def fake_repair(src, work, expect_pages=0, timeout=0, stats=None):
        out = Path(work) / 'salvaged.pdf'
        _pdf(out, npages=1)
        return out

    monkeypatch.setattr(owm, '_repair_pdf', fake_repair)
    row = CS.process_section(sec, root, delete=True, dry=False)
    assert row['status'] == 'OK', row
    assert row['pages'] == 3 and not sec.exists()
    assert 'repaired' in row['detail']


def test_broken_parts_are_named_by_path_not_filename(tmp_path, monkeypatch):
    """A bare filename cannot identify the culprit: one real section holds
    `Clutch System\\General Description.pdf` AND `Control Systems\\General
    Description.pdf`, and a report saying 'General Description.pdf' twice tells you
    nothing about which is broken."""
    root = tmp_path / 'M'
    sec = root / 'Transmission Section'
    for subname in ('Clutch System', 'Control Systems'):
        (sec / subname).mkdir(parents=True)
        (sec / subname / 'General Description.pdf').write_bytes(
            b'%PDF-1.4\n' + b'/Type /Page\n' * 4 + b'\x00 truncated')
    _pdf(sec / 'ok.pdf', npages=2)

    import ocrmyworkshopmanual as owm
    monkeypatch.setattr(owm, '_repair_pdf',
                        lambda src, work, expect_pages=0, timeout=0, stats=None: None)
    row = CS.process_section(sec, root, delete=False, dry=False)
    assert row['status'] == 'FAILED', row
    unrec = row['unrecoverable']
    assert str(Path('Clutch System') / 'General Description.pdf') in unrec, unrec
    assert str(Path('Control Systems') / 'General Description.pdf') in unrec, unrec
    assert unrec.count('General Description.pdf') == 2


def test_skip_unrecoverable_combines_the_rest_and_keeps_the_broken_parts(tmp_path,
                                                                        monkeypatch):
    """--skip-unrecoverable must not mean --lose-unrecoverable: the section is combined
    from what is readable, and the damaged originals are MOVED out of the folder before it
    is deleted, so a better tool can still be tried on them."""
    root = tmp_path / 'M'
    sec = root / 'Transmission Section'
    (sec / 'Clutch System').mkdir(parents=True)
    _pdf(sec / 'Clutch System' / 'Good.pdf', npages=3)
    (sec / 'Clutch System' / 'Broken.pdf').write_bytes(
        b'%PDF-1.4\n' + b'/Type /Page\n' * 9 + b'\x00 truncated')
    _pdf(sec / '1. Intro.pdf', npages=2)

    import ocrmyworkshopmanual as owm
    monkeypatch.setattr(owm, '_repair_pdf',
                        lambda src, work, expect_pages=0, timeout=0, stats=None: None)
    row = CS.process_section(sec, root, delete=True, dry=False, skip_broken=True)

    assert row['status'] == 'OK-PARTIAL', row
    assert row['pages'] == 5, 'only the readable pages are in the PDF'
    assert row['pages_dropped'] == 9
    assert str(Path('Clutch System') / 'Broken.pdf') in row['unrecoverable']
    assert row['deleted'] == 'yes' and not sec.exists()
    # the broken original survives, with its subfolder path intact
    kept = root / 'Transmission Section (UNRECOVERABLE)' / 'Clutch System' / 'Broken.pdf'
    assert kept.is_file(), 'a skipped part must never be destroyed'
    assert (root / 'Transmission Section.pdf').is_file()
    assert len(PdfReader(str(root / 'Transmission Section.pdf')).pages) == 5


def test_skip_unrecoverable_still_fails_when_nothing_is_readable(tmp_path, monkeypatch):
    """Skipping every input would produce an empty PDF and delete the folder — refuse."""
    root = tmp_path / 'M'
    sec = root / 'S'
    sec.mkdir(parents=True)
    for i in (1, 2):
        (sec / f'{i}.pdf').write_bytes(b'%PDF-1.4\n/Type /Page\n\x00 truncated')
    import ocrmyworkshopmanual as owm
    monkeypatch.setattr(owm, '_repair_pdf',
                        lambda src, work, expect_pages=0, timeout=0, stats=None: None)
    row = CS.process_section(sec, root, delete=True, dry=False, skip_broken=True)
    assert row['status'] == 'FAILED', row
    assert sec.exists() and row['deleted'] == 'no'
    assert not (root / 'S.pdf').exists()


def test_without_the_flag_a_broken_part_still_refuses_the_section(tmp_path, monkeypatch):
    """The default stays safe: skipping is opt-in, so an unattended run never silently
    ships a section with pages missing."""
    root = tmp_path / 'M'
    sec = root / 'S'
    sec.mkdir(parents=True)
    _pdf(sec / 'good.pdf', npages=2)
    (sec / 'bad.pdf').write_bytes(b'%PDF-1.4\n/Type /Page\n\x00 truncated')
    import ocrmyworkshopmanual as owm
    monkeypatch.setattr(owm, '_repair_pdf',
                        lambda src, work, expect_pages=0, timeout=0, stats=None: None)
    row = CS.process_section(sec, root, delete=True, dry=False)   # skip_broken defaults off
    assert row['status'] == 'FAILED' and sec.exists()
    assert not (root / 'S (UNRECOVERABLE)').exists(), 'nothing moved without the flag'


def test_repair_reports_how_much_is_salvageable(tmp_path, monkeypatch):
    """'recovered 1 of ~9 pages' is what tells you whether a file is worth chasing, so a
    failed strict repair is retried leniently purely to measure that."""
    d = tmp_path / 's'
    d.mkdir()
    (d / 'broken.pdf').write_bytes(b'%PDF-1.4\n' + b'/Type /Page\n' * 9 + b'\x00 trunc')

    import ocrmyworkshopmanual as owm

    def fake_repair(src, work, expect_pages=0, timeout=0, stats=None):
        if expect_pages and expect_pages > 1:
            return None                      # strict attempt fails
        out = Path(work) / 's.pdf'           # lenient attempt salvages one page
        _pdf(out, npages=1)
        return out

    monkeypatch.setattr(owm, '_repair_pdf', fake_repair)
    _files, reps = CM.repair_inputs(CM.collect(d), tmp_path / 'w', base=d)
    assert len(reps) == 1
    r = reps[0]
    assert r.status == 'incomplete'
    assert (r.recovered, r.expected) == (1, 9)
    assert str(r.rel) == 'broken.pdf'


# ── a folder that contains a merge of itself ──────────────────────────────────

def test_self_combined_copy_is_ignored(tmp_path):
    """WIRING DIAGRAM SECTION holds nine parts in subfolders AND a Wiring_diagram.pdf that
    is those same pages already merged. Combining everything would emit every page twice.
    Detected by EXACT page-count identity with the subfolders' total."""
    sec = tmp_path / 'WIRING DIAGRAM SECTION'
    sub = sec / 'WIRING DIAGRAM'
    sub.mkdir(parents=True)
    p1 = _pdf(sub / '1.pdf', npages=2)
    p2 = _pdf(sub / '2.pdf', npages=3)
    merged = sec / 'Wiring_diagram.pdf'         # 5 pages == 2 + 3 in the subfolder
    w = PdfWriter()
    w.append(str(p1))
    w.append(str(p2))
    with open(merged, 'wb') as f:
        w.write(f)
    assert CM.page_count(merged) == 5

    files = CM.collect(sec, recursive=True)
    assert len(files) == 3
    kept, dropped = CS.drop_self_combined(sec, files)
    assert dropped == ['Wiring_diagram.pdf'], dropped
    assert CM.expected_pages(kept)[0] == 5, 'pages must not be double-counted'


def test_a_genuine_chapter_is_not_mistaken_for_a_self_copy(tmp_path):
    """The ratio heuristic tried first (>=40% of the subfolders' pages) flagged 31 real
    chapters — a 19-page `Pre-delivery Inspection.pdf` beside 43 pages of subfolder. Only
    exact identity may drop a file, or genuine pages vanish."""
    sec = tmp_path / 'GENERAL INFORMATION SECTION'
    sub = sec / 'PERIODIC MAINTENANCE'
    sub.mkdir(parents=True)
    _pdf(sub / '1.pdf', npages=6)                    # subfolders hold 6 pages
    _pdf(sec / '8. Pre-delivery Inspection.pdf', npages=4)   # 4/6 = 0.67, but genuine
    files = CM.collect(sec, recursive=True)
    kept, dropped = CS.drop_self_combined(sec, files)
    assert dropped == [], 'a real chapter must never be dropped'
    assert len(kept) == 2


# ── the driver's delete gate ──────────────────────────────────────────────────

def test_section_is_deleted_only_after_verification(tmp_path):
    root = tmp_path / 'USDM Manual FSM 2006'
    sec = root / 'BODY SECTION'
    sub = sec / 'AIRBAG SYSTEM AB'
    sub.mkdir(parents=True)
    _pdf(sub / '1. General Description.pdf', npages=2)
    _pdf(sub / '2. Airbag Connector.pdf', npages=3)
    _pdf(sec / '1. Intro.pdf', npages=1)

    row = CS.process_section(sec, root, delete=True, dry=False)
    assert row['status'] == 'OK', row
    assert row['pages'] == 6 and row['deleted'] == 'yes'
    assert (root / 'BODY SECTION.pdf').is_file()
    assert not sec.exists(), 'a verified section folder should be gone'
    assert len(PdfReader(str(root / 'BODY SECTION.pdf')).pages) == 6


def test_failed_section_keeps_its_folder(tmp_path):
    """The whole safety property: nothing is deleted when verification fails."""
    root = tmp_path / 'M'
    sec = root / 'BAD SECTION'
    sec.mkdir(parents=True)
    _pdf(sec / '1.pdf', npages=2)
    (sec / '2.pdf').write_bytes(b'%PDF-1.4 but truncated garbage')

    row = CS.process_section(sec, root, delete=True, dry=False)
    assert row['status'] == 'FAILED', row
    assert row['deleted'] == 'no'
    assert sec.exists() and (sec / '1.pdf').exists()
    assert not (root / 'BAD SECTION.pdf').exists()


def test_existing_pdf_is_verified_not_blindly_skipped(tmp_path):
    """46 sections were combined by another tool in 2019. An existing output is checked
    against the folder: a faithful one means the folder is redundant (ALREADY, safe to
    delete); a mismatched one is a CONFLICT and nothing is touched."""
    root = tmp_path / 'M'
    sec = root / 'BODY SECTION'
    sec.mkdir(parents=True)
    p1 = _pdf(sec / '1.pdf', npages=2)
    p2 = _pdf(sec / '2.pdf', npages=3)

    good = root / 'BODY SECTION.pdf'
    w = PdfWriter()
    w.append(str(p1))
    w.append(str(p2))
    with open(good, 'wb') as f:
        w.write(f)
    row = CS.process_section(sec, root, delete=True, dry=False)
    assert row['status'] == 'ALREADY', row
    assert row['deleted'] == 'yes' and not sec.exists()

    # now the mismatched case — an existing PDF that is short
    sec2 = root / 'OTHER SECTION'
    sec2.mkdir()
    _pdf(sec2 / '1.pdf', npages=2)
    _pdf(sec2 / '2.pdf', npages=3)
    shutil.copyfile(str(_pdf(tmp_path / 'short.pdf', npages=1)),
                    str(root / 'OTHER SECTION.pdf'))
    row = CS.process_section(sec2, root, delete=True, dry=False)
    assert row['status'] == 'CONFLICT', row
    assert row['deleted'] == 'no' and sec2.exists(), 'a conflict must touch nothing'


def test_dry_run_writes_and_deletes_nothing(tmp_path):
    root = tmp_path / 'M'
    sec = root / 'S'
    sec.mkdir(parents=True)
    _pdf(sec / '1.pdf', npages=2)
    before = set(p.relative_to(tmp_path) for p in tmp_path.rglob('*'))
    row = CS.process_section(sec, root, delete=False, dry=True)
    assert row['status'] == 'WOULD-COMBINE' and row['pages'] == 2
    assert set(p.relative_to(tmp_path) for p in tmp_path.rglob('*')) == before
