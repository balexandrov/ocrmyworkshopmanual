# ocrmyworkshopmanual

[![CI](https://github.com/balexandrov/ocrmyworkshopmanual/actions/workflows/ci.yml/badge.svg)](https://github.com/balexandrov/ocrmyworkshopmanual/actions/workflows/ci.yml)

Turn a folder of **scanned, image-only PDFs** into small, **searchable** PDFs —
without wrecking photos or breaking in browsers.

It was built to archive decades of scanned automotive workshop manuals (hence the
name), but it works on any tree of scanned documents. For each page it decides the
right treatment, compresses to **JBIG2** where that helps, keeps photos as images,
and adds an invisible OCR text layer.

Typical result on clean black-and-white scans: **~8–12% of the original size**,
crisp, and full-text searchable.

---

## Why this exists (vs. `ocrmypdf --optimize 3`)

[ocrmypdf](https://github.com/ocrmypdf/OCRmyPDF) is excellent, and this tool uses it for the
OCR step. The gap isn't tuning: a general-purpose optimizer improves *the images it finds*,
while shrinking a scanned manual means deciding what each *page* is — then proving the decision
cost nothing.

- **It optimizes the wrong representation.** ocrmypdf JBIG2-compresses only images that are
  **already 1-bit**; it won't binarize a grayscale scan, so the one step that shrinks line-art
  4–5× never happens: **~8% here vs ~37%** with `--optimize 3`.
- **"Smaller" and "intact" are not the same, and file size cannot tell them apart** — losing a
  page, a colour, a link or the text layer *all make a PDF smaller*. So pages are sorted into
  six types before anything is re-encoded (colour wiring diagrams have line-art's low ink
  coverage but their colour *is* the information; binarizing them destroyed diagrams
  archive-wide here), and every output is audited against its source — page count, colour depth,
  font metrics, text by word recall, links, bookmarks — keeping the original if a check fails.
- **Compress-then-OCR reads a degraded image.** Measured: ~1 word error per 70 OCR'ing the
  source at 400 dpi, ~5× that off the shipped 150-dpi page. So OCR runs on the **original** and
  its text layer is grafted onto the compressed pages.
- **It has to render everywhere, and survive an archive.** Generic, self-contained JBIG2 only —
  PDFium (Chrome/Edge) draws a shared-dictionary JBIG2 as **blank pages**, and symbol mode being
  ~30% smaller doesn't buy that back. Plus resumable, one corrupt file can't take down the
  batch, malformed downloads repaired, duplicates flagged.

## What it does, per page

0. **Safety check first** — if a file doesn't look like a scan (it's a born-digital
   vector/text PDF), it is **copied to the destination untouched** — never rendered,
   binarized, or OCR'd (the one exception: a corrupt one is repaired first rather than
   reproduced unreadable). See [Born-digital safety](#born-digital-safety).
1. **Render** the page (Ghostscript).
2. **Classify** it into a **page type** and apply that type's strategy:
   - `LINE` / `BLANK` (text, line-art, gray-wash/shadow pages) → **background-flatten +
     Sauvola adaptive threshold** → **generic JBIG2** (tiny, crisp)
   - `PHOTO_GRAY` (B&W photo / halftone / stipple) → **whiten the paper + trim dark scan
     edges** → **grayscale JPEG**
   - `PHOTO_COLOR` (genuine color — covers, color diagrams) → **color JPEG**
   - `COLOR_LINE` (color **line-art** — wiring diagrams, schematics: low ink coverage but
     the color carries the meaning) → **passed through losslessly**, never binarized. Getting
     this wrong destroyed color wiring diagrams archive-wide, so it's a type of its own
   - `VECTOR` (a born-digital page *inside* a scanned PDF — a TOC, an index, real type over a
     background scan) → **passed through untouched**, so its vector text, color and
     hyperlinks survive
3. Merge pages back in order (consecutive bitonal pages share one JBIG2).
4. **Skip it entirely** if a quick sample projects that compression won't shrink the
   file (already-efficient PDFs are kept as-is, never re-encoded/degraded).
5. **OCR** — add an invisible text layer with ocrmypdf, unless the file already has one.

Binarization is **adaptive by default** (local, so faint strokes and dotted leaders on
low-contrast/yellowed scans survive and gray washes don't turn to speckle). Color
detection is **cast-robust**, so a sepia B&W page is kept as whitened grayscale rather
than a yellow "color" scan. To handle a new kind of page, add a page type + a
classifier rule + a strategy (see the `PT_*` constants and `classify_page`).

Runs one worker process per file (uses all cores), at below-normal priority so your
machine stays responsive. Originals are never modified; output mirrors the source
tree under a sibling `"<src> (COMPRESSED)"` folder (or `--dest`). Skip-if-exists, so
it's resumable.

> ⚠️ For **scanned / image** PDFs only — but a built-in safety check protects
> born-digital PDFs by copying them through untouched (see below), so it's safe to
> point at a mixed tree. Every folder run also writes a **report log** of what happened.

---

## Install

**Python 3.10+** (3.11+ if you use `--config`/an `ocrmyworkshopmanual.toml` — TOML
parsing needs the stdlib `tomllib`, only added in 3.11; everything else works on 3.10), then:

```bash
pip install -r requirements.txt
```

Or install it as a package (adds an `ocrmyworkshopmanual` console command; editable
install keeps running from this source tree):

```bash
pip install -e .
ocrmyworkshopmanual --version
```

**External tools** (must be on your `PATH`):

| Tool | Purpose | Install |
|---|---|---|
| Ghostscript | render pages | Windows: [ghostscript.com](https://www.ghostscript.com/) · Debian/Ubuntu: `apt install ghostscript` · macOS: `brew install ghostscript` |
| jbig2enc (`jbig2`) | bitonal compression | No Debian/Ubuntu apt package ships the `jbig2` CLI (only the library) — build from source: `apt install build-essential autoconf automake libtool libleptonica-dev`, then clone [agl/jbig2enc](https://github.com/agl/jbig2enc), `./autogen.sh && ./configure && make && sudo make install` · macOS: `brew install jbig2enc` · Windows: [releases](https://github.com/agl/jbig2enc/releases) (unzip, add `bin/` to PATH) |
| Tesseract OCR | text layer | Windows: [UB-Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki) · Debian/Ubuntu: `apt install tesseract-ocr` · macOS: `brew install tesseract` |

The `jbig2topdf.py` wrapper ships in `tools/` — you don't need to find it. If a tool
isn't on PATH, point to it with `JBIG2_GS` (Ghostscript) or `JBIG2_BIN` (jbig2).
`--no-ocr` skips the Tesseract/ocrmypdf requirement.

---

## Usage

```bash
# Compress + OCR a whole folder tree  ->  "<folder> (COMPRESSED)"
python ocrmyworkshopmanual.py "/path/to/scanned/folder"

# Process a SINGLE file (writes a sibling "<name> (COMPRESSED).pdf")
python ocrmyworkshopmanual.py "one_manual.pdf"

# Preview a whole tree without writing anything (what would happen + projected savings)
python ocrmyworkshopmanual.py SRC --dry-run

# Test on the first few files
python ocrmyworkshopmanual.py SRC --limit 3

# Custom output, more workers
python ocrmyworkshopmanual.py SRC --dest OUT --workers 10

# Compress only, no searchable text layer
python ocrmyworkshopmanual.py SRC --no-ocr

# Multilingual OCR
python ocrmyworkshopmanual.py SRC --language eng+fra+spa+deu
```

### Options

> **Concurrency note:** point `src` at a folder and the whole tree is walked
> recursively into **one** global worker pool — every PDF under it, in every
> subfolder, is fed to `--workers` processes at once. Concurrency is **not**
> limited per-folder; you don't need to do anything special to keep all workers
> busy across a deep tree.

| Option | Default | Meaning |
|---|---|---|
| `src` (positional) | — | Source folder tree of scanned PDFs (recursed into one global pool). Omit only when using `--from-list` |
| `--dest DIR` | `"<src> (COMPRESSED)"` | Output root |
| `--in-place` | off | **Overwrite** each PDF with its result (no output tree); leaves non-PDFs, structure, born-digital & already-optimal files untouched. Destructive — back up first |
| `--dpi N` | `200` | Render resolution (~native scan dpi is usually ~200–220) |
| `--workers N` | one per **physical** core | Files in parallel (binarize is bandwidth-bound, so hyperthreads add little; falls back to logical, then 4) |
| `--language L` | `auto` | Tesseract language(s). The default detects each file's script from the image (Tesseract OSD) and picks the language per file — `Latin`→`eng`, `Cyrillic`→`rus+eng`, CJK→`jpn+eng`. Pass an explicit spec to override, e.g. `eng+fra+spa+deu`; a source whose existing text layer proves another script still gets that pack added, because OCR'ing Cyrillic as English replaces real text with Latin noise |
| `--no-ocr` | off | Skip the searchable text layer |
| `--sauvola-k F` | `0.30` | Adaptive threshold sensitivity (lower = bolder/thicker ink, higher = thinner/cleaner) |
| `--min-size N` | `10` | Drop black speckles smaller than N px |
| `--no-despeckle` | off | Skip speckle removal |
| `--photo-descreen F` | `0.6` | Descreen strength (gaussian σ, dpi-scaled) that merges halftone grain — less dithering + smaller (`0` = off; paper-whitening/edge-trim always run regardless) |
| `--photo-threshold F` | `0.02` | Fraction of continuous-tone tiles that marks a page as a photo |
| `--photo-dpi N` | `150` | Downsample photo pages to this dpi (`0` = keep render dpi) |
| `--jpeg-quality Q` | `60` | JPEG quality for photo pages |
| `--min-savings F` | `0.25` | Keep the compressed file only if ≥ this fraction smaller; else keep original + OCR |
| `--min-compress-mb N` | `5` | Don't compress files smaller than this. The absolute win on a small file is a few hundred KB, against a lossy re-encode of every page — so they're passed through untouched and reported `kept original` / `small size`. **OCR is still checked and added** if the file has no text layer: too small to be worth compressing is not too small to be worth making searchable. `0` = compress everything |
| `--dry-run` | off | Preview only: classify + project each file and report what **would** happen (+ projected savings); write nothing |
| `--timeout SECS` | `600` | **Stall** timeout, not a time budget: max seconds a step may make **no progress** (no new page rendered, no bytes written) before it's treated as hung, killed, and the file marked FAILED. A slow-but-working file is never killed for being big, however long it takes. OCR is deliberately *not* bounded by it (ocrmypdf emits no usable progress signal, so any bound just killed healthy work). Transient crashes are retried automatically. (`0` = disable) |
| `--retry-failed CSV` | — | Reprocess **only** the files marked FAILED in a previous run's report `.csv` |
| `--from-list FILE` | — | Compress+OCR **in place** exactly the PDF paths listed in FILE (one per line), as one global pool. For hand-picking a subset of a huge tree; plain folder mode is already globally concurrent, so most users don't need this |
| `--min-free-gb N` | `1.0` | Abort before starting if the destination drive has less than N GB free (`0` disables) |
| `--config PATH` | `./ocrmyworkshopmanual.toml` | TOML file of default option values (CLI flags override it) |
| `--log PATH` | timestamped file in dest | Where to write the run report log (a `.csv` sibling is written too) |
| `--no-log` | off | Don't write a run report log |
| `--limit N` | `0` | Process only the first N files (testing) |
| `--verbose` | off | Also echo each PDF-library warning to the console, prefixed with the file it came from. They always go to the report `.log`/`.csv` regardless; this is for debugging one file without opening the report |
| `--version` | — | Print the version and exit |

A few things are **not** configurable on purpose, to keep the tool's guarantees simple
and hard to accidentally weaken: binarization is always the adaptive Sauvola threshold
(no legacy global-threshold mode), JBIG2 is always generic self-contained pages (no
shared-dictionary "symbol" mode, which renders blank in Chrome/Edge), the born-digital
safety check and the pre-write output verification always run, a malformed PDF always
gets one repair attempt, the pre-check that skips not-worth-it compression always runs,
photo detection always runs, the source tree is always walked recursively, grayscale
photo pages are always paper-whitened/edge-trimmed, and duplicate files are always
flagged (hashing every file is cheap next to actually compressing it). None of these
have a legitimate reason to be turned off — see [Born-digital safety](#born-digital-safety)
and [Resilience & preview](#resilience--preview-for-large-collections) below.

### Where to set `--min-compress-mb` (it is a real trade-off)

The default `5` is a **CPU-for-size** choice, not a free win, and it is worth knowing what it
costs before leaving it alone. Measured across 14 run reports (1,031 scanned file-rows) against
a 375k-file archive (40,041 scanned PDFs, 51.4 GB):

| band (MB) | median result | archive files | archive GB | GB saveable |
|---|---|---|---|---|
| 0 – 0.25 | **99% of original** | 26,094 | 2.4 | 0.02 |
| 0.25 – 5 | 45–57% of original | 11,938 | 14.0 | 3.62 |
| 5 + | 32% of original | 2,009 | 35.1 | 19.87 |

- **`5`** skips 95% of scanned files (31.7% of scanned *bytes*) and forfeits **~3.6 GB**. Cheapest
  to run. It also means those files get no visual clean-up, because the cleaned image *is* the
  compressed image here — skip the compression and you skip the background-flatten, the Sauvola
  threshold, the paper-whitening and the descreen with it.
- **`0.25`** skips only the band where compression provably does nothing (median 99% of original)
  — still the bulk of the file count, so most of the CPU saving, for ~0.02 GB forfeited.
- **`0`** compresses everything and relies on `--min-savings` to judge each file on its real
  result. Nothing forfeited; ~26k files get a full render+encode pass that is then discarded.

Whichever you pick, each run's summary prints the MB behind every reason
(`kept because: 67 small size (12.7 MB), …`), so the floor's cost on *your* tree is visible in
the report rather than something to estimate.

---

## In-place mode

`--in-place` compresses an existing library **where it sits** instead of building a
mirrored `(COMPRESSED)` tree — useful when reorganizing a second output folder is more
hassle than it's worth. **It overwrites the source PDFs, so back up first** (the tool
is built around you being able to restore from an archive if needed).

What it does and doesn't touch:
- **PDFs that compress** → overwritten with the smaller, searchable version.
- **Born-digital, already-optimal, or unchanged** PDFs → **left exactly as-is** (never rewritten).
- **Non-PDF files and the folder structure** → untouched.
- The run **report** is written to the **tool folder** (`reports/` next to the script),
  not among your manuals.

Safety mechanics:
- Each file is compressed to scratch, **verified** (opens + correct page count), and only
  then **atomically swapped** over the original (`os.replace`). A failed verify keeps the
  original.
- **Temp locations:** the heavy scratch (page renders, binarize, JBIG2, OCR) goes in the
  **system temp dir** (`%TEMP%`/`$TMPDIR`), *not* on your manuals drive, and is deleted
  per file. The only thing written beside a manual is a short-lived `<name>.pdf.part`
  staging file (required for an atomic same-volume swap), gone in a fraction of a second.
- Re-runs are safe: already-compressed files project ≥100% and are left untouched.

```bash
python ocrmyworkshopmanual.py "M:\manuals" --in-place          # whole tree, overwrite
python ocrmyworkshopmanual.py "one_manual.pdf" --in-place      # single file
```

## Finding what to compress (`scan_candidates.py`)

On a big, mixed archive it helps to know **which folders are even worth compressing**
before you point the tool at anything. `scan_candidates.py` walks a tree and ranks the
folders that hold *scanned* PDFs which would actually benefit — ones that are **big
(≥50 MB)** and/or **missing an OCR text layer**. It's **read-only** and never renders a
page: it reuses this tool's own `looks_born_digital` / `has_text` heuristics (so its
verdicts match what a real run would do) plus a `%PDF-` magic-byte gate to skip files
that only *look* like PDFs (HTML error pages saved with a `.pdf` name, etc.).

```bash
python scan_candidates.py "M:\manuals" --workers 16
```

Writes into `./reports`:

- `scan_candidates.csv` — candidate folders, biggest-first, with per-folder counts
  (scanned / big / missing-OCR), sizes, and the reason each qualified
- `scan_candidates.txt` — just the candidate folder paths (a feed list)
- `scan_all_folders.csv` / `scan_files.csv` — the full picture (every folder / every PDF)

Typical flow: scan → skim the ranked CSV → compress the folders you want (e.g. with
`--in-place`).

## Combining loose pages (`combine_manual.py`)

Some manuals arrive as a folder of **loose page images** (`1-1.jpg`, `1-2.jpg`,
… `2a-1.jpg`, `2b-1.jpg`, …) and/or small per-section PDFs, rather than one file.
`combine_manual.py` merges such a folder into a **single PDF named after the
folder**, written next to it, and then (by default) runs it through the main tool
to compress and add a searchable text layer.

```bash
python combine_manual.py "…\Honda\--Engines--\Haynes_ZC_Manual"
#   -> …\Honda\--Engines--\Haynes_ZC_Manual.pdf   (combined, then compressed+OCR'd)

python combine_manual.py FOLDER --dry-run       # just print the page order, write nothing
python combine_manual.py FOLDER --no-compress   # raw combined PDF only, skip compress/OCR
python combine_manual.py FOLDER --language eng+rus --tessdata C:\path\to\tessdata
```

- Uses only files **directly** in the folder (a `*_files` HTML-asset subdir, or a
  stray `.htm`/`.txt`, is ignored) — images (`jpg/png/tif/…`) and loose PDFs, mixed.
- Orders pages by a **natural sort**, so `1-2` comes before `1-11` and `2a` before
  `2b`. Page order is the one thing that has to be right, so it **always prints the
  order first** — run `--dry-run` to eyeball it before committing.
- Images are wrapped losslessly (img2pdf embeds the JPEG as-is, no re-encode). As
  usual, if the combined scan won't benefit from JBIG2 (photo-heavy Haynes-style
  pages), the compress step keeps the images and just adds the OCR layer.

## Born-digital safety

This tool rasterizes each page, which is exactly what you want for **scanned** PDFs but
would **destroy** a born-digital (vector/text) PDF. So before touching a file it runs a
cheap check (`looks_born_digital`): it samples pages and counts "scan pages" — those carrying
a full-page raster image *and* no real text. A real scan has one on ~every page; a born-digital
file has none. If that "scan fraction" is below 0.5 (fixed, not a flag — a real file is
essentially always overwhelmingly one or the other, so this isn't a knob worth exposing),
the file is **copied to the destination untouched — no render, no binarize, no OCR.**

The bias is **never damage a file**, not "never skip a scan". Rasterizing vector type is
damage; failing to compress something only costs savings. Concretely:

- A page carrying real **visible** text is a text page *even when it also carries a full-page
  image* — publisher type over a background scan is content OCR cannot faithfully reproduce.
  Measured cost of that bias on a 54-file corpus: 3 files stop compressing, 0.5 MB.
- Visibility is what's judged, never raw text extraction. A scanned page whose only text is an
  invisible OCR layer has thousands of extractable characters, so keying off those would
  declare a genuinely scanned archive born-digital and skip the whole thing; such a page still
  counts as a scan and compresses normally. Text painted *underneath* a full-page image is
  hidden too, and likewise doesn't protect the page.
- Text nested inside a Form XObject counts. Some producers wrap an entire page in one form, so
  the page's own content stream is a couple of dozen bytes — inspecting only that stream saw no
  text at all and let real type be rasterized and re-OCR'd.
- An all-raster "image PDF" (e.g. images exported to PDF, no text) still counts as scanned and
  gets compressed — only real vector/text content is protected.
- One exception to "untouched": if a born-digital file is too corrupt to render, copying its
  bytes would faithfully reproduce a file that opens nowhere, so it's **repaired** and the
  repaired copy is shipped (noted as `born-digital: repaired and copied`). Under `--in-place`
  it's reported as a failure instead, rather than rewriting your original.
- Always on — there's no flag to force-rasterize a file this check calls born-digital,
  because doing so would defeat the one thing this safety check exists for.

## Run report log

After each folder run a report log is written (to the dest root by default, or `--log
PATH`; disable with `--no-log`). It records the settings used, then **per file** what
happened, **why**, and **what became of its text layer**, with sizes and the born-digital
scan signals, and a final **summary tally + total bytes saved** — so a big batch is
reviewable at a glance. Two machine-readable **`.csv` siblings** are written alongside it
and **flushed per file** (so you can open them while a run is still going):
- the main `.csv` — one row per file: `file, action, reason, ocr, language, orig size (MB),
  new size (MB), %, duplicate of, page types, note, warnings, error` (sizes in MB; filter
  `error` non-blank to feed `--retry-failed`).

  The **four decision columns** each take values from a fixed vocabulary, so a run over
  thousands of files sorts, filters and pivots without reading the prose `note`:

  | column | values | answers |
  |---|---|---|
  | `action` | `compressed` · `kept original` · `FAILED` | What was done to the file |
  | `reason` | `compressible` · `born digital` · `already compressed` · `small size` · `error` | Why that happened |
  | `ocr` | `new ocr` · `re-ocr` · `kept existing` · `not requested` · `failed` | What became of the searchable text layer |
  | `language` | e.g. `eng`, `rus+eng` | Which packs OCR actually used (blank when no OCR ran, since until then the value is still the unresolved `auto`) |

  `re-ocr` vs `new ocr` is the difference between *replacing* a manual's existing text
  layer and giving it its first one — and `kept existing` means ocrmypdf never ran on it.
  `page types` is a machine-readable tally of how the pages were classified
  (`line=12 vector=3`), so a collection can be grouped by content type. `warnings` carries
  whatever the PDF libraries said about *that* file — see below.
- a `…_by_folder.csv` — one summary row per source subfolder (`folder, files, orig size
  (MB), new size (MB), %, saved (MB)`) plus a `(TOTAL)` row, so you can see which
  manual-series compress well. The same rollup is printed in the `.log`.

**Malformed-PDF warnings are attributed, not dumped.** The PDF libraries have plenty to say
about a damaged source (`invalid pdf header`, `incorrect startxref pointer`, …). Those messages
carry no filename, and with several worker processes writing to one console they interleave
across files — so they are captured per file instead, tallied with counts, and written into the
`.log` as `pdf warning:` lines under that file plus the `warnings` CSV column. The console shows
only a compact `[3 pdf warnings]` marker; `--verbose` echoes each one prefixed with its file.

## Resilience & preview (for large collections)

- **`--dry-run`** — preview a whole tree without writing anything: it classifies each
  file (born-digital? scanned?), projects the compressed size via the same sample
  pre-check the real run uses, and reports the per-file plan **plus projected total
  savings**. The report/CSV are written next to the source (never inside a created dest
  tree). Run this first on a big archive to see what you're in for.
- **`--timeout SECS`** (default 600 = 10 min) — a **stall** timeout, not a time budget: the
  most seconds a step may go without making progress (no new page rendered, no bytes
  written) before it's treated as hung. A pathological PDF that would otherwise hang a
  worker forever is marked `FAILED` and the batch moves on; because it leaves no output, a
  later re-run retries it. A slow-but-working file is never killed for being big, however
  long it takes — which is how a 6,855-page manual once "failed" while OCR'ing correctly, so
  OCR is deliberately not bounded by this at all (ocrmypdf gives no usable progress signal).
  Set `0` to disable.
- **Output verification** (always on) — after writing each output it is re-opened and audited
  against the **source**, because size alone cannot tell success from damage: losing a page, a
  colour, a link or the text layer all make a file *smaller*. Checked: exact page count; that
  a page classified as colour was not binarised to 1-bit; that no page still paints an
  XObject the output no longer defines; that font `/Widths` match their own
  `/FirstChar`..`/LastChar`; that searchable text survived, by **word recall** on sampled
  pages rather than character count (a legitimate re-OCR differs in character count); and
  that links and bookmarks did not shrink (a file that has none is unaffected — the count
  cannot drop below zero — so a plain scan still compresses). Any of these failing keeps the
  original rather than shipping the bad file, and is reported loudly in the log/CSV. For an
  independent second opinion
  after a run, `helpers/verify_run.py` re-audits a `before/`+`after/` pair from first
  principles — it deliberately shares no code with the tool it audits.
- **Resumable** — outputs are skip-if-exists, so an interrupted run just continues where
  it left off, and failed files (no output written) are retried next time.
- **`--retry-failed report.csv`** — after a run, reprocess *only* the files the CSV marked
  `FAILED` (e.g. after freeing disk, fixing a tool, or raising `--timeout`), without
  re-scanning the whole tree.
- **Duplicate flagging** (always on) — large
  collections often contain byte-identical copies of the same PDF. Each file's content
  hash is computed as it's processed and, when two files match, **both are flagged** in
  the report (console `[dup of …]`, a `duplicate_of` CSV column, and a note). Duplicates
  are **never skipped or merged** — a byte-identical file can legitimately belong to a
  different manual, so every file is still fully processed and gets its own output.
- **PDF repair** (always on) — if a file is too malformed to render, it's rewritten and
  retried before being given up on. One bad download shouldn't be silently lost. **qpdf
  first** (via pikepdf), Ghostscript's `pdfwrite` second: they fail differently, and on a real
  corrupt manual pdfwrite salvaged 1 of 21 pages where qpdf recovered all 21 — so a repair
  that returns fewer pages than the source is rejected outright and the next engine tried.
  Duplicate object definitions are resolved first: a download that stitches a repeated chunk
  into a file leaves two copies of each object in that range, damaged in *different* places,
  and qpdf's last-definition-wins silently picks corrupt ones (measured: a `/Widths` array
  with 221 entries for a 119-slot range, mis-advancing every heading glyph, plus a dropped
  footer form) — so each copy is scored and the one that validates is kept. What repair did
  is reported per file, never silent.
- **`--min-free-gb N`** (default 1.0) — aborts up front if the destination drive is nearly
  full, instead of failing partway through a long run.
- **Doesn't die on a partial failure** — a worker crashing (OOM, an OS kill, a native
  segfault → `BrokenProcessPool`) marks the affected files FAILED and lets the run finish
  with a complete report, instead of aborting everything with a traceback. Console output
  is crash-safe (a closed stdout pipe won't kill the run), `Ctrl-C` still writes the
  partial report, and stale render-scratch from earlier killed runs is swept from the temp
  dir at startup (age-gated, so a concurrent run's active scratch is safe).

## Config file

Instead of retyping flags, drop an `ocrmyworkshopmanual.toml` next to where you run the
tool (or point at one with `--config`). Keys are the long option names with dashes as
underscores; any explicit CLI flag still overrides the file.

```toml
# ocrmyworkshopmanual.toml
dpi = 200
workers = 8
language = "eng+deu"
jpeg_quality = 60
min_free_gb = 5.0
# no_ocr = true
```

See `ocrmyworkshopmanual.example.toml` in the repo for a fuller template.

---

## Tuning notes (learned the hard way on real scans)

- **Renders are interpolated, and that is not optional.** Ghostscript does not average pixels
  when it scales a raster down unless told to, so reducing a 600 dpi scan to 200 keeps 1 of
  every 9 source pixels and a hairline survives only if it lands on the sample grid. Measured
  on a 600 dpi wiring diagram: 0.00% mid-grey in the render, and the shipped page's lines
  broken into 2,455 pieces. With `-dDOINTERPOLATE` a hairline arrives as *grey* — which is
  precisely what adaptive binarization is for — giving 1,425 pieces instead of 2,455 and a
  JBIG2 **7.7% smaller**, since continuous lines cost fewer contexts than dotted ones. Note
  both halves are needed: averaging judged against a fixed global cutoff looks like it
  changes nothing, which is how this hid.
- **A scan's resolution is measured two ways.** The effective dpi of the largest image over
  the page area is only right when that image covers the page. Scans stored as full-width
  *strips* break it — 68 strips of 4961×105 px read as 73 dpi against a true 604 — so an image
  at least 4:1 wider than tall also contributes a width-based reading. That reading informs
  classification (so a striped 600 dpi scan can't be mistaken for born-digital) but
  deliberately does **not** raise the render dpi: doing so measured 2× the bytes and 2× the
  runtime, while interpolation already fixes the hairlines for less.
- **Adaptive binarization is the only mode — on purpose.** On low-contrast/yellowed
  scans a single global cutoff erodes faint strokes and drops dotted leaders, while a
  high cutoff turns a gray shaded wash (common on foldout wiring diagrams) into
  salt-and-pepper noise. The background-flatten + Sauvola approach adapts locally: it
  keeps faint ink *and* resolves the wash cleanly. A hard ink floor keeps **solid-black
  fills** (bold display type, filled tabs) solid — Sauvola alone hollows them out.
  `--sauvola-k` tunes boldness (lower = thicker); there's no fixed-threshold fallback
  mode, since it was strictly worse on the scans this tool targets.
- **Photo pages always get their paper whitened.** Grayscale photo/mixed/stipple pages are
  flat-fielded (against a bright-paper envelope, so **solid black fills stay black** and
  aren't washed to gray) so the yellow paper goes white and the dark scan-edge border is
  trimmed. A soft-levels tone curve then adds contrast (deeper blacks) while a highlight
  knee keeps the photograph's bright tones from blowing out to white — so photos stay
  rich, not washed (`--jpeg-quality` for detail vs size). This only ever helps, so it's
  always on — there's no legitimate case for a scanned photo page to skip it.
  A mild **descreen** (`--photo-descreen`, default on, `0` = off) merges the scan's
  halftone dot grain into smooth tone — less "dithering" on photos/shaded diagrams, and
  smaller files.
- **Color detection ignores a sepia cast.** A yellowed B&W page would otherwise be
  mistaken for "color" and kept as a large yellow JPEG; the detector white-balances
  first, so only genuine color (covers, color diagrams) stays color.
- **Generic JBIG2 only — no shared-dictionary mode.** Chrome/Edge use PDFium, which
  renders a large shared JBIG2 dictionary as blank pages. A "symbol" mode using that
  shared dictionary would be ~30% smaller, but a compressed manual that goes blank in
  the most common PDF viewers isn't a trade worth offering.
- **Never grows a file.** If compression (or the sample pre-check) won't beat the
  original, the original images are kept untouched and only OCR is added.
- **Windows long paths.** Inputs longer than 260 chars are opened via the `\\?\`
  prefix (Ghostscript otherwise can't open them).

Rough size comparison on grayscale line-art scans: this tool ~8%, CCITT-G4 ~34%,
grayscale-JPEG ~47%, `ocrmypdf --optimize 3` ~37%.

## Limitations

- Best on scanned line-art/text; photo-heavy documents stay larger (they must, to
  keep the photos). Color/photo-heavy files may be kept as-is.
- Developed on Windows and in daily production use there; CI runs the test suite on
  Linux (Ubuntu, Python 3.10/3.11) on every push, but macOS isn't automated yet —
  reports welcome.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup (including an optional
dev container), how to run the tests, and the design philosophy behind this
project's deliberately small CLI surface.

## License

MIT — see [LICENSE](LICENSE). Third-party tools and the bundled wrapper are covered
in [NOTICE](NOTICE). Ghostscript, jbig2enc, and Tesseract are invoked as external
programs (not redistributed here).
