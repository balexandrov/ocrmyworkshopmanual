#!/usr/bin/env python3
r"""Combine a folder of loose page images (and/or small PDFs) into ONE PDF.

For a folder whose files are the scattered pages/sections of a single manual —
e.g. `1-1.jpg 1-2.jpg ... 2a-1.jpg 2b-1.jpg ...` — this merges them, in natural
page order, into a single PDF named after the folder, written **next to** the
folder (a sibling in its parent directory). By default it then hands that PDF to
ocrmyworkshopmanual.py to compress (JBIG2/photo-aware) and add a searchable OCR
text layer.

Run it per folder, manually:

  python combine_manual.py "M:\Auto\Backup\Auto\Japan\Honda\--Engines--\Haynes_ZC_Manual"
      -> writes  ...\--Engines--\Haynes_ZC_Manual.pdf  (combined, then compressed+OCR'd)

  python combine_manual.py FOLDER --dry-run      # just show the page order, write nothing
  python combine_manual.py FOLDER --no-compress  # raw combined PDF only, skip compress/OCR
  python combine_manual.py FOLDER --language eng+rus --tessdata C:\path\to\tessdata

By default ONLY files directly in the folder are used (subfolders like a `*_files`
HTML asset dir are ignored), and only image/PDF types — a stray .htm/.txt is
skipped. With `--recursive`, subfolders are walked too, which is what a manual
split into per-topic subfolders needs:

  BODY SECTION\AIRBAG SYSTEM AB\1. General Description.pdf
  BODY SECTION\AIRBAG SYSTEM AB\2. Airbag Connector.pdf
  BODY SECTION\BODY STRUCTURE BS\1. General Description.pdf   -> one BODY SECTION.pdf

Ordering is a NATURAL sort (so `1-2` comes before `1-11`, and `2a` before `2b`)
over a page key that also forgives how scans get named in practice: separators
carry no order (`EM11` is page 11, beside `EM-2`), a `0` after a letter is an O
(`B0-4` and `BO-4` are one chapter, not two), front matter leads its folder in the
order it is printed (cover, foreword, index, then the numbered pages), and a stray
mojibake byte is ignored. Always eyeball the printed order before trusting it, or
use --dry-run first.

Two copies of ONE page — the same scan kept as a big JPG and a small TIF — are
merged once, keeping the higher-resolution copy. Every drop is printed, and the
file itself is left on disk; a page count that shrinks has to be checkable.

Under --recursive each section folder gets a BOOKMARK at its first page, so a
combined manual of a thousand pages can be navigated.

An unreadable input is REPAIRED first (qpdf, then Ghostscript), so one malformed part
out of hundreds does not strand a whole manual. A repair is not taken on trust: the
repaired copy is used only if it recovered at least as many pages as the original's
raw bytes say it held, otherwise the part stays unreadable and the merge is refused.
The originals on disk are never modified — repairs happen on scratch copies.
--no-repair turns that off; --skip-unrecoverable combines the readable parts instead
of refusing, naming every part left out and the pages it held (those files stay on
disk, untouched).

The combined PDF is VERIFIED before this exits: it must open and carry exactly the
sum of its inputs' page counts (after duplicates are dropped — the count is checked
against what was actually merged). That check is what makes it safe to delete the
source folder afterwards — merging is where pages go missing silently, since a
short PDF looks no different from a complete one. Note that with
--skip-unrecoverable the count verifies what was MERGED, so it cannot tell you the
manual is incomplete; the INCOMPLETE summary is what says that.
"""
import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import namedtuple
from pathlib import Path

import img2pdf
from PIL import Image
from pypdf import PdfReader, PdfWriter

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tif', '.tiff', '.webp'}
SCRIPT_DIR = Path(__file__).resolve().parent
TOOL = SCRIPT_DIR / 'ocrmyworkshopmanual.py'


def win_long(p) -> str:
    """Windows extended-length path so long paths open (no-op elsewhere)."""
    if os.name == 'nt':
        ap = os.path.abspath(str(p))
        return ap if ap.startswith('\\\\?\\') else '\\\\?\\' + ap
    return str(p)


# Extensions `_base` may strip. A page's IDENTITY is its name without its format, and only
# these are formats — `Path.suffix` would call `.2 ENGINE` one.
_PAGE_EXTS = IMAGE_EXTS | {'.pdf'}


def _ext(name: str) -> str:
    """The trailing KNOWN page extension, lowercased, or '' if there is none."""
    i = name.rfind('.')
    return name[i:].lower() if i > 0 and name[i:].lower() in _PAGE_EXTS else ''


def _base(name: str) -> str:
    """`name` minus a KNOWN page extension, otherwise unchanged.

    Deliberately NOT `Path.stem`: these keys run on FOLDER names too — `collect` orders files
    and subfolders with one key — and
    `Path('4.2 ENGINE').stem` is `'4'`, which would file that whole folder as if it were
    page 4. An extensionless page keeps its entire name, which is also right: the archive's
    `null` really is a PDF named `null`."""
    e = _ext(name)
    return name[:-len(e)] if e else name


# Front matter, in the order it is printed in: the cover, then a foreword, then the index/
# contents, then the numbered pages. These pages carry no page number to sort on, so without a
# rank they land wherever the alphabet drops them — `GI-COVER.jpg` sorted near the END of its
# section, and `index.tif` after page 50.
_LEAD_WORDS = ((0, ('cover',)), (1, ('foreword','title')), (2, ('index', 'contents')))

# Abbreviations that rank ONLY as the whole name — no tag, no page number. `fwd.pdf` may be a
# foreword, but three letters is not much to go on: measured over this archive, all 275 hits are
# a drivetrain section, not front matter (`…\2009 Maxima A34\fwd.pdf` sits among the Nissan FSM
# section codes ADP, BR, CHG, CO, EC, EM, FAX, FSU, GI, LU, MA…, and
# `…\LX570\EWD\system\pdf\fwd.pdf` among 60 EWD systems including mm4wd). Requiring the bare
# name at least keeps `fwd2.pdf` and a numbered `FWD-12.jpg` series out of the rank. Every one
# of the archive's 40 real forewords is spelled `foreword`, which needs none of this.
_LEAD_EXACT = {'fwd': 1}

