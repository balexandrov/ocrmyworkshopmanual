"""Tests for `combine_manual.py`: which page of which chapter a loose file holds, and the
order the combined PDF puts them in.

Page ORDER is the whole point and it is not obvious — every check below corresponds to
something that actually went wrong on the real archive (an `EM11.jpg` sorting second, a
cover landing last, a capital O typed for a zero splitting one chapter into four).
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

import _util as U
import combine_manual as CM


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


# ── the page key ──────────────────────────────────────────────────────────────
#
# All of these come from one real folder: a 21-section Daihatsu manual of 1216 .jpg + 30 .tif
# whose pages a plain filename sort put in the wrong order in 14 of the 21 sections.

def test_page_key_keeps_the_documented_hyphen_order():
    """The order this module's own docstring promises. It is the reason the key marks a
    separator that sits BETWEEN TWO DIGITS instead of stripping it: strip it and `1-1`
    becomes the number 11, which drops page 1-1 after 1-2 and puts 2-1 in the middle."""
    names = ['1-11.jpg', '2b-1.jpg', '1-1.jpg', '2-1.jpg', '1-2.jpg', '2a-1.jpg']
    assert sorted(names, key=CM.natkey) == ['1-1.jpg', '1-2.jpg', '1-11.jpg',
                                            '2-1.jpg', '2a-1.jpg', '2b-1.jpg']


def test_page_key_ignores_separator_noise_in_the_prefix():
    """`engine mechanical` holds EM11.jpg beside EM-2.jpg. Splitting on digit runs alone
    gives two prefixes, `em` and `em-`, so page 11 sorted SECOND — right after the cover and
    78 pages before where it belongs."""
    names = ['EM-10.jpg', 'EM11.jpg', 'EM-2.jpg', 'EM-12.jpg']
    assert sorted(names, key=CM.natkey) == ['EM-2.jpg', 'EM-10.jpg', 'EM11.jpg', 'EM-12.jpg']


def test_page_key_folds_the_scanners_o_for_zero():
    """`body` holds B0-4, B040, BO169 and BO-2 — capital O typed for zero, hyphens dropped at
    random. That made FOUR bogus chapters out of one 183-page section, with page 4 sorting
    before page 2. A 0 that FOLLOWS A LETTER is an O."""
    names = ['B0-4.jpg', 'B040.jpg', 'BO169.jpg', 'BO-2.jpg', 'B0-11.jpg', 'BO-3.jpg']
    assert sorted(names, key=CM.natkey) == ['BO-2.jpg', 'BO-3.jpg', 'B0-4.jpg',
                                            'B0-11.jpg', 'B040.jpg', 'BO169.jpg']


def test_front_matter_leads_its_section():
    """Every section of that manual leads with an unnumbered cover (`cover`, `COVER`,
    `covera`, `coverhw`, `GI-COVER`) and most hold an index page. Alphabetically GI-COVER.jpg
    landed near the END of general info and index.tif after page 50. Front matter sorts in the
    order it is printed in: cover, foreword, index, then the numbered pages."""
    for names, want in (
            (['GI-1.jpg', 'index.tif', 'GI-COVER.jpg'], ['GI-COVER.jpg', 'index.tif', 'GI-1.jpg']),
            (['A-002.jpg', 'indexa.jpg', 'covera.jpg'], ['covera.jpg', 'indexa.jpg', 'A-002.jpg']),
            (['HW-002.jpg', 'indexhw.jpg', 'coverhw.jpg'],
             ['coverhw.jpg', 'indexhw.jpg', 'HW-002.jpg']),
            (['CO-1.jpg', 'C0-2.jpg', 'COVER.jpg'], ['COVER.jpg', 'CO-1.jpg', 'C0-2.jpg']),
            (['BO-2.jpg', 'index-BO-1.jpg', 'cover.jpg'],
             ['cover.jpg', 'index-BO-1.jpg', 'BO-2.jpg']),
            # a foreword sits between the cover and the contents, and these are PDFs: the
            # archive's foreword pages are almost all `Foreword.pdf`, not images
            (['1. Chapter.pdf', 'INDEX.pdf', 'Foreword.pdf', 'COVER.pdf'],
             ['COVER.pdf', 'Foreword.pdf', 'INDEX.pdf', '1. Chapter.pdf']),
            (['MT-2.jpg', 'fwd.pdf', 'covermt.jpg'], ['covermt.jpg', 'fwd.pdf', 'MT-2.jpg']),
            (['CO-2.jpg', 'contents.jpg', 'COVER.jpg'],
             ['COVER.jpg', 'contents.jpg', 'CO-2.jpg']),
            (['2_INDEX.pdf', '1_FOREWORD.pdf', 'FW Foreword.pdf'],
             ['1_FOREWORD.pdf', 'FW Foreword.pdf', '2_INDEX.pdf'])):
        assert sorted(names, key=CM.natkey) == want, names


def test_fwd_ranks_only_as_the_bare_name():
    """`fwd` is three letters and it is ALSO a drivetrain section. Measured over this archive,
    all 275 `fwd.pdf` hits are the latter: `…\\2009 Maxima A34\\fwd.pdf` sits among the Nissan
    FSM section codes (ADP, BR, CHG, CO, EC, EM, FAX, FSU, GI, LU, MA …) and
    `…\\LX570\\EWD\\system\\pdf\\fwd.pdf` among 60 EWD systems including `mm4wd`. Requiring the
    bare name is what keeps a numbered series out of the front-matter rank; the bare name itself
    stays ambiguous by choice, and the printed page order is the check on it.

    Every one of the archive's 40 genuine forewords is spelled `foreword`."""
    assert CM._lead_rank('fwd.pdf') == 1
    assert CM._lead_rank('FWD.pdf') == 1
    for name in ('fwd2.pdf', 'FWD-12.jpg', 'FWD-1.jpg', 'fwd-a.pdf', 'GI-FWD.jpg'):
        assert CM._lead_rank(name) == 3, name
    # so a Nissan FSM section folder keeps its own order, apart from the bare fwd.pdf
    names = ['FWD-2.jpg', 'FWD-10.jpg', 'FWD-1.jpg']
    assert sorted(names, key=CM.natkey) == ['FWD-1.jpg', 'FWD-2.jpg', 'FWD-10.jpg']


