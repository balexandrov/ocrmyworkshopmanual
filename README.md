# ocrmyworkshopmanual

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

[ocrmypdf](https://github.com/ocrmypdf/OCRmyPDF) is excellent and this tool uses it
for the OCR step. But for *these* inputs its built-in optimizer falls short:

- ocrmypdf only JBIG2-compresses images that are **already 1-bit**. It won't
  **binarize a grayscale scan**, so a grayscale line-art manual lands around **37%**
  (lossy JPEG) instead of **~8%** here.
- Its JBIG2 page-grouping is no longer configurable, so it can emit a
  **shared-dictionary** JBIG2 that renders as **blank pages in Chrome/Edge (PDFium)**.

This tool binarizes first, uses **generic** (self-contained) JBIG2 that renders
everywhere, keeps photos/color as images, and only then hands the file to ocrmypdf
for the text layer.

## What it does, per page

0. **Safety check first** — if a file doesn't look like a scan (it's a born-digital
   vector/text PDF), it is **copied to the destination byte-for-byte, untouched** —
   never rendered, binarized, or OCR'd. See [Born-digital safety](#born-digital-safety).
1. **Render** the page (Ghostscript).
2. **Classify** it into a **page type** and apply that type's strategy:
   - `LINE` / `BLANK` (text, line-art, gray-wash/shadow pages) → **background-flatten +
     Sauvola adaptive threshold** → **generic JBIG2** (tiny, crisp)
   - `PHOTO_GRAY` (B&W photo / halftone / stipple) → **whiten the paper + trim dark scan
     edges** → **grayscale JPEG**
   - `PHOTO_COLOR` (genuine color — covers, color diagrams) → **color JPEG**
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

**Python 3.10+**, then:

```bash
pip install -r requirements.txt
```

**External tools** (must be on your `PATH`):