# How many alpha characters a ranked name may carry BESIDES the keyword. A real front-matter
# page is named for what it is, at most with a short section tag — `covera`, `coverhw`,
# `covermt`, `GI-COVER`, `FW Foreword.pdf`, `index-BO-1`. Anything wordier is a page that
# merely MENTIONS the word, and measured over 1.11M image/PDF names in this archive that is
# the difference between genuine front-matter pages and false hits: 135 parts photos
# (`Medium_2002-CHEVY-TRUCK-COLUMN-COVER.jpg` is a steering-column cover) and
# `tyrerotation_fwd.jpg` (front-wheel-drive tyre rotation, not a foreword).
#
# At 10 a short topic tag fits, so `DTC Index.pdf` / `dtc_index.gif` and `mag_cover.jpg` DO
# rank as front matter — print-captured topic names whose filename order is arbitrary anyway,
# which is what --order docid exists to fix. What 10 still excludes is the wordy end: the parts
# photos above (24+ characters besides the keyword) and a publisher's
# `PBGE95E1_FOR_EUROPE_CARISMA_96_BRM_COVER.pdf`, which `_lead_rank` instead rescues from the
# folder's shared affix rather than from its length.
_MAX_LEAD_TAG = 10


def _folder_affix(bases) -> tuple:
    """(prefix, suffix) that EVERY stem in one folder shares, trimmed back to a separator.

    What all the siblings share identifies the folder, not the page: the publisher's
    `PBGE95E1_FOR_EUROPE_CARISMA_96_BRM_` says nothing about which page you are holding, and
    the part that does — `0`…`8` against `COVER` — is what is left after it.

    Cut at a separator so `ABC_1` and `ABC_12` do not "share" the prefix `ABC_1` and leave one
    file with an empty remainder. Empty for a folder of fewer than two files, where 'shared'
    would only mean the whole name."""
    bases = list(bases)
    if len(bases) < 2:
        return '', ''

    def to_sep(s: str) -> str:
        m = re.search(r'^.*[^A-Za-z0-9]', s)      # keep up to the LAST separator in s
        return m.group(0) if m else ''

    pre = to_sep(os.path.commonprefix(bases))
    suf = to_sep(os.path.commonprefix([b[::-1] for b in bases]))[::-1]
    return pre, suf


def _lead_rank(name: str, affix=('', '')) -> int:
    """0/1/2 for a cover / foreword / index page, else 3 (a numbered page).

    Matched on the name's WORDS, split on separators — not on the flattened key — because the
    separator is what tells a whole word from a coincidence: `GI-COVER.jpg` is a cover page and
    `DISCOVER.PDF` (a Land Rover Discovery manual, really in this archive) is not. A name whose
    FIRST word merely starts with the keyword counts too, since that is how the glued tags are
    written (`coverhw`, `indexa`).

    Digits are not counted against the tag budget, so a numbered front-matter page still ranks
    (`index-BO-1.jpg`, `INDEX3.pdf`) — a whole numbered series moves to the front together,
    keeping its own order. The `_LEAD_EXACT` abbreviations are the exception and must be the
    bare name, which is what keeps a numbered `FWD-12.jpg` series out.

    `affix` is what this file's SIBLINGS all share (see `_folder_affix`), and the remainder
    gets a SECOND chance at the same test. A publisher writes
    `PBGE95E1_FOR_EUROPE_CARISMA_96_BRM_COVER.pdf`, which carries 24 characters besides the
    keyword and so fails the tag budget — the budget that exists to keep 135 parts photos
    (`Medium_2002-CHEVY-TRUCK-COLUMN-COVER.jpg`) out of the cover rank. On the name alone those
    cases are indistinguishable; on the folder they are not, because those photos share nothing
    with each other while the manual's pages share everything but their page number. Measured
    over 578 archive folders: 51 real covers gained, none lost.

    The full name is judged FIRST, so this can only ever add a hit and never remove one.

    The tool prints the page order before it writes anything, and that is the real check on all
    of this — which is what --dry-run is for."""
    rank = _lead_rank_of(_base(name))
    if rank < 3:
        return rank
    pre, suf = affix
    if not pre and not suf:
        return 3
    stem = _base(name)
    if pre and stem.startswith(pre):
        stem = stem[len(pre):]
    if suf and len(stem) > len(suf) and stem.endswith(suf):
        stem = stem[:-len(suf)]
    return _lead_rank_of(stem) if stem.strip() else 3


def _lead_rank_of(stem: str) -> int:
    """`_lead_rank`'s test, applied to one already-extensionless stem."""
    words = [w for w in re.split(r'[^a-z0-9]+', stem.lower()) if w]
    if not words:
        return 3
    if len(words) == 1 and words[0] in _LEAD_EXACT:
        return _LEAD_EXACT[words[0]]
    alpha = sum(len(re.sub(r'[^a-z]', '', w)) for w in words)
    for rank, keys in _LEAD_WORDS:
        for k in keys:
            if (words[0].startswith(k) or k in words) and alpha - len(k) <= _MAX_LEAD_TAG:
                return rank
    return 3