def test_a_publishers_long_cover_name_is_ranked_by_what_its_siblings_do_not_share():
    """The real folder `1996 Body Repair Manual`, verbatim. Its cover sorted LAST — the worst
    place — because the name carries 24 characters besides the keyword and the tag budget
    (which keeps 135 parts photos out) rejected it. Name length cannot separate a publisher's
    `..._BRM_COVER.pdf` from `...-STEERING-COLUMN-COVER.jpg`; the FOLDER can, because these
    pages share everything but their page number and those photos share nothing."""
    stem = 'PBGE95E1_FOR_EUROPE_CARISMA_96_BRM_'
    names = [f'{stem}{n}.pdf' for n in list('012345678') + ['COVER']]
    affix = CM._folder_affix([CM._base(n) for n in names])
    assert affix == (stem, '')

    assert CM._lead_rank(names[-1]) == 3, 'the full name still fails, as it must'
    assert CM._lead_rank(names[-1], affix) == 0, 'the remainder is exactly "COVER"'
    assert sorted(names, key=lambda n: CM.natkey(n, affix)) == [names[-1]] + names[:-1]


def test_a_shared_suffix_is_stripped_too():
    """`PHDE9608-C_GALANT_2001_COVER_ELECTRICAL_WIRING.pdf` — a real file whose cover word is
    in the MIDDLE, so only stripping both ends leaves it alone."""
    names = ['PHDE9608-C_GALANT_2001_COVER_ELECTRICAL_WIRING.pdf',
             'PHDE9608-C_GALANT_2001_01_ELECTRICAL_WIRING.pdf',
             'PHDE9608-C_GALANT_2001_02_ELECTRICAL_WIRING.pdf']
    affix = CM._folder_affix([CM._base(n) for n in names])
    assert affix == ('PHDE9608-C_GALANT_2001_', '_ELECTRICAL_WIRING')
    assert CM._lead_rank(names[0], affix) == 0
    assert sorted(names, key=lambda n: CM.natkey(n, affix))[0] == names[0]