| Tool | Purpose | Install |
|---|---|---|
| Ghostscript | render pages | Windows: [ghostscript.com](https://www.ghostscript.com/) · Debian/Ubuntu: `apt install ghostscript` · macOS: `brew install ghostscript` |
| jbig2enc (`jbig2`) | bitonal compression | Debian/Ubuntu: `apt install jbig2enc` · macOS: `brew install jbig2enc` · Windows: [releases](https://github.com/agl/jbig2enc/releases) (unzip, add `bin/` to PATH) |
| Tesseract OCR | text layer | Windows: [UB-Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki) · Debian/Ubuntu: `apt install tesseract-ocr` · macOS: `brew install tesseract` |

The `jbig2topdf.py` wrapper ships in `tools/` — you don't need to find it. If a tool
isn't on PATH, point to it with `JBIG2_GS` (Ghostscript) or `JBIG2_BIN` (jbig2).
`--no-ocr` skips the Tesseract/ocrmypdf requirement.

---

## Usage

```bash
# Compress + OCR a whole folder tree  ->  "<folder> (COMPRESSED)"
python ocrmyworkshopmanual.py "/path/to/scanned/folder"

# Preview a whole tree without writing anything (what would happen + projected savings)
python ocrmyworkshopmanual.py SRC --dry-run

# Test on the first few files
python ocrmyworkshopmanual.py SRC --limit 3

# Custom output, more workers
python ocrmyworkshopmanual.py SRC --dest OUT --workers 10

# Compress only (no text layer) / just add OCR, no compression
python ocrmyworkshopmanual.py SRC --no-ocr
python ocrmyworkshopmanual.py SRC --ocr-only

# Multilingual OCR
python ocrmyworkshopmanual.py SRC --language eng+fra+spa+deu
```

### Options

| Option | Default | Meaning |
|---|---|---|
| `src` (positional) | — | Source folder tree of scanned PDFs (required) |
| `--dest DIR` | `"<src> (COMPRESSED)"` | Output root |
| `--dpi N` | `200` | Render resolution (~native scan dpi is usually ~200–220) |
| `--workers N` | `min(10, cores)` | Files processed in parallel |
| `--language L` | `eng` | Tesseract language(s), e.g. `eng+fra+spa+deu` |
| `--no-ocr` | off | Skip the searchable text layer |
| `--ocr-only` | off | Don't compress — copy originals and only add OCR (skips files that already have text) |
| `--sauvola-k F` | `0.30` | Adaptive threshold sensitivity (lower = bolder/thicker ink, higher = thinner/cleaner) |
| `--global-threshold` | off | Legacy fixed-threshold binarization instead of adaptive (rarely better) |
| `--threshold T` | `125` | `gray < T` ⇒ ink — **only** used with `--global-threshold` |
| `--min-size N` | `10` | Drop black speckles smaller than N px |
| `--no-despeckle` | off | Skip speckle removal |
| `--no-photo-clean` | off | Don't whiten paper / trim dark edges on grayscale photo pages |
| `--photo-descreen F` | `0.6` | Descreen strength (gaussian σ, dpi-scaled) that merges halftone grain — less dithering + smaller (`0` = off) |
| `--photo-threshold F` | `0.02` | Fraction of continuous-tone tiles that marks a page as a photo |
| `--photo-dpi N` | `150` | Downsample photo pages to this dpi (`0` = keep render dpi) |
| `--jpeg-quality Q` | `60` | JPEG quality for photo pages |
| `--min-savings F` | `0.25` | Keep the compressed file only if ≥ this fraction smaller; else keep original + OCR |
| `--precheck-threshold F` | `0.75` | Skip full compression if a sample projects the result ≥ this fraction of the original |
| `--no-precheck` | off | Always fully compress (disable the sample pre-check) |
| `--symbol` | off | Shared-dictionary JBIG2: ~30% smaller but **blank in Chrome/Edge** (Ghostscript/Acrobat only) |
| `--scan-fraction F` | `0.5` | A PDF is treated as scanned (eligible for compression) only if ≥ this fraction of sampled pages carry a full-page raster image; below this it's considered born-digital and copied untouched |
| `--no-skip-born-digital` | off | Disable the born-digital safety check (rasterize **every** PDF, even vector/text ones) |
| `--dry-run` | off | Preview only: classify + project each file and report what **would** happen (+ projected savings); write nothing |
| `--timeout SECS` | `1800` | Max seconds for the render step and the OCR step per file; a file that exceeds it is marked FAILED and the batch continues (`0` = no timeout) |
| `--no-verify-output` | off | Skip the post-write check that each output opens and its page count matches the source |
| `--no-repair` | off | Don't attempt a Ghostscript pdfwrite repair on a malformed PDF before giving up |
| `--skip-duplicates` | off | Detect byte-identical duplicate PDFs (content hash); process each unique file once and copy its output to the twins |
| `--retry-failed CSV` | — | Reprocess **only** the files marked FAILED in a previous run's report `.csv` |
| `--min-free-gb N` | `1.0` | Abort before starting if the destination drive has less than N GB free (`0` disables) |
| `--config PATH` | `./ocrmyworkshopmanual.toml` | TOML file of default option values (CLI flags override it) |
| `--log PATH` | timestamped file in dest | Where to write the run report log (a `.csv` sibling is written too) |
| `--no-log` | off | Don't write a run report log |
| `--limit N` | `0` | Process only the first N files (testing) |

---

## Born-digital safety

This tool rasterizes each page, which is exactly what you want for **scanned** PDFs but
would **destroy** a born-digital (vector/text) PDF. So before touching a file it runs a
cheap check (`looks_born_digital`): it samples pages and measures the fraction that are
dominated by a full-page raster image. A real scan has one on ~every page; a born-digital
file has none. If the "scan fraction" is below `--scan-fraction` (default 0.5), the file
is **copied to the destination byte-for-byte — no render, no binarize, no OCR.**

- Conservative by design: ties fall to "scanned", so a genuine scan archive is never
  skipped. An all-raster "image PDF" (e.g. images exported to PDF) still counts as
  scanned and gets compressed — only real vector/text content is protected.
- A scanned PDF that already carries an OCR text layer is still detected as a scan
  (it has full-page images) and compressed normally.
- Disable with `--no-skip-born-digital` to force compression of everything.

## Run report log

After each folder run a report log is written (to the dest root by default, or `--log
PATH`; disable with `--no-log`). It records the settings used, then **per file** what
happened (`compressed` / `kept original` / `OCR-only` / `born-digital (copied untouched)`
/ `FAILED`) with sizes and the born-digital scan signals, and a final **summary tally +
total bytes saved** — so a big batch is reviewable at a glance. A machine-readable
**`.csv` sibling** is written alongside it (one row per file: action, sizes, %, note,
error) so you can sort/filter a collection of thousands.

## Resilience & preview (for large collections)

- **`--dry-run`** — preview a whole tree without writing anything: it classifies each
  file (born-digital? scanned?), projects the compressed size via the same sample
  pre-check the real run uses, and reports the per-file plan **plus projected total
  savings**. The report/CSV are written next to the source (never inside a created dest
  tree). Run this first on a big archive to see what you're in for.
- **`--timeout SECS`** (default 1800) — bounds the render and OCR steps per file. A
  pathological or corrupt PDF that would otherwise hang a worker forever is marked
  `FAILED` and the batch moves on; because it leaves no output, a later re-run retries
  it. Set `0` to disable.
- **Output verification** (on by default; `--no-verify-output` to skip) — after writing
  each output, it's re-opened and its page count checked against the source. A
  silently-corrupt result is flagged loudly in the log/CSV instead of shipping unnoticed.
- **Resumable** — outputs are skip-if-exists, so an interrupted run just continues where
  it left off, and failed files (no output written) are retried next time.
- **`--retry-failed report.csv`** — after a run, reprocess *only* the files the CSV marked
  `FAILED` (e.g. after freeing disk, fixing a tool, or raising `--timeout`), without
  re-scanning the whole tree.
- **`--skip-duplicates`** — large collections often contain byte-identical copies of the
  same PDF. This hashes each source, compresses each unique file **once**, and copies the
  result to the duplicate paths — so the output tree still mirrors the input, at a
  fraction of the compute.
- **PDF repair** (on by default; `--no-repair` to disable) — if a file is too malformed to
  render, it's rewritten through Ghostscript's `pdfwrite` and retried once before being
  given up on. One bad download shouldn't be silently lost.
- **`--min-free-gb N`** (default 1.0) — aborts up front if the destination drive is nearly
  full, instead of failing partway through a long run.

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
skip_duplicates = true
min_free_gb = 5.0
# no_ocr = true
# scan_fraction = 0.5
```

See `ocrmyworkshopmanual.example.toml` in the repo for a fuller template.

---

## Tuning notes (learned the hard way on real scans)

- **Adaptive binarization is the default and usually right.** On low-contrast/yellowed
  scans a single global cutoff erodes faint strokes and drops dotted leaders, while a
  high cutoff turns a gray shaded wash (common on foldout wiring diagrams) into
  salt-and-pepper noise. The default background-flatten + Sauvola adapts locally: it
  keeps faint ink *and* resolves the wash cleanly. A hard ink floor keeps **solid-black
  fills** (bold display type, filled tabs) solid — Sauvola alone hollows them out.
  `--sauvola-k` tunes boldness (lower = thicker). `--global-threshold` restores the old
  fixed-`--threshold` behavior.
- **Photo pages get their paper whitened.** Grayscale photo/mixed/stipple pages are
  flat-fielded (against a bright-paper envelope, so **solid black fills stay black** and
  aren't washed to gray) so the yellow paper goes white and the dark scan-edge border is
  trimmed. A soft-levels tone curve then adds contrast (deeper blacks) while a highlight
  knee keeps the photograph's bright tones from blowing out to white — so photos stay
  rich, not washed (`--no-photo-clean` to disable, `--jpeg-quality` for detail vs size).
  A mild **descreen** (`--photo-descreen`, default on) merges the scan's halftone dot
  grain into smooth tone — less "dithering" on photos/shaded diagrams, and smaller files.
- **Color detection ignores a sepia cast.** A yellowed B&W page would otherwise be
  mistaken for "color" and kept as a large yellow JPEG; the detector white-balances
  first, so only genuine color (covers, color diagrams) stays color.
- **Generic (default) vs `--symbol`.** Chrome/Edge use PDFium, which renders a large
  shared JBIG2 dictionary as blank pages. Generic mode has no shared dictionary, so
  it works everywhere — at ~30% more size than symbol mode.
- **Never grows a file.** If compression (or the sample pre-check) won't beat the
  original, the original images are kept untouched and only OCR is added.
- **Windows long paths.** Inputs longer than 260 chars are opened via the `\\?\`
  prefix (Ghostscript otherwise can't open them).

Rough size comparison on grayscale line-art scans: this tool ~8%, CCITT-G4 ~34%,
grayscale-JPEG ~47%, `ocrmypdf --optimize 3` ~37%.

## Limitations

- Best on scanned line-art/text; photo-heavy documents stay larger (they must, to
  keep the photos). Color/photo-heavy files may be kept as-is.
- Developed and tested on Windows; the code is cross-platform (paths, `os.nice`) but
  Linux/macOS are less exercised — reports welcome.

## License

MIT — see [LICENSE](LICENSE). Third-party tools and the bundled wrapper are covered
in [NOTICE](NOTICE). Ghostscript, jbig2enc, and Tesseract are invoked as external
programs (not redistributed here).