def pagekey(name: str, affix=('', '')):
    """Which page of which chapter this file holds, with everything that records only how the
    scanner was driven thrown away. Two files with the same `pagekey` inside ONE folder are
    two copies of one page — that is what `dedupe_pages` keys off.

    `affix` is passed to `_lead_rank` and affects ONLY the front-matter rank, never the sort
    tokens: pages that share a folder prefix already sort correctly by the tokens they do not
    share, so re-tokenising the remainder would be risk without gain.

    Measured on a 21-section Daihatsu manual (1216 .jpg + 30 .tif) where a plain filename
    sort put 14 of the 21 sections in the wrong order:
      * `engine mechanical` holds `EM11.jpg` beside `EM-2.jpg`. Splitting on digit runs alone
        gives the prefixes `em` and `em-`, so page 11 sorted SECOND, right after the cover.
      * `body` holds `B0-4`, `B040`, `BO169`, `BO-2` — whoever named the scans typed capital
        O for zero and dropped hyphens at random, making FOUR bogus chapters out of one. A 0
        that FOLLOWS A LETTER is an O, so folding it merges them back.
      * one file is mojibake, `╡FS-2.jpg`, which sorted after every ASCII name. That
        leading byte carries no page information and goes with the other separators.
      * every section leads with an unnumbered `cover.jpg` (also `covera`, `coverhw`,
        `GI-COVER`) and most hold an `index` page. Alphabetically `GI-COVER.jpg` landed near
        the END of its section, so front matter is ranked ahead of the numbered pages —
        see `_lead_rank`, which is also what keeps `DISCOVER.PDF` and a photo of a steering
        column cover out of that rank.

    The `\\x00` marker is not decoration: a separator BETWEEN TWO DIGITS carries order, so
    stripping it merges `1-1` into `11` and breaks the sequence this module documents,
    `1-1, 1-2, 1-11, 2-1, 2a-1, 2b-1`. With the marker that sequence is identical to the one
    the old key gave. The empty strings `re.split` emits are kept for the same reason — drop
    them and `AIRBAG SYSTEM AB` sorts ahead of `1. General Description.pdf`, reversing the
    old order. Every token is the same 3-tuple shape, so an int is never compared to a str."""
    s = _base(name).lower()
    s = re.sub(r'(?<=[a-z])0', 'o', s)                  # B0-4 == BO-4
    s = re.sub(r'(?<=\d)[^a-z0-9]+(?=\d)', '\x00', s)   # KEEP digit|digit boundaries
    s = re.sub(r'[^a-z0-9\x00]', '', s)                 # other separators carry no order
    toks = tuple((1, '', int(t)) if t.isdigit() else (0, t.replace('\x00', ''), 0)
                 for t in re.split(r'(\d+)', s))
    return (_lead_rank(name, affix), toks)


def natkey(name: str, affix=('', '')):
    """Natural sort key: `pagekey`, then the raw name as a final tiebreak.

    The tiebreak is what makes the order reproducible. `pagekey` is lossy on purpose —
    `GI-12.jpg` and `GI-12.tif` share one key — and `sorted` is only stable with respect to
    whatever order the filesystem happened to hand back, which is not a guarantee.

    `affix` defaults to nothing, so a caller with no sibling context — `order_by_docid`
    breaking a tie, for one — behaves exactly as before."""
    return (pagekey(name, affix), name.lower())


def looks_like_pdf(p: Path) -> bool:
    """True if the file BEGINS with a PDF header, whatever its name says.

    Not paranoia: one page in this archive is a genuine 1-page PDF stored as a file
    literally named `null` (five stray newlines, then `%PDF-1.3`), inside a Wiring Diagram
    Section. Keying off the extension alone dropped it from its section — and this tool
    then deletes the folder, so that page would have been gone. Leading whitespace is
    tolerated because that is exactly how these files are malformed; the header must still
    be the first real bytes, so a text file that merely mentions %PDF does not match."""
    try:
        with open(win_long(p), 'rb') as f:
            head = f.read(1024)
    except OSError:
        return False
    return head.lstrip(b'\r\n \t\x00').startswith(b'%PDF')


def is_pdf(p: Path) -> bool:
    """A page-bearing PDF: by extension, or by content for a misnamed one."""
    return p.suffix.lower() == '.pdf' or looks_like_pdf(p)


def collect(folder: Path, recursive: bool = False):
    """Image + PDF files in `folder`, in natural page order.

    Non-recursive (default): direct files only. With `recursive`: subfolders are walked
    depth-first, and at EVERY level files and subfolders are ordered together by the same
    natural key — so a subfolder takes its pages' place in the sequence rather than being
    appended at the end. That is what a mixed section needs, where numbered intro pages
    sit beside a subfolder that continues them:

        1. Foreword.pdf  …  8. Pre-delivery Inspection.pdf
        PERIODIC MAINTENANCE SERVICES PM\\1. …          <- comes after 8., as printed
    """
    out = []

    def walk(d: Path):
        try:
            raw = list(d.iterdir())
        except OSError as ex:
            print(f'  WARNING: cannot list {d}: {ex}', file=sys.stderr)
            return
        # What this folder's PAGES all share tells you nothing about which page each one is,
        # so `_lead_rank` gets to judge the part that differs. Computed over the page files
        # only — a subfolder whose name does not carry the prefix is simply unaffected.
        affix = _folder_affix([_base(p.name) for p in raw
                               if p.is_file() and _ext(p.name)])
        entries = sorted(raw, key=lambda p: natkey(p.name, affix))
        for p in entries:
            if p.is_dir():
                if recursive:
                    walk(p)
            elif p.suffix.lower() in _PAGE_EXTS or looks_like_pdf(p):
                out.append(p)

    walk(folder)
    return out


# A page-key collision is called a low-resolution rescan when the biggest copy has at least
# this many times the pixel area of a smaller one. Measured on `general info` of the Daihatsu
# manual: all 26 real duplicate pairs sit between 4.00x and 16.92x (e.g. GI-12.jpg 2550x3508
# against GI-12.tif 637x877). Two genuinely DIFFERENT pages that merely normalise to the same
# key would be scans off the same machine, i.e. ~1.0x — so the floor sits clear of both.
MIN_DUP_AREA_RATIO = 2.0