def test_the_parts_photos_are_still_not_covers():
    """The 135 measured false hits. They live in one folder and share at most `Medium_`, so
    the remainder is still a whole descriptive sentence and the tag budget still rejects it —
    which is the point: stripping does not weaken the test, it only re-aims it."""
    names = ['Medium_1998-FORD-RANGER-GRAY-IGNITION-HARNESS-COVER.jpg',
             'Medium_2002-CHEVY-TRUCK-COLUMN-COVER.jpg',
             'Medium_2005-DODGE-CARAVAN-UNDERDASH-COVER.jpg']
    affix = CM._folder_affix([CM._base(n) for n in names])
    for n in names:
        assert CM._lead_rank(n, affix) == 3, n


def test_discover_is_still_not_a_cover():
    """A Land Rover Discovery manual, alone and beside siblings."""
    assert CM._lead_rank('DISCOVER.PDF') == 3
    names = ['DISCOVER.PDF', 'DEFENDER.PDF', 'FREELANDER.PDF']
    affix = CM._folder_affix([CM._base(n) for n in names])
    assert CM._lead_rank('DISCOVER.PDF', affix) == 3


def test_the_shared_prefix_is_cut_at_a_separator():
    """`ABC_1` and `ABC_12` share the characters `ABC_1`, but cutting there would leave page 1
    with an empty remainder and page 12 pretending to be page 2."""
    names = ['ABC_1.pdf', 'ABC_12.pdf', 'ABC_COVER.pdf']
    pre, suf = CM._folder_affix([CM._base(n) for n in names])
    assert (pre, suf) == ('ABC_', '')
    assert CM._lead_rank('ABC_COVER.pdf', (pre, suf)) == 0
    assert sorted(names, key=lambda n: CM.natkey(n, (pre, suf))) == \
        ['ABC_COVER.pdf', 'ABC_1.pdf', 'ABC_12.pdf']


def test_one_file_has_no_siblings_to_share_anything_with():
    """With a single file, 'what they all share' is the whole name — which would strip it to
    nothing. A folder of one is left exactly as it is today."""
    assert CM._folder_affix(['PBGE95E1_FOR_EUROPE_CARISMA_96_BRM_COVER']) == ('', '')
    assert CM._folder_affix([]) == ('', '')


def test_stripping_can_only_add_a_hit_never_remove_one():
    """A folder where every file is `Cover_N`: the shared prefix IS the keyword, so the
    remainder is just a number. The full name is judged first, so all of them keep the rank
    they have today — the affix is a second chance, never a replacement."""
    names = ['Cover_1.pdf', 'Cover_2.pdf', 'Cover_3.pdf']
    affix = CM._folder_affix([CM._base(n) for n in names])
    assert affix[0] == 'Cover_'
    for n in names:
        assert CM._lead_rank(n) == 0 and CM._lead_rank(n, affix) == 0, n


def test_a_folder_with_no_common_prefix_still_uses_the_plain_test():
    """The Daihatsu `general info` shape: `index.tif` shares nothing with `GI-*`, so there is
    no affix at all and `GI-COVER.jpg` must still rank on its own two-character tag."""
    names = ['GI-COVER.jpg', 'GI-1.jpg', 'GI-2.jpg', 'index.tif']
    affix = CM._folder_affix([CM._base(n) for n in names])
    assert affix == ('', '')
    assert sorted(names, key=lambda n: CM.natkey(n, affix)) == \
        ['GI-COVER.jpg', 'index.tif', 'GI-1.jpg', 'GI-2.jpg']


