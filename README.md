# ocrmyworkshopmanual

[![CI](https://github.com/balexandrov/ocrmyworkshopmanual/actions/workflows/ci.yml/badge.svg)](https://github.com/balexandrov/ocrmyworkshopmanual/actions/workflows/ci.yml)

Turn a folder of **scanned, image-only PDFs** into small, **searchable** PDFs — without wrecking
photos or breaking in browsers.

Built to archive decades of scanned automotive workshop manuals (hence the name), but it works on
any tree of scanned documents. For each page it decides the right treatment, compresses to
**JBIG2** where that helps, keeps photos as images, and adds an invisible OCR text layer.

- Clean black-and-white scans → **~8–12% of the original size**, crisp and full-text searchable
- **Born-digital** (vector/text) PDFs are never rasterised — they get a
  [lossless re-store](#lossless-rewrite) instead: same operators, same pixels, fewer bytes
  (**−62%** on a 513 MB Subaru manual)
- Safe to point at a **mixed tree**; every output is verified against its source before it ships

---

## Features

**Compression**
- **Page-type router** — six types, each with its own strategy; colour line-art and vector pages
  are passed through losslessly rather than binarised
- **Adaptive binarization** (background-flatten + Sauvola) so faint strokes and dotted leaders
  survive on yellowed scans, and gray washes don't turn to speckle
- **Generic, self-contained JBIG2** — renders everywhere, including Chrome/Edge
- **Photo handling** — paper whitening, dark scan-edge trim, descreen, tone curve with a
  highlight knee
- **Never grows a file** — a sample pre-check skips compression that wouldn't pay, and the
  original is kept if the real result misses `--min-savings`
- **[Lossless rewrite](#lossless-rewrite)** for born-digital PDFs: unfiltered streams Flated,
  objects bundled into `/ObjStm`, per-illustration authoring XMP dropped, optional zopfli

**Searchable text**
- OCR runs on the **original** and its text layer is grafted onto the compressed pages, so
  compression never degrades recognition
- **Per-file language detection** (`--language auto`) from the page image, plus the script its
  existing text layer proves
- **Stamp-aware** — a paywall watermark repeated on every page isn't mistaken for a text layer

**Safety**
- **[Born-digital detection](#born-digital-safety)** — always on, no flag to defeat it
- **[Output verification](#verification)** — always on: page count, colour depth, font metrics,
  text by word recall, links, bookmarks; a failed check keeps the original
- **Independent auditors** in `helpers/` that share no code with the tool they audit
- Originals untouched unless you ask for `--in-place`, which stages and atomically swaps

**Running it at archive scale**
- One worker per file, all cores, below-normal priority; **resumable** (skip-if-exists)
- **Stall** timeouts, automatic **PDF repair**, **duplicate flagging**, `--retry-failed`
- A worker dying doesn't abort the run; `Ctrl-C` still writes a complete report
- **[One CSV report](#run-report)** with four machine-groupable decision columns
- **`--dry-run`** previews a whole tree, including projected savings

**Companion tools**
- [`scan_candidates.py`](#finding-what-to-compress) — rank folders worth compressing (read-only)
- [`combine_manual.py`](#combining-loose-pages) — merge loose page images/PDFs into one manual
- [`helpers/`](#sweeping-an-existing-archive) — build work lists, audit a pass, promote results
- [Windows right-click menus](#windows-right-click-menus) for both tools

---

## Install

**Python 3.10+** (3.11+ for `--config`/TOML, which needs stdlib `tomllib`):

```bash
pip install -r requirements.txt
```

Or as a package (adds an `ocrmyworkshopmanual` console command):

```bash
pip install -e .
ocrmyworkshopmanual --version
```

**External tools** (must be on your `PATH`):

| Tool | Purpose | Install |
|---|---|---|
| Ghostscript | render pages | Windows: [ghostscript.com](https://www.ghostscript.com/) · Debian/Ubuntu: `apt install ghostscript` · macOS: `brew install ghostscript` |
| jbig2enc (`jbig2`) | bitonal compression | No apt package ships the CLI — build from source: `apt install build-essential autoconf automake libtool libleptonica-dev`, then clone [agl/jbig2enc](https://github.com/agl/jbig2enc), `./autogen.sh && ./configure && make && sudo make install` · macOS: `brew install jbig2enc` · Windows: [releases](https://github.com/agl/jbig2enc/releases) (unzip, add `bin/` to PATH) |
| Tesseract OCR | text layer | Windows: [UB-Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki) · Debian/Ubuntu: `apt install tesseract-ocr` · macOS: `brew install tesseract` |

The `jbig2topdf.py` wrapper ships in `tools/`. If a tool isn't on PATH, point at it with
`JBIG2_GS` (Ghostscript) or `JBIG2_BIN` (jbig2). `--no-ocr` drops the Tesseract requirement.
**Optional:** `pip install zopfli` enables `--lossless-zopfli`.

---

## Quick start

```bash
# Compress + OCR a whole tree  ->  "<folder> (COMPRESSED)"
python ocrmyworkshopmanual.py "/path/to/scanned/folder"

# One file (writes a sibling "<name> (COMPRESSED).pdf")
python ocrmyworkshopmanual.py "one_manual.pdf"

# Preview a tree without writing anything (plan + projected savings)
python ocrmyworkshopmanual.py SRC --dry-run

# First few files only, custom output, more workers
python ocrmyworkshopmanual.py SRC --limit 3
python ocrmyworkshopmanual.py SRC --dest OUT --workers 10

# Compress only / multilingual OCR
python ocrmyworkshopmanual.py SRC --no-ocr
python ocrmyworkshopmanual.py SRC --language eng+fra+spa+deu

# Overwrite an existing library where it sits (destructive — back up first)
python ocrmyworkshopmanual.py "M:\manuals" --in-place

# Lossless pass over the big born-digital PDFs a past run copied untouched
python helpers/lossless_candidates.py --min-mb 50
python ocrmyworkshopmanual.py --from-list reports/lossless_list.txt --dest OUT --no-ocr
```

> Point `src` at a folder and the whole tree is walked into **one** global worker pool — every
> PDF in every subfolder, fed to `--workers` processes at once. Concurrency is never limited
> per-folder.

---

## Options

| Option | Default | Meaning |
|---|---|---|
| `src` (positional) | — | Source folder tree (recursed into one global pool). Omit only with `--from-list` |
| `--dest DIR` | `"<src> (COMPRESSED)"` | Output root |
| `--in-place` | off | **Overwrite** each PDF with its result. Non-PDFs, structure and already-optimal files untouched. Born-digital files are never rasterised but *are* [re-stored losslessly](#lossless-rewrite) (`--no-lossless` to opt out). Destructive — back up first |
| `--dpi N` | `200` | Render resolution (native scan dpi is usually ~200–220) |
| `--workers N` | one per **physical** core | Files in parallel. OCR threads come from the same budget and follow how many files are still in flight, so the last file of a batch gets the cores the batch no longer needs |
| `--language L` | `auto` | Tesseract language(s). `auto` detects each file's script from the image — Latin→`eng`, Cyrillic→`rus+eng`, CJK→`jpn+eng` — and adds any pack the file's existing text layer proves it needs. Or pass a spec: `eng+fra+spa+deu` |
| `--no-ocr` | off | Skip the searchable text layer |
| `--sauvola-k F` | `0.30` | Threshold sensitivity (lower = bolder ink, higher = thinner) |
| `--min-size N` | `10` | Drop black speckles smaller than N px (an area at 300 dpi, scaled by dpi²) |
| `--no-despeckle` | off | Skip speckle removal |
| `--photo-descreen F` | `0.6` | Descreen strength (gaussian σ, dpi-scaled); `0` = off |
| `--photo-threshold F` | `0.02` | Fraction of continuous-tone tiles that marks a page as a photo |
| `--photo-dpi N` | `150` | Downsample photo pages to this dpi (`0` = keep render dpi) |
| `--jpeg-quality Q` | `60` | JPEG quality for photo pages |
| `--min-savings F` | `0.25` | Keep the compressed file only if ≥ this fraction smaller |
| `--min-compress-mb N` | `5` | Don't re-image files smaller than this; they're passed through and reported `small size`. **OCR is still added** if missing. `0` = compress everything. See [the trade-off](#the-size-floors) |
| `--no-lossless` | off | Don't [re-store](#lossless-rewrite) born-digital PDFs — copy them byte-for-byte |
| `--lossless-keep-xmp` | off | Compress the per-illustration authoring XMP instead of deleting it (costs about half the saving; keeps artwork provenance) |
| `--lossless-zopfli` | off | Re-Deflate every stream with zopfli: standard Deflate output, normal read speed, ~700× the encoder time, ~12% more. One-time passes, not routine runs. Needs `pip install zopfli` |
| `--lossless-min-savings F` | `0.03` | Discard the rewrite unless it's at least this much smaller |
| `--lossless-min-mb N` | `--min-compress-mb` | Size floor for the lossless lane alone. Lower it to sweep small born-digital files without letting the raster path re-image small scans |
| `--dry-run` | off | Preview only: classify, project, report; write nothing |
| `--timeout SECS` | `600` | **Stall** timeout, not a time budget: max seconds a step may make no progress before it's treated as hung. OCR is deliberately unbounded. `0` = disable |
| `--retry-failed CSV` | — | Reprocess only the files a previous report marked FAILED |
| `--from-list FILE` | — | Process exactly the PDFs listed in FILE, as one global pool. **In place** by default; `--dest DIR` writes a mirror tree keyed off the listed paths' common base |
| `--min-free-gb N` | `1.0` | Abort up front if the destination drive has less than N GB free (`0` disables) |
| `--config PATH` | `./ocrmyworkshopmanual.toml` | TOML of default values (CLI flags override) |
| `--log [PATH]` | off | Write the run report `.csv` — **omitted = console only**. Bare `--log` = current folder; `--log DIR`/`--log FILE` as given. This is what `--retry-failed` reads |
| `--limit N` | `0` | Process only the first N files (testing) |
| `--verbose` | off | Echo each PDF-library warning to the console too (they always reach the report's `warnings` column) |
| `--version` | — | Print the version and exit |

**Deliberately not configurable**, to keep the guarantees hard to weaken by accident: adaptive
binarization (no global-threshold mode), generic JBIG2 (no shared-dictionary mode), the
born-digital check, output verification, the one repair attempt on a malformed PDF, the
not-worth-it pre-check, photo detection, recursive walking, photo paper-whitening, and duplicate
flagging.

---

## How it works

### Per-page pipeline

0. **Safety check first** — a born-digital (vector/text) PDF is never rendered, binarized or
   OCR'd. It is copied byte-for-byte or [re-stored losslessly](#lossless-rewrite). A corrupt one
   is repaired rather than reproduced unreadable.
1. **Render** the page (Ghostscript, interpolated, at the page's own source resolution).
2. **Classify** it into a page type and apply that type's strategy:

   | page type | what it is | strategy |
   |---|---|---|
   | `LINE` / `BLANK` | text, line-art, gray-wash pages | background-flatten + Sauvola → **generic JBIG2** |
   | `PHOTO_GRAY` | B&W photo, halftone, stipple | whiten paper, trim scan edges → **grayscale JPEG** |
   | `PHOTO_COLOR` | genuine colour — covers, colour diagrams | **colour JPEG** |
   | `COLOR_LINE` | colour **line-art** — wiring diagrams, schematics | **passed through losslessly**, never binarized |
   | `VECTOR` | a born-digital page inside a scanned PDF — TOC, index, type over a scan | **passed through untouched** (vector text, colour, links survive) |

3. **Merge** back in order; consecutive bitonal pages share one JBIG2.
4. **Skip** compression entirely if a sample projects it won't shrink the file.
5. **OCR** — add an invisible text layer with ocrmypdf, unless the file already has one.

Add a page kind by adding a type + a classifier rule + a strategy (see the `PT_*` constants and
`classify_page`).

### Born-digital safety

`looks_born_digital` samples pages and counts "scan pages" — those carrying a full-page raster
image *and* no real text. A real scan has one on ~every page; a born-digital file has none. Below
a 0.5 scan fraction the file never enters the raster pipeline. The bias is **never damage a
file**: rasterizing vector type is damage, failing to compress only costs savings.

- **Visible** text counts, even over a full-page image — publisher type over a background scan is
  content OCR can't reproduce. Invisible OCR layers and text painted *under* an image don't
  count, or a genuinely scanned archive would read as born-digital and be skipped wholesale.
- Text inside a **Form XObject** counts; some producers wrap a whole page in one.
- An all-raster "image PDF" still counts as scanned and gets compressed.
- No flag can force-rasterize a file this check protects.

### Lossless rewrite

A born-digital PDF is big because of **how its bytes are stored**, not what it draws — and storage
can change without touching a drawing operator. Three tiers, on by default:

| tier | what it does | on the manual below |
|---|---|---|
| 1 | Flate the **unfiltered** streams, bundle objects into `/ObjStm`, re-Deflate at level 9 | 537.6 → 195.1 MB |
| 2 | delete the **per-illustration** authoring XMP (`--lossless-keep-xmp` to keep it) | included above |
| 3 | re-Deflate everything with **zopfli** (`--lossless-zopfli`, opt-in) | → ~172 MB |

Measured on `2020 WRX - WRX STI SERVICE MANUAL G1740BE.pdf` (537,575,830 bytes, 7,376 pages,
FrameMaker 7.2 → Distiller 9, PDF 1.4, 489,674 loose objects): **−62% in 3.2 minutes**, every
page's decoded content byte-for-byte identical.

**Where the bytes are.** Half that file — 254.6 MB — was XMP metadata stored with **no filter at
all**, and it was *per-illustration* metadata: each drawing's source `.ai`/`.eps` provenance hung
off its marked-content property dictionary (`Page /Resources /Properties /MC0…`), 44% of it a
base64 JPEG preview thumbnail. Two families exist and both are handled — 7,711 typed
`/Type /Metadata` packets and 9,898 untyped ones. The document-level packet in `/Root /Metadata`
is **kept**, and only the `/Metadata` key is deleted from each carrier, never the carrier itself,
since page content streams name those dictionaries via `BDC`.

**Expect the result to vary by producer, not by size** — it depends entirely on whether that
authoring chain wrote its metadata compressed:

| file | XMP found | result |
|---|---|---|
| 513 MB Subaru WRX (Distiller 9) | 7,710 **unfiltered** + 9,898 compressed | **−62%** |
| 236 MB Mitsubishi L200 (Distiller 6) | 0 unfiltered, 10,235 compressed | **−8%** |

**Why it can't damage a file.** No page is rendered, no image re-encoded, no operator touched.
Tiers 1 and 3 change only compression, and a recompressed stream is accepted only if it decodes
to identical bytes. Tier 2 is the only tier that alters the object graph. On top of that the
output is discarded and the original bytes copied unless it beats `--lossless-min-savings` **and**
matches the source on:

- page, annotation, bookmark and named-destination counts
- **document-wide decoded content bytes and stream-part count** — not a sample; this is the check
  that caught an early version dropping 9,898 XMP streams while every count and sampled page
  still matched
- per-page content-stream + XObject fingerprints across a spread of pages
- docinfo and document XMP, compared as **parsed fields** (pikepdf renormalises the packet on
  save, so bytes would differ on every file)

If the baseline can't be captured from the source, the rewrite is **skipped**, never treated as
passed. Under `--in-place` the source is fingerprinted, a temp written beside it, verified, then
atomically swapped — any failure leaves the original byte-identical. A corrupt-but-repairable file
still refuses in place, since repairing changes content rather than storage.

**Not preserved:** Fast Web View. The linearization hint stream (a pure index) is dropped;
relinearizing costs ~6 MB and 6× the save time and only matters for byte-range HTTP streaming.

**Not preserved: encryption.** An encrypted born-digital PDF is opened and re-stored
**decrypted**, and its owner permission flags (`extract`, `modify_*`) are dropped. Measured across
this archive's encrypted service manuals — RC4-128 from Acrobat Distiller 4 — every one opens with
an **empty user password**: the encryption holds permission flags, not a lock, and refusing those
files bought no safety. Passwords tried are `''` and `vector`; a file that fits neither is skipped
and reported as `encrypted: none of the known passwords fit`, never as unreadable or damaged.

This is the one thing the lane changes about a file besides how its bytes are stored, so it is
recorded per file in the report's `note` column. Page content is untouched and verified as above —
a permission flag cannot change what a page draws. Because bytes are not what these files buy,
they are also exempt from `--lossless-min-savings`: the bar is only *not bigger than the source*.
On a 26-file measurement the median was 3.6% smaller with 11 under the 3% default, so a size bar
would have left half of them encrypted for a rounding error.

### Text-layer decisions

**A text layer says something different on every page; a stamp says the same thing.** Text
repeating on ~every sampled page is discounted wherever the tool asks "is this already
searchable?", so a paywall watermark can't answer yes for a file with no text layer. Deliberately
narrow: ≥3 pages must carry text, a line must appear on ~90% of them, and the whole thing must be
at most 4 lines / 400 chars — twenty identical lines is a form template, and a template is
content. Applied **line-wise**, so a page with a repeated running header plus body text keeps its
body. The stamp is **never removed**; the file is reported with `boiler=2ln/66c` in
`scan signals`.

### Verification

Always on. After writing each output it is re-opened and audited against the **source**, because
size alone can't tell success from damage — losing a page, a colour, a link or the text layer all
make a file *smaller*. Checked: exact page count; a colour page wasn't binarised to 1-bit; no page
paints an XObject the output no longer defines; font `/Widths` match their own
`/FirstChar`..`/LastChar`; text survived by **word recall** on sampled pages (a legitimate re-OCR
differs in character count); links and bookmarks didn't shrink. Any failure keeps the original and
is reported on the console and in the CSV.

For an independent second opinion, `helpers/` has two auditors that deliberately share no code
with the tool — `verify_run.py` (colour from rendered pixels, text by word recall, structure via
pypdf) and `verify_lossless.py` (below).

---

## Working with a large archive

### In-place mode

`--in-place` compresses a library **where it sits** instead of mirroring it. It overwrites source
PDFs, so back up first.

- **PDFs that compress** → overwritten with the smaller, searchable version
- **Born-digital** → never rasterised; re-stored losslessly when that verifies, else left alone
- **Already-optimal or unchanged** PDFs, **non-PDFs**, folder structure → untouched
- Reports go only where `--log` says, never among your manuals

Each file is built in the **system temp dir** (not on your manuals drive), verified, then
**atomically swapped** (`os.replace`); the only thing written beside a manual is a short-lived
`.part`. Re-runs are safe: already-compressed files project ≥100% and are skipped.

### The size floors

`--min-compress-mb` (default 5) prices a **lossy re-encode of every page**; `--lossless-min-mb`
prices **churn on a file that is merely small**. They're separate knobs for that reason — lowering
the first to reach small born-digital files would also let the raster path re-image small scans.

Measured across 14 run reports (1,031 scanned file-rows) against a 375k-file archive (40,041
scanned PDFs, 51.4 GB):

| band (MB) | median result | archive files | archive GB | GB saveable |
|---|---|---|---|---|
| 0 – 0.25 | **99% of original** | 26,094 | 2.4 | 0.02 |
| 0.25 – 5 | 45–57% of original | 11,938 | 14.0 | 3.62 |
| 5 + | 32% of original | 2,009 | 35.1 | 19.87 |

`5` skips 95% of scanned files (31.7% of scanned *bytes*) and forfeits ~3.6 GB — and those files
also get no visual clean-up, since the cleaned image *is* the compressed image. `0.25` skips only
the band where compression provably does nothing, for ~0.02 GB forfeited. `0` compresses
everything and lets `--min-savings` judge each result. Every run prints the MB behind each reason
(`kept because: 67 small size (12.7 MB), …`), so the floor's cost on your tree is in the report.

### Finding what to compress

`scan_candidates.py` ranks the folders holding *scanned* PDFs that would actually benefit — big
(≥50 MB) and/or missing an OCR text layer. **Read-only**, never renders a page; reuses the tool's
own `looks_born_digital`/`has_text` heuristics so its verdicts match a real run, plus a `%PDF-`
magic-byte gate to skip HTML error pages saved as `.pdf`.

```bash
python scan_candidates.py "M:\manuals" --workers 16
```

Writes into `./reports`: `scan_candidates.csv` (ranked folders, counts, sizes, why each
qualified), `scan_candidates.txt` (just the paths, as a feed list), and
`scan_all_folders.csv` / `scan_files.csv` (the full picture).

### Sweeping an existing archive

Every born-digital file a past run touched was reported as `born digital`, so those rows already
are the inventory — no re-scan needed:

```bash
# 1. build the work list from the run reports you already have
python helpers/lossless_candidates.py --min-mb 50 --sample 8

# 2. rewrite into a staging tree, so a "before" copy still exists
python ocrmyworkshopmanual.py --from-list reports/lossless_list.txt --dest OUT --no-ocr --log reports

# 3. audit the pairs independently of the code that produced them
python helpers/verify_lossless.py --before SRC_ROOT --after OUT --render 3

# 4. replace the originals with the verified outputs
python helpers/promote_lossless.py --before SRC_ROOT --after OUT --audit reports/lossless_audit.csv --apply
```

`--in-place` skips steps 2–4 and is fully verified per file, but leaves **no before copy**, so
step 3 becomes impossible. On the first band that independent audit found three bugs — all of them
in the audit code, none in the rewrite — which is the argument for staging a large pass.

**`verify_lossless.py`** asks what the run's own guard never does: *of everything that
disappeared, what was it?* It pairs streams by the hash of their decoded bytes (qpdf renumbers
objects), tests **reachability** from the trailer so dead objects aren't mistaken for losses, and
classifies every removal as XMP packet, linearization hint stream, or **failure**. `--render N`
also compares raw pixels (Ghostscript) and per-page text (poppler `pdftotext`, which reaches deep
pages Ghostscript can't on flat-page-tree files).

**`promote_lossless.py`** promotes a file only if its audit row says `ok`, the output is genuinely
smaller, the page count still matches when re-checked, and the copy beside the original is
byte-identical to the audited output — then one atomic `os.replace`. `--audit` is optional.
Read-only originals (mode 444 / Windows `R`, common on files copied off a CD) would fail with
`PermissionError`; the flag is cleared and **left cleared**.

Two bands of one archive swept in full, the third a sample estimate:

| band | files | total | saving | cost |
|---|---|---|---|---|
| ≥ 50 MB | 127 | 17.0 GB | **−29%** (4.99 GB) — whole band | 27 min |
| 5–50 MB | 774 | 12.1 GB | **−12%** (1.49 GB) — whole band | 28 min |
| < 5 MB | 282,676 | 19.2 GB | ≈−12% (≈2.3 GB) — *300-file sample* | ~10 h |

**Don't size a sweep from a small sample.** A 10-file rewrite of the 5–50 MB band projected −36%;
all 774 gave **−12%** — median 14%, min 3%, max 94%, and 189 files with nothing to gain. A
signature scan mispredicts the *opposite* way: those files show 0% unfiltered bytes and almost no
XMP, which reads as "nothing here" while they yield 1.5 GB from object streams alone. The sub-5 MB
band is the largest pool and the worst value, and its cost is per-file **I/O**: 28 files/s on the
archive drive regardless of thread count, 8.1 files/s end-to-end.

### Combining loose pages

Some manuals arrive as a folder of loose page images (`1-1.jpg`, `2a-1.jpg`, …) and/or per-section
PDFs. `combine_manual.py` merges such a folder into a **single PDF named after the folder**, then
by default compresses and OCRs it.

```bash
python combine_manual.py "…\Honda\--Engines--\Haynes_ZC_Manual"
python combine_manual.py FOLDER --dry-run              # print the page order, write nothing
python combine_manual.py FOLDER --no-compress          # raw combined PDF only
python combine_manual.py FOLDER --recursive            # include subfolders, add bookmarks
python combine_manual.py FOLDER --skip-unrecoverable   # combine readable parts, name the rest
python combine_manual.py FOLDER --no-repair            # refuse instead of repairing
```

- Uses images and PDFs **directly** in the folder; HTML-asset subdirs and stray `.htm`/`.txt` are
  ignored. A PDF is recognised by its **header, not its extension**.
- **Natural-sort page order** that forgives how scans get named: separators carry no order
  (`EM11` is page 11), a `0` after a letter is an `O` (`B0-4`/`BO-2` are one chapter), mojibake
  bytes are ignored. It **always prints the order first** — `--dry-run` to check before writing.
- **Front matter leads its folder**: cover → foreword → index/contents → numbered pages. Matched
  on whole words, and the keyword must dominate the name, so `…-STEERING-COLUMN-COVER.jpg` and
  `DISCOVER.PDF` don't qualify. A publisher's shared prefix is stripped for a second attempt, so
  `PBGE95E1_…_COVER.pdf` leads instead of sorting last. For a print-captured manual whose
  filenames are topic names rather than page numbers, `--order docid` orders by the captured
  source URL instead.
- **The same page scanned twice** is merged once, by pixel area, and only if one copy is ≥2×
  smaller — similar resolutions may be two different pages, so both are kept and flagged.
  Grouping is per folder. Every drop is printed and **nothing is deleted from disk**.
- **An unreadable part is repaired, not fatal** — qpdf then Ghostscript, on a scratch copy. A
  repair is accepted only if it recovers at least as many pages as the original's raw bytes say it
  held, so a partial salvage can't pass as complete. Otherwise the merge is refused, listing every
  such file; `--skip-unrecoverable` combines the rest and prints an `INCOMPLETE:` summary before
  *and* after the result.
- Images are wrapped **losslessly** (img2pdf embeds the JPEG as-is).
- `--recursive` orders files and subfolders together at every level, so a subfolder's pages take
  their place in the sequence, and each section folder gets a **bookmark** at its first page.
- **The result is verified before the tool exits**: it must reopen and carry exactly the sum of
  its inputs' page counts, counted over the pages actually merged. Staged to a `.part` and moved
  into place only once that passes.

```
bertone\general info\GI-1.jpg …            21 sections, 1246 files
  ->  bertone.pdf   1220 pages, 135.0 MB, 26 low-res duplicates dropped, 21 bookmarks
```

### Windows right-click menus

Both `.reg` files write only under `HKEY_CURRENT_USER`, so **no admin rights**; double-click to
install, and each has an `-uninstall.reg`. On Windows 11 look under *Show more options*.

`tools\compress-pdf-context-menu.reg` adds **Compress + OCR (searchable)** to a `.pdf`. Defaults
mean your original is never touched — the result lands beside it as `<name> (COMPRESSED).pdf`, no
report files appear next to it, and the window stays open so you can read the log. It installs
under `SystemFileAssociations\.pdf`, so it survives changing your default viewer.

`tools\combine-pdf-context-menu.reg` adds a **Combine PDF** submenu to any folder:

| menu item | runs |
|---|---|
| Preview page order (writes nothing) | `--dry-run` |
| Combine into one PDF | `--no-compress` |
| Combine including subfolders | `--recursive --no-compress` |
| Combine, then compress + OCR | the full default pipeline (slow) |

The registry holds one path — `tools\combine_pdf_here.cmd` — which locates the script and the
repo's virtualenv relative to itself. If you move the checkout, edit that path and re-import.

---

## Run report

A run reports to the console and writes nothing but its output. **`--log`** keeps a report too,
never placed relative to the work being done. It is **one `.csv`**, flushed per file (openable
mid-run, and a killed run still has one), rewritten complete and sorted at the end.

One row per file: `file, action, reason, ocr, language, orig size (MB), new size (MB), %,
duplicate of, page types, scan signals, note, warnings, error`. `file` and `duplicate of` are
**full paths**, so a report read days later says which tree it came from; `--retry-failed` reads
them back but derives outputs from the path *relative* to the `src` you pass, so a retry can never
write over its source.

The **four decision columns** use fixed vocabularies, so a run over thousands of files sorts and
pivots without reading the prose `note`:

| column | values | answers |
|---|---|---|
| `action` | `compressed` · `kept original` · `FAILED` | What was done |
| `reason` | `compressible` · `lossless rewrite` · `born digital` · `already compressed` · `small size` · `error` | Why |
| `ocr` | `new ocr` · `re-ocr` · `kept existing` · `not requested` · `failed` | What became of the text layer |
| `language` | e.g. `eng`, `rus+eng` | Which packs OCR used (blank when none ran) |

`lossless rewrite` vs `compressible` is the distinction that matters for trust: both are
`compressed`, but the first means **no page rendered and no image re-encoded**. `re-ocr` vs
`new ocr` distinguishes replacing an existing text layer from giving a file its first.
`page types` tallies the classification (`line=12 vector=3`); `scan signals` carries the
born-digital scan's evidence (`scan_frac=0.033 scan_pages=1/30 text_pages=29 chars=8412`).

**Malformed-PDF warnings are attributed, not dumped.** Library messages carry no filename and
interleave across worker processes, so they're captured per file, tallied, and written to that
file's `warnings` column (`4x pypdf: incorrect startxref pointer`). Pre-scan warnings get one
`(pre-scan)` row whose `error` cell is blank, so `--retry-failed` never mistakes it for work. The
console shows a compact `[3 pdf warnings]`; `--verbose` echoes each.

---

## Resilience

- **`--dry-run`** — classify and project a whole tree, write nothing. With `--log` the report is
  marked `_DRYRUN`.
- **`--timeout SECS`** (default 600) — a **stall** timeout: seconds without progress, not a wall
  clock. A hung file is marked FAILED and, leaving no output, is retried on a later run. A
  slow-but-working file is never killed for being big — a 6,855-page manual once "failed" while
  OCR'ing correctly, so OCR is unbounded by it.
- **Resumable** — outputs are skip-if-exists; failed files wrote nothing and are retried.
- **`--retry-failed report.csv`** — reprocess only FAILED rows without re-scanning the tree.
  Entries since deleted are skipped and counted; entries not under `src` are reported loudly
  rather than guessed at.
- **Duplicate flagging** (always on) — content-hashed as they're processed; both copies are
  flagged (`[dup of …]`, a `duplicate_of` column). Never skipped or merged: a byte-identical file
  can legitimately belong to another manual.
- **PDF repair** (always on) — **qpdf first** (via pikepdf), Ghostscript's `pdfwrite` second;
  they fail differently, and a repair returning fewer pages than the source is rejected outright.
  Duplicate object definitions are scored and the copy that validates is kept — a stitched
  download leaves two copies damaged in *different* places, and last-definition-wins silently
  picks corrupt ones. What repair did is always reported.
- **`--min-free-gb N`** — abort up front rather than failing partway through.
- **Doesn't die on partial failure** — a worker crashing (OOM, OS kill, segfault →
  `BrokenProcessPool`) marks its files FAILED and lets the run finish with a complete report.
  Console output is crash-safe, `Ctrl-C` still writes the partial report, and stale scratch from
  killed runs is age-gated and swept at startup.

---

## Config file

Drop an `ocrmyworkshopmanual.toml` next to where you run the tool (or use `--config`). Keys are
long option names with dashes as underscores; explicit CLI flags still win.

```toml
dpi = 200
workers = 8
language = "eng+deu"
jpeg_quality = 60
min_free_gb = 5.0
# no_ocr = true
# lossless_min_mb = 5
```

See `ocrmyworkshopmanual.example.toml` for a fuller template.

---

## Tuning reference

| topic | what to know |
|---|---|
| **Interpolated renders** | Ghostscript doesn't average pixels when downscaling unless told to, so a 600→200 dpi reduction keeps 1 pixel in 9 and hairlines vanish. `-dDOINTERPOLATE` makes a hairline arrive as *grey* — what adaptive binarization is for — measured 1,425 line pieces instead of 2,455 and a JBIG2 **7.7% smaller** |
| **Resolution measured two ways** | Effective dpi over the page area is wrong for scans stored as full-width *strips* (68 strips read as 73 dpi against a true 604), so an image ≥4:1 wider than tall also contributes a width-based reading. It informs classification but deliberately doesn't raise render dpi (2× bytes, 2× runtime for no gain) |
| **`--sauvola-k`** | Boldness: lower = thicker ink. A hard ink floor keeps solid-black fills solid, which Sauvola alone hollows out |
| **Photo pages** | Always flat-fielded against a bright-paper envelope (so solid blacks stay black), edges trimmed, soft-levels curve plus a highlight knee so photos stay rich rather than washed. `--photo-descreen` merges halftone grain into smooth tone |
| **Colour detection** | White-balances first, so a yellowed B&W page isn't mistaken for colour and kept as a large yellow JPEG |
| **JBIG2 mode** | Generic only. A shared-dictionary "symbol" mode would be ~30% smaller, but PDFium (Chrome/Edge) renders it as **blank pages** |
| **Never grows a file** | If compression or the pre-check won't beat the original, images are kept untouched and only OCR is added |
| **Windows long paths** | Inputs over 260 chars are opened via the `\\?\` prefix |

Rough size comparison on grayscale line-art scans: **this tool ~8%**, CCITT-G4 ~34%,
grayscale-JPEG ~47%, `ocrmypdf --optimize 3` ~37%.

---

## Why not just `ocrmypdf --optimize 3`?

[ocrmypdf](https://github.com/ocrmypdf/OCRmyPDF) is excellent and this tool uses it for the OCR
step. A general-purpose optimizer improves *the images it finds*; shrinking a scanned manual means
deciding what each *page* is, then proving the decision cost nothing.

- **It optimizes the wrong representation** — ocrmypdf JBIG2s only images that are *already*
  1-bit and won't binarize a grayscale scan, so the step that shrinks line-art 4–5× never
  happens: **~8% here vs ~37%**.
- **"Smaller" and "intact" aren't the same, and size can't tell them apart** — losing a page, a
  colour, a link or the text layer all make a PDF smaller. Hence page types, and
  [verification](#verification) against the source.
- **Compress-then-OCR reads a degraded image** — ~1 word error per 70 off the 400 dpi source, ~5×
  that off the shipped 150 dpi page.
- **It has to render everywhere and survive an archive** — self-contained JBIG2 only, resumable,
  one corrupt file can't kill the batch.

## Limitations

- Best on scanned line-art/text; photo-heavy documents stay larger (they must, to keep the
  photos). Colour/photo-heavy files may be kept as-is.
- Developed and in daily use on Windows; CI runs the suite on Linux (Ubuntu, Python 3.10/3.11).
  macOS isn't automated yet — reports welcome.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup (including an optional dev container), how to
run the tests, and the design philosophy behind this project's deliberately small CLI surface.

## License

MIT — see [LICENSE](LICENSE). Third-party tools and the bundled wrapper are covered in
[NOTICE](NOTICE). Ghostscript, jbig2enc, and Tesseract are invoked as external programs and keep
their own licenses.