DupGroup = namedtuple('DupGroup', 'files keep drop size reason')
"""Files sharing one page key inside ONE folder. `keep` is the copy that goes into the PDF and
`drop` the redundant low-resolution ones; when the call is ambiguous `keep` is None, `drop` is
empty — every file stays in the merge — and `reason` says what a human has to look at. `size`
is {path: (w, h)}, so the report shows the pixels the decision was made on."""


def _image_size(p: Path) -> tuple:
    """(width, height) in pixels, (0, 0) if the image cannot be read. Pillow's `open` parses
    the header only, so this costs a few KB per file rather than decoding a 2550x3508 scan —
    and it is called for collision groups ONLY (26 groups out of 1246 files on the real
    folder), never for every page."""
    try:
        with Image.open(win_long(p)) as im:
            return im.size
    except Exception:
        return (0, 0)


def dedupe_pages(files) -> tuple:
    """(files to merge, [DupGroup, ...]) — the same page, scanned twice, included once.

    `general info` of a real Daihatsu manual holds 26 of its pages BOTH as a high-resolution
    JPG and as a low-resolution TIF (`GI-12.jpg` 2550x3508 beside `GI-12.tif` 637x877; and
    `GI-2.jpg` beside `GI2.tif`, which only collide once the hyphen is normalised away).
    Merging both puts every one of those pages in the manual twice.

    Dropping .tif wholesale is exactly the wrong fix: four of that folder's 30 TIFs
    (`GI-11`, `GI-17`, `GI-21`, `index`) are the ONLY copy of their page, so a format rule
    loses four pages silently — the failure this tool's page-count check exists to prevent.
    So RESOLUTION decides, per page, and only where two files really claim the same page.

    Grouping is per PARENT FOLDER, never global. All 21 sections of that manual have a
    `cover.jpg`: grouping globally put 17 of them in one group and would have thrown away 16
    section covers. Measured — per-folder: 26 groups, none ambiguous; global: 28 groups over
    71 files.

    All-or-nothing per group, like `order_by_docid`: unless EVERY other member is at least
    MIN_DUP_AREA_RATIO smaller than the biggest, nothing is dropped and the group is reported
    for review instead. A PDF is never dropped — its name says nothing about how many pages
    are inside it. An unreadable image measures 0, so it is neither dropped nor kept as the
    winner, and `combine` still refuses it as it does today.

    Nothing is deleted from disk. A dropped file stays exactly where it is and is named in
    the report, because a page deliberately left out of a manual has to be checkable."""
    groups = {}
    # Same affix `collect` sorted with, per folder, so an identity key and a sort key can
    # never disagree about the same file.
    by_dir = {}
    for p in files:
        by_dir.setdefault(p.parent, []).append(p)
    affixes = {d: _folder_affix([_base(q.name) for q in ps]) for d, ps in by_dir.items()}
    for p in files:
        groups.setdefault((p.parent, pagekey(p.name, affixes[p.parent])), []).append(p)
    drop, out = set(), []
    for grp in groups.values():
        if len(grp) < 2:
            continue
        if any(is_pdf(p) for p in grp):
            out.append(DupGroup(tuple(grp), None, (), {},
                                'one of them is a PDF, which carries pages its name cannot '
                                'account for'))
            continue
        size = {p: _image_size(p) for p in grp}
        area = {p: size[p][0] * size[p][1] for p in grp}
        keep = max(grp, key=lambda p: (area[p], p.name))     # name breaks an exact tie
        rest = [p for p in grp if p is not keep]
        if all(area[p] and area[keep] >= MIN_DUP_AREA_RATIO * area[p] for p in rest):
            drop.update(rest)
            out.append(DupGroup(tuple(grp), keep, tuple(rest), size, ''))
        else:
            out.append(DupGroup(tuple(grp), None, (), size,
                                'same page key at a similar resolution — these may be two '
                                'DIFFERENT pages, so both are in the PDF'))
    return [p for p in files if p not in drop], out


def report_dups(dups, folder: Path) -> None:
    """Print every duplicate decision — the drops AND the ambiguous groups, on a dry run and
    a real one alike. `expected_pages` REPORTS an unreadable input rather than skipping it,
    for the same reason: an omission nobody can see is indistinguishable from a bug."""
    if not dups:
        return

    def label(p):
        try:
            return p.relative_to(folder)
        except ValueError:
            return p.name

    def px(g, p):
        w, h = g.size.get(p, (0, 0))
        return f'{w}x{h} ({w * h / 1e6:.1f} MP)' if w else 'unreadable'

    print('\nDuplicate pages (the same page inside one folder):')
    for g in dups:
        if not g.drop:
            continue
        ka = g.size[g.keep][0] * g.size[g.keep][1]
        for p in g.drop:
            pa = g.size[p][0] * g.size[p][1]
            print(f'  DROP {label(p)}  {px(g, p)}  ->  keeping {label(g.keep)}  '
                  f'{px(g, g.keep)}, {ka / pa:.1f}x bigger')
    n_drop = sum(len(g.drop) for g in dups)
    if n_drop:
        print(f'  {n_drop} dropped as low-resolution copies; those files are untouched on disk')
    for g in dups:
        if g.drop:
            continue
        print(f'  WARNING: {len(g.files)} copies kept — a human has to look: {g.reason}')
        for p in g.files:
            print(f'      {label(p)}  {px(g, p)}')


def page_count(pdf: Path) -> int:
    """Pages in a PDF, or -1 if it cannot be read."""
    try:
        return len(PdfReader(win_long(pdf)).pages)
    except Exception:
        return -1