def test_collect_applies_the_folder_affix_end_to_end(tmp_path):
    """Through `collect`, which is what actually orders a combine — the affix has to be
    computed there, from the directory being walked."""
    d = tmp_path / '1996 Body Repair Manual'
    d.mkdir()
    stem = 'PBGE95E1_FOR_EUROPE_CARISMA_96_BRM_'
    for n in list('012345678') + ['COVER']:
        _pdf(d / f'{stem}{n}.pdf')
    got = [p.name for p in CM.collect(d)]
    assert got[0] == f'{stem}COVER.pdf', got[:3]
    assert got[1:] == [f'{stem}{n}.pdf' for n in '012345678']


def test_page_key_survives_a_mojibake_name():
    """One file in `front axle and suspension` is named `╡FS-2.jpg` (U+2561). It sorted after
    every ASCII name in the section. The stray byte carries no page information."""
    names = ['FS-10.jpg', '╡FS-2.jpg', 'FS-1.jpg', 'FS-3.jpg']
    assert sorted(names, key=CM.natkey) == ['FS-1.jpg', '╡FS-2.jpg',
                                            'FS-3.jpg', 'FS-10.jpg']


def test_base_strips_only_a_known_page_extension():
    """These keys sort FOLDER names too (collect walks files and subfolders with one key), so
    `Path.stem` is not usable: it turns `4.2 ENGINE` into `4` and would file that whole folder
    as if it were page 4."""
    assert CM._base('GI-12.tif') == 'GI-12'
    assert CM._base('4.2 ENGINE') == '4.2 ENGINE'
    assert CM._base('null') == 'null'          # the extensionless PDF keeps its whole name
    assert CM._base('notes.txt') == 'notes.txt'
    # and the empty strings re.split emits are kept, so a digit-leading name still sorts
    # ahead of an alpha-leading one, as it did before
    names = ['AIRBAG SYSTEM AB', '4.2 ENGINE', '1. General Description.pdf']
    assert sorted(names, key=CM.natkey) == ['1. General Description.pdf', '4.2 ENGINE',
                                            'AIRBAG SYSTEM AB']


def test_page_key_is_a_total_order_whatever_the_input_order():
    """pagekey is lossy on purpose — GI-12.jpg and GI-12.tif share one key — and `sorted` is
    only stable with respect to whatever order the filesystem handed back, which is not a
    guarantee. The name tiebreak is what makes a run reproducible. Also pins that no int is
    ever compared to a str: every token is the same 3-tuple shape."""
    import random
    names = ['GI-12.jpg', 'GI-12.tif', 'null', '4.2 ENGINE', 'cover.jpg', '1-1.jpg',
             'AIRBAG SYSTEM AB', 'B0-4.jpg', 'BO-4.jpg', '╡FS-2.jpg', 'index.tif']
    a, b = list(names), list(names)
    random.Random(1).shuffle(a)
    random.Random(9).shuffle(b)
    assert sorted(a, key=CM.natkey) == sorted(b, key=CM.natkey)


def test_real_image_section_orders_end_to_end(tmp_path):
    """The whole fix, through collect, on the shape `engine mechanical` really has."""
    sec = tmp_path / 'engine mechanical'
    for n in ('cover', 'index', 'EM-2', 'EM-3', 'EM-10', 'EM11', 'EM-12'):
        U.make_page_image(sec / f'{n}.jpg', px=(300, 400))
    assert [p.stem for p in CM.collect(sec)] == ['cover', 'index', 'EM-2', 'EM-3',
                                                 'EM-10', 'EM11', 'EM-12']


# ── the same page scanned twice ───────────────────────────────────────────────

def _img(path: Path, px) -> Path:
    return U.make_page_image(path, px=px)