# A browser print-to-PDF header, which is where a printed HTML manual records where the
# page came from:
#     1/6/23, 10:57 PM DTC Index
#     https://mitsubishitechinfo.com/data/DG/2022/06/HTML/N5060302G0000900USA.htm 1/15
# The `06` is the publisher's group and `N5060302G0000900USA` its document id; sorting on
# them reproduces the manual's own order.
_DOCID_URL = re.compile(r'https?://\S*?/(\d{4})/(\d+)/HTML/([A-Za-z0-9]+)\.htm',
                        re.IGNORECASE)


def docid_key(pdf: Path):
    """(group, docid) from a PDF's page-1 print header, or None if it has none.

    Some manuals are captured by printing an online manual page by page. The parts are then
    named by TOPIC (`CONSULT Function.pdf`, `DTC Index.pdf`), so a filename sort is
    alphabetical — i.e. arbitrary — and would put the diagnostics tooling before the DTC
    index. The print header is the only surviving record of the publisher's own order."""
    try:
        text = PdfReader(win_long(pdf)).pages[0].extract_text() or ''
    except Exception:
        return None
    m = _DOCID_URL.search(text)
    return (m.group(2).zfill(4), m.group(3)) if m else None


def order_by_docid(files) -> tuple:
    """(files in publisher order, how many had no doc id).

    ALL-OR-NOTHING by design: if any file lacks a doc id the ORIGINAL order is returned
    untouched. A consistent order can be reviewed; a half-publisher, half-alphabetical one
    cannot, and there is no way to tell from the result which half you are looking at. It
    also makes the option safe to leave on for a manual whose parts are numbered instead —
    none of those carry a doc id, so coverage is zero and nothing is reordered."""
    keys = [(docid_key(p), p) for p in files if is_pdf(p)]
    missing = sum(1 for k, _p in keys if k is None) + sum(1 for p in files if not is_pdf(p))
    if missing or not keys:
        return list(files), missing
    return [p for _k, p in sorted(keys, key=lambda kp: (kp[0], natkey(kp[1].name)))], 0


def expected_pages(files) -> tuple:
    """(total pages the combined PDF must have, list of unreadable inputs).

    An image is one page; a PDF contributes its own count. Unreadable inputs are
    returned rather than skipped: silently dropping one produces a short PDF that looks
    perfectly valid, and the whole point of the count is to catch exactly that."""
    total, bad = 0, []
    for p in files:
        if not is_pdf(p):
            total += 1                     # an image is one page
            continue
        n = page_count(p)
        if n < 0:
            bad.append(p)
        else:
            total += n
    return total, bad


def section_bookmarks(files, folder: Path) -> dict:
    """{index into `files`: bookmark title} — one bookmark per SECTION folder.

    A section is the FIRST path component under `folder`, so `bertone\\general info\\GI-1.jpg`
    belongs to `general info` and the bookmark goes on the first page that section
    contributes. A file sitting directly in `folder` belongs to no section and gets none: it
    is already at the top of the sequence.

    A combined manual with no outline cannot be navigated at all — measured: 21 sections and
    1220 pages from one real Daihatsu folder. The titles are folder names and the order is the
    page order, so the outline asserts nothing the sequence does not; it is navigation, never
    a claim about content, and it adds no pages.

    Returns {} unless at least two sections exist, so a flat folder — and therefore every
    non-recursive run — produces exactly the PDF it produced before."""
    marks, seen = {}, set()
    for i, p in enumerate(files):
        try:
            rel = p.relative_to(folder)
        except ValueError:
            continue
        if len(rel.parts) < 2:
            continue                       # directly in `folder`: no section of its own
        title = rel.parts[0]
        if title not in seen:
            seen.add(title)
            marks[i] = title
    return marks if len(marks) > 1 else {}


def bookmark_preview(files, marks: dict) -> list:
    """[(title, 1-based page), ...] PREDICTED from the inputs' page counts — for --dry-run,
    which merges nothing. An image is one page and a PDF contributes its own count, exactly
    as `expected_pages` counts them. The real bookmarks are placed from what was ACTUALLY
    appended (see `combine`), never from this."""
    out, page = [], 1
    for i, p in enumerate(files):
        if i in marks:
            out.append((marks[i], page))
        page += 1 if not is_pdf(p) else max(page_count(p), 0)
    return out


def outline_pages(pdf: Path) -> list:
    """[(title, 1-based page), ...] read BACK out of a finished PDF, top level only.
    Reported instead of what was asked for, for the same reason the page count is: the output
    is the only thing worth believing."""
    try:
        r = PdfReader(win_long(pdf))
        return [(str(it.title), r.get_destination_page_number(it) + 1)
                for it in r.outline if not isinstance(it, list)]
    except Exception:
        return []


_PAGE_OBJ = re.compile(rb'/Type\s*/Page(?![s/\w])')


def raw_pages(pdf: Path) -> tuple:
    """(page count read from the RAW BYTES, whether that count can be trusted).

    Usable when the file is too broken for a parser to open at all. Counts `/Type /Page`
    object dictionaries — never `/Type /Pages`, the tree node.

    A PDF that keeps its page dicts inside compressed object streams shows none of them in
    the clear, so a zero count there means "cannot tell", not "no pages". The presence of
    `/ObjStm` is what separates the two, and the distinction matters: a count of 0 that is
    TRUSTWORTHY means the file has no page structure left at all, and a "repair" of it —
    Ghostscript will happily emit one page out of `%PDF-1.4 but truncated garbage` — is
    inventing content, not recovering it."""
    try:
        raw = pdf.read_bytes()
    except OSError:
        return 0, False
    n = len(_PAGE_OBJ.findall(raw))
    return n, (n > 0 or b'/ObjStm' not in raw)


def pages_in_raw_bytes(pdf: Path) -> int:
    """Page count from the raw bytes, 0 when unknown. See `raw_pages` for the caveat."""
    return raw_pages(pdf)[0]