def test_low_res_rescan_is_dropped_and_reported(tmp_path, capsys):
    """`general info` holds 26 of its pages BOTH as a 2550x3508 JPG and as a 637x877 TIF.
    Merging both puts every one of them in the manual twice."""
    sec = tmp_path / 'general info'
    _img(sec / 'GI-12.jpg', (2550, 3508))
    tif = _img(sec / 'GI-12.tif', (637, 877))

    kept, dups = CM.dedupe_pages(CM.collect(sec))
    assert [p.name for p in kept] == ['GI-12.jpg']
    assert len(dups) == 1 and dups[0].keep.name == 'GI-12.jpg'
    assert [p.name for p in dups[0].drop] == ['GI-12.tif']
    assert dups[0].reason == ''
    assert CM.combine(kept, tmp_path / 'out.pdf') == 1
    # nothing is deleted: a drop is an omission from the PDF, never a change on disk
    assert tif.is_file()
    CM.report_dups(dups, tmp_path)
    out = capsys.readouterr().out
    # both sides of the comparison are labelled the same way — a bare name for the KEPT file
    # was ambiguous in exactly the folders this runs on (every section has a `cover.jpg`)
    assert 'DROP' in out and 'GI-12.tif' in out
    assert str(Path('general info') / 'GI-12.jpg') in out.split('keeping', 1)[1]


def test_a_tif_that_is_the_only_copy_of_its_page_is_kept(tmp_path):
    """The trap that rules out a format rule. Four of that folder's 30 TIFs (GI-11, GI-17,
    GI-21, index) have no JPG counterpart, so 'drop the TIFs' loses four pages silently —
    exactly what the page-count check exists to prevent. Resolution decides PER PAGE."""
    sec = tmp_path / 'general info'
    _img(sec / 'GI-11.tif', (637, 877))          # no JPG of this page exists
    _img(sec / 'index.tif', (1275, 1754))
    _img(sec / 'GI-12.jpg', (2550, 3508))
    _img(sec / 'GI-12.tif', (637, 877))

    kept, dups = CM.dedupe_pages(CM.collect(sec))
    assert sorted(p.name for p in kept) == ['GI-11.tif', 'GI-12.jpg', 'index.tif']
    assert CM.combine(kept, tmp_path / 'out.pdf') == 3


def test_a_missing_hyphen_still_counts_as_the_same_page(tmp_path):
    """Nine of the duplicates are `GI-2.jpg` against `GI2.tif` — they only collide once the
    separator is normalised away, which is why dedupe keys off the same normalisation the
    ordering does."""
    sec = tmp_path / 'general info'
    _img(sec / 'GI-2.jpg', (2550, 3508))
    _img(sec / 'GI2.tif', (637, 877))
    kept, dups = CM.dedupe_pages(CM.collect(sec))
    assert [p.name for p in kept] == ['GI-2.jpg']
    assert [p.name for p in dups[0].drop] == ['GI2.tif']


def test_similar_resolution_collision_keeps_both_and_says_so(tmp_path, capsys):
    """Two files can share a page key and still be two DIFFERENT pages. All 26 real
    duplicate pairs are 4.0x-16.9x apart in pixel area; two scans off one machine are ~1.0x.
    Below the floor the tool keeps everything and asks for a human — it never guesses."""
    sec = tmp_path / 'general info'
    _img(sec / 'GI-5.jpg', (2550, 3508))
    _img(sec / 'GI5.jpg', (2544, 3500))
    kept, dups = CM.dedupe_pages(CM.collect(sec))
    assert len(kept) == 2, 'an ambiguous group must not lose a page'
    assert len(dups) == 1 and dups[0].keep is None and dups[0].drop == ()
    assert dups[0].reason
    assert CM.combine(kept, tmp_path / 'out.pdf') == 2
    CM.report_dups(dups, tmp_path)
    assert 'WARNING' in capsys.readouterr().out


def test_every_sections_cover_survives_because_grouping_is_per_folder(tmp_path):
    """The single most damaging bug this code could have. All 21 sections of that manual have
    a `cover.jpg`: grouping by page key GLOBALLY put 17 of them in one group, which at these
    sizes would have thrown away 16 section covers. The sizes differ on purpose here — with
    equal sizes a global grouping would be caught by the ratio floor instead."""
    for name, px in (('body', (2550, 3508)), ('clutch', (1275, 1754)),
                     ('transfer', (637, 877))):
        _img(tmp_path / name / 'cover.jpg', px)
        _img(tmp_path / name / 'p-1.jpg', (300, 400))

    kept, dups = CM.dedupe_pages(CM.collect(tmp_path, recursive=True))
    assert [p.name for p in kept].count('cover.jpg') == 3, 'every section keeps its cover'
    assert dups == []


def test_a_pdf_is_never_deduped(tmp_path):
    """A PDF's name says nothing about how many pages are inside it, so a page-key collision
    involving one cannot mean 'the same page twice'."""
    sec = tmp_path / 'sec'
    sec.mkdir()
    _pdf(sec / '1.pdf', npages=2)
    _img(sec / '1.jpg', (2550, 3508))
    kept, dups = CM.dedupe_pages(CM.collect(sec))
    assert len(kept) == 2, 'nothing may be dropped'
    assert len(dups) == 1 and dups[0].drop == () and 'PDF' in dups[0].reason
    assert CM.combine(kept, tmp_path / 'out.pdf') == 3


def test_an_unreadable_image_is_never_dropped_or_kept_as_the_winner(tmp_path):
    """A file that cannot be measured has no resolution to compare, so it is neither dropped
    nor allowed to displace a good copy. combine still refuses it, as it does today."""
    sec = tmp_path / 'sec'
    sec.mkdir()
    _img(sec / 'p-1.jpg', (2550, 3508))
    (sec / 'p1.jpg').write_bytes(b'not an image at all')
    kept, dups = CM.dedupe_pages(CM.collect(sec))
    assert len(kept) == 2 and dups[0].drop == ()
    with pytest.raises(CM.CombineFailed):
        CM.combine(kept, tmp_path / 'out.pdf')
    assert not (tmp_path / 'out.pdf').exists()


def test_the_page_count_is_verified_against_the_kept_list(tmp_path):
    """A deliberate omission must still be arithmetically self-consistent: the merge is
    verified against the pages it was actually given, so the count cannot silently absorb a
    page that went missing for some OTHER reason."""
    sec = tmp_path / 'general info'
    for n in (12, 13, 14):
        _img(sec / f'GI-{n}.jpg', (2550, 3508))
        _img(sec / f'GI-{n}.tif', (637, 877))
    files = CM.collect(sec)
    kept, dups = CM.dedupe_pages(files)
    assert len(files) == 6 and len(kept) == 3
    assert CM.expected_pages(kept)[0] == 3
    assert CM.combine(kept, tmp_path / 'out.pdf') == 3


# ── bookmarks ─────────────────────────────────────────────────────────────────

def test_bookmarks_point_at_each_sections_first_page(tmp_path):
    """A 1220-page combined manual with no outline cannot be navigated at all."""
    root = tmp_path / 'bertone'
    for name, n in (('appendix', 2), ('body', 3), ('clutch', 1)):
        for i in range(1, n + 1):
            _img(root / name / f'p-{i}.jpg', (300, 400))
    files = CM.collect(root, recursive=True)
    marks = CM.section_bookmarks(files, root)
    assert list(marks.values()) == ['appendix', 'body', 'clutch']

    out = tmp_path / 'bertone.pdf'
    assert CM.combine(files, out, bookmarks=marks) == 6
    assert CM.outline_pages(out) == [('appendix', 1), ('body', 3), ('clutch', 6)]
    assert CM.bookmark_preview(files, marks) == CM.outline_pages(out)


def test_a_bookmark_after_a_multipage_pdf_lands_on_the_right_page(tmp_path):
    """Page indices are MEASURED during the merge, not assumed one-page-per-file: a section
    whose first part is a 3-page PDF must not put the next section's bookmark 2 pages early."""
    root = tmp_path / 'manual'
    (root / 'a').mkdir(parents=True)
    _pdf(root / 'a' / '1. Intro.pdf', npages=3)
    _img(root / 'b' / 'p-1.jpg', (300, 400))
    files = CM.collect(root, recursive=True)
    out = tmp_path / 'manual.pdf'
    assert CM.combine(files, out, bookmarks=CM.section_bookmarks(files, root)) == 4
    assert CM.outline_pages(out) == [('a', 1), ('b', 4)]