Repair = namedtuple('Repair', 'src rel status recovered expected')
"""One input's repair outcome. `status` is:
     'repaired'   - fully recovered, safe to merge
     'incomplete' - a repair worked but salvaged FEWER pages than the original held
     'failed'     - nothing could be read out of it at all
`rel` is the path relative to the section, because a bare filename is ambiguous: one real
section holds `Clutch System\\General Description.pdf` AND
`Control Systems\\General Description.pdf`, and a report naming only 'General
Description.pdf' twice cannot tell you which one is broken."""


def repair_inputs(files, work: Path, base: Path = None) -> tuple:
    """Repair the unreadable PDFs among `files`, reusing ocrmyworkshopmanual's qpdf-then-
    Ghostscript repair. Returns (files with repaired copies substituted, [Repair, ...]).

    Refusing a whole section over one malformed part would leave the manual split forever,
    so a recoverable part is recovered. But a repair is NOT taken at face value: the page
    count that guards the merge is computed from the files handed to it, so a repair that
    salvaged 1 page of 9 would agree with itself and pass, and the section's source folder
    would then be deleted. Measured on a real Baja transmission section — three truncated
    parts holding 3, 9 and 6 page objects, from each of which qpdf salvaged exactly one
    page: ~15 pages would have been lost silently.

    So a repair must recover at least as many pages as the raw bytes say the original had.
    When it cannot, the part is left unreadable on purpose — `expected_pages` still reports
    it and `combine` still refuses — but HOW SHORT the salvage was is measured and returned,
    because 'recovered 1 of ~9 pages' is what tells you whether the file is worth chasing,
    and it is what --skip-unrecoverable needs in order to say what a section is missing."""
    import ocrmyworkshopmanual as owm
    out, results = [], []
    for i, p in enumerate(files):
        if not is_pdf(p) or page_count(p) >= 0:
            out.append(p)
            continue
        rel = p.relative_to(base) if base else Path(p.name)
        want, trusted = raw_pages(p)
        if trusted and want == 0:
            # No page structure survives in the bytes, so there is nothing to recover and no
            # way to verify a salvage. Ghostscript still produces a one-page PDF from pure
            # garbage; accepting that would append an invented page to the manual.
            results.append(Repair(p, rel, 'failed', 0, 0))
            out.append(p)
            continue
        sub = work / f'rep{i:05d}'
        sub.mkdir(parents=True, exist_ok=True)
        # STRICT first — exactly what the main tool does, but told how many pages to expect
        # so it rejects a partial salvage instead of quietly returning a shortened file.
        rep = owm._repair_pdf(p, sub, expect_pages=want)
        got = page_count(Path(rep)) if rep else -1
        if rep and got >= 0 and (not want or got >= want):
            out.append(Path(rep))
            results.append(Repair(p, rel, 'repaired', got, want))
            continue
        # It failed the strict bar. Repeat WITHOUT the expectation — the main tool's own
        # leniency — purely to measure what is actually recoverable, so the report can say
        # how much of the file is left rather than just 'failed'.
        sub2 = work / f'sal{i:05d}'
        sub2.mkdir(parents=True, exist_ok=True)
        sal = owm._repair_pdf(p, sub2)
        sgot = page_count(Path(sal)) if sal else -1
        out.append(p)                           # unusable — let the count refuse it
        results.append(Repair(p, rel, 'incomplete' if sgot > 0 else 'failed',
                             max(sgot, 0), want))
    return out, results


def _image_to_pdf_bytes(path: Path) -> bytes:
    """A one-page PDF wrapping the image. Fast path is img2pdf (embeds the JPEG
    losslessly, no re-encode); fall back to a Pillow re-wrap for images img2pdf
    rejects (alpha, palette, CMYK oddities)."""
    try:
        return img2pdf.convert(win_long(path))
    except Exception:
        im = Image.open(win_long(path))
        if im.mode in ('RGBA', 'LA', 'P'):
            im = im.convert('RGB')
        buf = io.BytesIO()
        im.save(buf, 'PNG')
        return img2pdf.convert(buf.getvalue())


class CombineFailed(Exception):
    """The combined PDF is not a faithful merge of its inputs — do not trust it, and
    above all do not delete the sources on the strength of it."""


def combine(files, out_pdf: Path, verify: bool = True, bookmarks: dict = None) -> int:
    """Merge `files` (images and/or PDFs), in the given order, into out_pdf. Returns the
    page count. Raises CombineFailed if an input cannot be read or merged, or if the
    result does not carry exactly the sum of the inputs' pages.

    Written to a .part first and moved into place only once verified, so a failure never
    leaves a half-written PDF that looks like a finished one.

    `bookmarks` maps an INDEX INTO `files` to an outline title (see `section_bookmarks`) —
    never a page number, because the page a file lands on is something only the merge knows.
    Outline items are catalogue objects, not pages, so they cannot move the count that guards
    this merge: `want` is still computed from the inputs before anything is appended, and it
    is still checked against the REOPENED result at the end."""
    want, bad = (0, []) if not verify else expected_pages(files)
    if bad:
        # EVERY unreadable input, by FULL path, one per line — never a count plus the first.
        # These are the files the run has to be fixed by hand, and a manual is combined from
        # hundreds of parts spread over as many subfolders: one real Toyota tree holds `ovi.pdf`
        # in 60 different ones, so a bare name does not say which to go and look at, and naming
        # only the first hides the other three. Matches the `--dry-run` listing above.
        listed = ''.join(f'\n       unreadable: {p}' for p in bad)
        raise CombineFailed(f'{len(bad)} unreadable input(s):{listed}')
    w = PdfWriter()
    marks = []
    for i, p in enumerate(files):
        first = len(w.pages)      # the index the next appended page will land at
        try:
            if is_pdf(p):
                w.append(win_long(p))
            else:
                w.append(io.BytesIO(_image_to_pdf_bytes(p)))
        except Exception as ex:
            raise CombineFailed(f'{p}: {type(ex).__name__}: {ex}') from ex
        if bookmarks and i in bookmarks:
            marks.append((bookmarks[i], first, p))
    # Page indices come from the merge, not from a prediction: a section whose first part is a
    # 9-page PDF must not have its bookmark land 8 pages early. An index past the end can only
    # mean that input contributed NO pages — a 0-page PDF, which `expected_pages` counts as 0
    # and which would otherwise merge invisibly — so it is refused, not quietly moved.
    n_pages = len(w.pages)
    for title, pg, src in marks:
        if pg >= n_pages:
            raise CombineFailed(f'bookmark {title!r} would point past the end (page {pg + 1} '
                                f'of {n_pages}): {src} contributed no pages')
        w.add_outline_item(title, pg)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_pdf.with_suffix(out_pdf.suffix + '.part')
    try:
        with open(win_long(tmp), 'wb') as f:
            w.write(f)
        got = page_count(tmp)
        if got < 0:
            raise CombineFailed('the combined PDF cannot be reopened')
        if verify and got != want:
            raise CombineFailed(f'page loss: inputs total {want} pages, combined has {got}')
        os.replace(win_long(tmp), win_long(out_pdf))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return got