def test_a_flat_folder_gets_no_bookmarks(tmp_path):
    """So every non-recursive run — the default, and what the Explorer menu uses — produces
    exactly the PDF it produced before this existed."""
    d = tmp_path / 'f'
    for i in (1, 2):
        _img(d / f'p-{i}.jpg', (300, 400))
    files = CM.collect(d)
    assert CM.section_bookmarks(files, d) == {}
    out = tmp_path / 'f.pdf'
    assert CM.combine(files, out, bookmarks=CM.section_bookmarks(files, d)) == 2
    assert CM.outline_pages(out) == []


def test_bookmarks_do_not_change_the_page_count(tmp_path):
    """Outline items are catalogue objects, not pages. A bookmark bug must never be able to
    hide — or cause — a page-count failure."""
    root = tmp_path / 'm'
    for name in ('a', 'b'):
        _img(root / name / 'p-1.jpg', (300, 400))
    files = CM.collect(root, recursive=True)
    plain = CM.combine(files, tmp_path / 'plain.pdf')
    marked = CM.combine(files, tmp_path / 'marked.pdf',
                        bookmarks=CM.section_bookmarks(files, root))
    assert plain == marked == 2


def test_a_bookmark_past_the_end_is_refused(tmp_path):
    """A 0-page PDF is counted as 0 by expected_pages, so the count check agrees with itself
    and the file merges invisibly. Placing a bookmark on it is what surfaces it."""
    root = tmp_path / 'm'
    (root / 'a').mkdir(parents=True)
    _img(root / 'a' / 'p-1.jpg', (300, 400))
    (root / 'b').mkdir()
    w = PdfWriter()
    with open(root / 'b' / 'empty.pdf', 'wb') as f:
        w.write(f)
    files = CM.collect(root, recursive=True)
    marks = CM.section_bookmarks(files, root)
    assert len(marks) == 2, 'the empty PDF must still be offered a bookmark'
    out = tmp_path / 'm.pdf'
    with pytest.raises(CM.CombineFailed, match='contributed no pages'):
        CM.combine(files, out, bookmarks=marks)
    assert not out.exists() and not (tmp_path / 'm.pdf.part').exists()


# ── the CLI, end to end ───────────────────────────────────────────────────────

def test_dry_run_reports_drops_and_bookmarks_and_writes_nothing(tmp_path, monkeypatch, capsys):
    """--dry-run is the Explorer menu's 'Preview page order' item, and it is what you check a
    1200-page merge with before committing to it. It has to show the omissions."""
    root = tmp_path / 'bertone'
    _img(root / 'general info' / 'GI-12.jpg', (2550, 3508))
    _img(root / 'general info' / 'GI-12.tif', (637, 877))
    _img(root / 'general info' / 'GI-5.jpg', (2550, 3508))
    _img(root / 'general info' / 'GI5.jpg', (2544, 3500))
    _img(root / 'body' / 'BO-2.jpg', (300, 400))
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob('*') if p.is_file()}

    monkeypatch.setattr(sys, 'argv', ['prog', str(root), '--recursive', '--dry-run'])
    CM.main()
    out = capsys.readouterr().out
    assert '1 dropped as low-resolution duplicates' in out
    assert 'DROP' in out and 'GI-12.tif' in out
    assert 'WARNING' in out, 'the ambiguous pair must be flagged'
    assert 'Bookmarks (predicted):' in out and 'general info' in out and 'body' in out
    assert 'Would produce 4 pages from 5 file(s)' in out
    assert '(--dry-run: nothing written)' in out
    assert {p: p.stat().st_mtime_ns for p in tmp_path.rglob('*') if p.is_file()} == before
    assert not (tmp_path / 'bertone.pdf').exists()


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


# ── the size ratio is a proxy; word recall is the real check ──────────────────


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


# ── the driver's delete gate ──────────────────────────────────────────────────




# ── repair and --skip-unrecoverable are wired into the tool, not just available ──

def _run_combine(folder, *extra):
    """Invoke combine_manual as the user does, with --no-compress so the test does not
    render or OCR anything. Returns (returncode, stdout+stderr)."""
    r = subprocess.run([sys.executable, str(Path(CM.__file__)), str(folder),
                        '--recursive', '--no-compress', *extra],
                       capture_output=True, text=True)
    return r.returncode, (r.stdout or '') + (r.stderr or '')


def test_an_unrepairable_part_refuses_the_merge_and_names_every_one(tmp_path):
    """Default behaviour: a part that cannot be repaired must stop the merge, and the message
    must name EVERY such file by full path — a count plus 'first:' does not say what to fix.
    Regression guard: `repair_inputs` existed but nothing in this tool called it, so the only
    behaviour was an immediate refusal with no repair attempt at all."""
    d = tmp_path / 'manual'
    (d / 'a').mkdir(parents=True)
    _pdf(d / 'a' / '1. Good.pdf', npages=2)
    for n in ('2. Junk.pdf', '3. AlsoJunk.pdf'):
        (d / 'a' / n).write_bytes(b'%PDF-1.4 but truncated garbage')

    rc, out = _run_combine(d)
    assert rc != 0, out
    assert 'could not be repaired' in out, out
    # BOTH bad parts, by full path — not just the first
    assert str(d / 'a' / '2. Junk.pdf') in out, out
    assert str(d / 'a' / '3. AlsoJunk.pdf') in out, out
    assert '--skip-unrecoverable' in out, 'the message must say how to proceed'
    assert not (tmp_path / 'manual.pdf').exists(), 'refused, so nothing may be written'


def test_skip_unrecoverable_combines_the_rest_and_says_what_is_missing(tmp_path):
    """Opt-in: combine the readable parts, and report the omission loudly. The verified page
    count checks what was MERGED, so it cannot reveal that pages are missing — the INCOMPLETE
    summary is the only thing that can."""
    d = tmp_path / 'manual'
    (d / 'a').mkdir(parents=True)
    _pdf(d / 'a' / '1. Good.pdf', npages=2)
    _pdf(d / 'a' / '3. AlsoGood.pdf', npages=1)
    bad = d / 'a' / '2. Junk.pdf'
    bad.write_bytes(b'%PDF-1.4 but truncated garbage')

    rc, out = _run_combine(d, '--skip-unrecoverable')
    assert rc == 0, out
    assert 'INCOMPLETE' in out, out
    assert str(bad) in out, out
    outfile = tmp_path / 'manual.pdf'
    assert outfile.is_file()
    assert len(PdfReader(str(outfile)).pages) == 3, 'the two readable parts, nothing invented'
    assert bad.is_file(), 'a skipped part must be LEFT on disk, not moved or deleted'


def test_no_repair_skips_the_attempt(tmp_path):
    """--no-repair must refuse without trying, and say so rather than reporting a repair
    that never ran."""
    d = tmp_path / 'manual'
    (d / 'a').mkdir(parents=True)
    _pdf(d / 'a' / '1. Good.pdf', npages=1)
    (d / 'a' / '2. Junk.pdf').write_bytes(b'%PDF-1.4 but truncated garbage')

    rc, out = _run_combine(d, '--no-repair')
    assert rc != 0, out
    assert 'trying qpdf' not in out, 'no repair should have been attempted'
    assert str(d / 'a' / '2. Junk.pdf') in out, out


def test_dry_run_says_what_it_would_do_about_unreadable_inputs(tmp_path):
    """A dry run has to be a faithful preview: 'would repair' vs 'would refuse' is the whole
    reason to look at one first."""
    d = tmp_path / 'manual'
    (d / 'a').mkdir(parents=True)
    _pdf(d / 'a' / '1. Good.pdf', npages=1)
    (d / 'a' / '2. Junk.pdf').write_bytes(b'%PDF-1.4 but truncated garbage')

    rc, out = _run_combine(d, '--dry-run')
    assert rc == 0, out
    assert 'would' in out and 'repair' in out, out
    assert str(d / 'a' / '2. Junk.pdf') in out, out
    assert not (tmp_path / 'manual.pdf').exists()

    rc, out = _run_combine(d, '--dry-run', '--skip-unrecoverable')
    assert 'leave out' in out, out