def main():
    ap = argparse.ArgumentParser(
        description='Combine a folder of page images/PDFs into one PDF named after the folder.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('folder', type=Path, help='folder of loose page images / small PDFs')
    ap.add_argument('--recursive', action='store_true',
                    help='also walk subfolders (for a manual split into per-topic '
                         'subfolders); files and subfolders are ordered together by the '
                         'same natural key at every level')
    ap.add_argument('--order', choices=('natural', 'docid'), default='natural',
                    help="page order. 'natural' (default) sorts by filename, which is right "
                         "when the parts are numbered. 'docid' reads the publisher document "
                         "id out of each PDF's page-1 browser print header and sorts on "
                         'that — for a manual captured by printing an online one, whose '
                         'parts are named by topic so a filename sort is arbitrary. Falls '
                         'back to natural order for the whole folder unless EVERY part has '
                         'a doc id')
    ap.add_argument('--dry-run', action='store_true',
                    help='print the page order and output path, then stop (write nothing)')
    ap.add_argument('--no-repair', action='store_true',
                    help='do NOT try to repair unreadable inputs (qpdf, then Ghostscript) '
                         'before combining; refuse the merge instead. Repair is on by default '
                         'because it only ever substitutes a copy that recovered AT LEAST as '
                         'many pages as the original held — the source files are never modified')
    ap.add_argument('--skip-unrecoverable', action='store_true',
                    help='combine what IS readable when some parts cannot be repaired, instead '
                         'of refusing the whole manual. The skipped parts and the pages they '
                         'held are named in full; the files themselves are left where they are. '
                         'Opt-in, so an unattended run never ships a manual with pages missing')
    ap.add_argument('--no-compress', action='store_true',
                    help='produce the raw combined PDF only; skip the compress + OCR step')
    ap.add_argument('--language', default='auto',
                    help="OCR language for the compress step (default 'auto' — per-page "
                         'script detection); passed straight to ocrmyworkshopmanual.py')
    ap.add_argument('--tessdata', default=None,
                    help='TESSDATA_PREFIX dir for OCR language packs (e.g. for rus); '
                         'else the current environment / system default is used')
    args = ap.parse_args()

    folder = args.folder
    if not folder.is_dir():
        sys.exit(f'ERROR: not a folder: {folder}')

    files = collect(folder, recursive=args.recursive)
    if not files:
        sys.exit(f'ERROR: no image or PDF files {"under" if args.recursive else "directly in"} '
                 f'{folder}' + ('' if args.recursive else
                                ' (its files may be in subfolders — try --recursive)'))

    # Duplicates are resolved BEFORE ordering: a page's identity is what decides which copy of
    # it to merge, and that does not depend on where it sits in the sequence.
    n_found = len(files)
    files, dups = dedupe_pages(files)
    n_dropped = sum(len(g.drop) for g in dups)
    n_review = sum(1 for g in dups if not g.drop)

    order_note = ''
    if args.order == 'docid':
        files, missing = order_by_docid(files)
        order_note = (f'  [order: natural — {missing} part(s) carry no publisher doc id, '
                      f'so doc-id order was NOT applied]' if missing else
                      '  [order: publisher doc id]')

    marks = section_bookmarks(files, folder)
    out_pdf = folder.parent / (folder.name + '.pdf')
    n_img = sum(1 for p in files if not is_pdf(p))
    n_pdf = len(files) - n_img
    print(f'{len(files)} files ({n_img} image, {n_pdf} pdf) -> {out_pdf}{order_note}')
    if n_dropped:
        print(f'  {n_found} found, {n_dropped} dropped as low-resolution duplicates '
              f'(listed below; the files themselves are untouched)')
    if marks:
        print(f'  {len(marks)} section bookmark(s)')
    print()
    print('Page order:')
    for i, p in enumerate(files, 1):
        # under --recursive the bare name is ambiguous (every subfolder has a
        # `1. General Description.pdf`), so show the path relative to the folder
        label = p.relative_to(folder) if args.recursive else p.name
        print(f'  {i:>4}. {label}')

    # After the page order, not before it: on a 1200-page manual anything printed first is
    # scrolled off the screen by the order dump, and these are the lines to act on.
    report_dups(dups, folder)

    if args.dry_run:
        want, bad = expected_pages(files)
        if marks:
            print('\nBookmarks (predicted):')
            for title, page in bookmark_preview(files, marks):
                print(f'  {page:>5}  {title}')
        print(f'\nWould produce {want} pages from {n_found} file(s)'
              + (f', {n_dropped} duplicate(s) dropped' if n_dropped else '')
              + (f', {n_review} group(s) kept for review' if n_review else '')
              + (f' — WARNING: {len(bad)} unreadable input(s)' if bad else ''))
        for p in bad:
            print(f'    unreadable: {p}')
        if bad:
            # Say what the real run would DO about them, so a dry run is a faithful preview:
            # the difference between "would repair these" and "would refuse" is the whole
            # reason to look at a dry run first.
            print('    -> would ' + ('refuse the merge (--no-repair)' if args.no_repair else
                                     'try qpdf/Ghostscript repair first'
                                     + (', then leave out any that stay broken'
                                        if args.skip_unrecoverable else
                                        ', then refuse if any stays broken')))
        print('(--dry-run: nothing written)')
        return

    if out_pdf.exists():
        print(f'\nNOTE: overwriting existing {out_pdf.name}')

    # Repair unreadable parts BEFORE combining, in a scratch dir. One malformed page out of
    # hundreds would otherwise strand a whole manual as un-combinable, and these archives are
    # full of them. `repair_inputs` substitutes a repaired COPY only when it recovered at least
    # as many pages as the original's raw bytes say it held, so a partial salvage can never
    # pass itself off as complete; the originals on disk are never modified.
    work, skipped = None, []
    try:
        if not args.no_repair:
            unreadable = expected_pages(files)[1]
            if unreadable:
                print(f'\n{len(unreadable)} unreadable input(s) — trying qpdf/Ghostscript '
                      f'repair ...', flush=True)
                work = Path(tempfile.mkdtemp(prefix='combman_'))
                files, reps = repair_inputs(files, work, base=folder)
                for r in reps:
                    print('  ' + (
                        f'repaired: {r.src} ({r.recovered} pages)' if r.status == 'repaired'
                        else f'UNRECOVERABLE: {r.src} — only {r.recovered} of ~{r.expected} '
                             f'pages can be salvaged' if r.status == 'incomplete'
                        else f'UNRECOVERABLE: {r.src} — nothing could be read out of it'))
                broken = [r for r in reps if r.status != 'repaired']
                n_fixed = len(reps) - len(broken)
                if n_fixed:
                    print(f'  {n_fixed} repaired and merged from a scratch copy '
                          f'(the originals on disk are untouched)')
                if broken:
                    if not args.skip_unrecoverable:
                        lost = sum(r.expected or 0 for r in broken)
                        sys.exit(
                            f'ERROR: {len(broken)} input(s) could not be repaired'
                            + (f' (~{lost} page(s) affected)' if lost else '') + ':\n'
                            + ''.join(f'       {r.src}\n' for r in broken)
                            + f'       {folder} is untouched — do NOT delete it.\n'
                            + '       Pass --skip-unrecoverable to combine the readable parts '
                              'without them.')
                    # Combine what IS readable. Unlike the section tool this never deletes the
                    # source folder, so the damaged originals are LEFT WHERE THEY ARE rather
                    # than moved aside — moving files out of a tree this tool otherwise never
                    # touches would be a worse surprise than leaving them. They are named
                    # here and again in the summary, because a manual quietly missing pages is
                    # indistinguishable from a complete one.
                    bad_set = {r.src for r in broken}
                    skipped = broken
                    files = [p for p in files if p not in bad_set]
                    if not files:
                        sys.exit(f'ERROR: every input is unrecoverable — nothing to combine.\n'
                                 f'       {folder} is untouched.')
                    lost = sum(r.expected or 0 for r in broken)
                    print(f'  --skip-unrecoverable: leaving out {len(broken)} part(s)'
                          + (f', ~{lost} page(s)' if lost else '')
                          + ' — the files stay on disk, untouched')
                    marks = section_bookmarks(files, folder)   # indices shifted; recompute

        print(f'\nCombining -> {out_pdf} ...', flush=True)
        try:
            pages = combine(files, out_pdf, bookmarks=marks)
        except CombineFailed as ex:
            sys.exit(f'ERROR: combine refused: {ex}\n'
                     f'       {folder} is untouched — do NOT delete it.')
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)
    size_mb = out_pdf.stat().st_size / 1048576
    print(f'Combined: {len(files)} files -> {pages} pages, {size_mb:.1f} MB (page count verified)')
    if skipped:
        # Repeated AFTER the result, not only before it: this is the one fact that makes the
        # output incomplete, and on a long run the earlier notice has scrolled away. The page
        # count "verified" above is verified against what was MERGED, so it cannot reveal this.
        lost = sum(r.expected or 0 for r in skipped)
        print(f'INCOMPLETE: {len(skipped)} unrecoverable part(s) left out'
              + (f', ~{lost} page(s) missing' if lost else '') + ':')
        for r in skipped:
            print(f'  {r.src}'
                  + (f'  (~{r.expected} page(s), {r.recovered} salvageable)'
                     if r.expected else ''))
    if marks:
        # Read back out of the finished file, like the page count. A missing bookmark is
        # reported as what it is — lost navigation — and never as lost pages.
        got = outline_pages(out_pdf)
        print(f'Bookmarks: {len(got)} of {len(marks)} section(s) present in the output')
        for title, page in got:
            print(f'  {page:>5}  {title}')
        if len(got) < len(marks):
            print(f'  WARNING: {len(marks) - len(got)} bookmark(s) did not survive the write '
                  f'— navigation only, no page is affected')

    if args.no_compress:
        print('(--no-compress: raw combined PDF left as-is)')
        return

    env = dict(os.environ)
    if args.tessdata:
        env['TESSDATA_PREFIX'] = args.tessdata
    print(f'\nCompressing + OCR ({args.language}) in place via {TOOL.name} ...', flush=True)
    r = subprocess.run([sys.executable, str(TOOL), str(out_pdf),
                        '--in-place', '--language', args.language], env=env)
    if r.returncode != 0:
        sys.exit(f'ERROR: compress/OCR step failed (exit {r.returncode}); '
                 f'the raw combined PDF is still at {out_pdf}')
    print(f'\nDone -> {out_pdf}')


if __name__ == '__main__':
    main()
