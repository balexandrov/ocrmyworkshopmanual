# ocrmyworkshopmanual

[![CI](https://github.com/balexandrov/ocrmyworkshopmanual/actions/workflows/ci.yml/badge.svg)](https://github.com/balexandrov/ocrmyworkshopmanual/actions/workflows/ci.yml)

Turn a folder of **scanned, image-only PDFs** into small, **searchable** PDFs — without wrecking
photos or breaking in browsers.

Built to archive decades of scanned automotive workshop manuals (hence the name), but it works on
any tree of scanned documents. For each page it decides the right treatment, compresses to
**JBIG2** where that helps, keeps photos as images, and adds an invisible OCR text layer.

- Clean black-and-white scans → **~8–12% of the original size**, crisp and full-text searchable
- **Born-digital** (vector/text) PDFs are never rasterised — they get a
  [lossless re-store](docs/lossless.md) instead: same operators, same pixels, fewer bytes
  (**−62%** on a 513 MB Subaru manual)
- Safe to point at a **mixed tree**; every output is verified against its source before it ships

**Contents:** [Features](#features) · [Install](#install) · [Quick start](#quick-start) ·
[Options](#options) · [How it works](#how-it-works) · [Documentation](#documentation) ·
[Config file](#config-file) · [Why not `ocrmypdf --optimize 3`?](#why-not-just-ocrmypdf---optimize-3)

---

## Features

**Compression**
- **Page-type router** — six page types, each with its own strategy; colour line-art and vector
  pages are passed through losslessly rather than binarised
- **Adaptive binarization** (background-flatten + Sauvola), so faint strokes survive on yellowed
  scans and gray washes don't turn to speckle
- **Generic, self-contained JBIG2** that renders everywhere, including Chrome/Edge; photo pages
  get paper whitening, edge trim, descreen and a tone curve
- **Never grows a file** — a sample pre-check skips compression that wouldn't pay
- **[Lossless rewrite](docs/lossless.md)** for born-digital PDFs — no page rendered, fewer bytes

**Searchable text** — [OCR](docs/ocr.md)
- OCR reads the **original** page images and the text is grafted onto the compressed pages, so
  compression never degrades recognition
- **Per-file language detection** (`--language auto`)
- **A second engine for Japanese and Chinese**: `--ocr-engine paddle` (PaddleOCR PP-OCRv6), which
  found 25 of 28 diagram labels on Japanese wiring manuals where Tesseract found 14
- **An existing OCR layer is carried** onto the compressed pages instead of being re-OCR'd
- A file whose OCR fails is **FAILED**, never shipped without a text layer

**Source clean-up** — [each on by default and audited](docs/cleanup.md)
- **Re-distributor stamps removed** — a download site's domain on every page, deleted at the
  operator that draws it; the scan underneath is never touched
- **Box-shaped spaces fixed**, **per-page font subsets merged**, **encrypted PDFs decrypted**
- **Named lines stripped** on request — a printout service's running header or credit lines

**Browser-usable cross-references** — [links](docs/links.md)
- **`/GoToR` and `/Launch` rewritten to relative `/URI`**, in page annotations *and* bookmarks,
  because no browser follows either — so a contents page built from them does nothing
- Only links a target **beside the linking file**, never a same-named file found elsewhere in the
  tree: that one is usually the wrong car

**Safety** — [how nothing gets damaged](docs/safety.md)
- **Born-digital detection** — a vector/text PDF is never rasterised, and no flag overrides it
- **Every output is audited against its source** before it replaces anything: pages, colour
  depth, fonts, text, links, bookmarks. A failed check keeps the original
- Originals untouched unless you ask for `--in-place`, which stages and atomically swaps

**Archive scale** — [working with a large archive](docs/archive.md)
- One global worker pool over the whole tree; **resumable**, `--dry-run`, `--retry-failed`
- Stall timeouts, automatic PDF repair, duplicate flagging; one crashed worker doesn't end the run
- **[One CSV report](docs/report.md)** with four machine-groupable decision columns

**Companion tools** — [tools](docs/tools.md)
- `combine_manual.py` merges loose page images/PDFs into one manual; `scan_candidates.py` ranks
  folders worth compressing; `helpers/` builds work lists, audits a pass and promotes results
- `pdflinks.py`, `pdfspaces.py`, `pdffonts.py`, `pdfwatermark.py` run each clean-up stage on its
  own; `ocrmypdf_paddle.py` is the paddle engine as a plain OCRmyPDF plugin
- Windows right-click menus: compress a PDF, compress a folder in place, combine a folder

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
**Optional:** `pip install zopfli` enables `--lossless-zopfli`. `pip install rapidocr onnxruntime`
enables `--ocr-engine paddle`; install `onnxruntime-directml` (Windows, any DirectX 12 GPU) or
`onnxruntime-gpu` (CUDA) *instead of* `onnxruntime` to run it on a GPU. The models are downloaded
on first use.

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
| `--in-place` | off | **Overwrite** each PDF with its result. Non-PDFs, structure and already-optimal files untouched. Born-digital files are never rasterised but *are* [re-stored losslessly](docs/lossless.md) (`--no-lossless` to opt out). Destructive — back up first |
| `--dpi N` | `200` | Render resolution (native scan dpi is usually ~200–220) |
| `--workers N` | one per **physical** core | Files in parallel. OCR threads come from the same budget and follow how many files are still in flight, so the last file of a batch gets the cores the batch no longer needs |
| `--language L` | `auto` | Tesseract language(s). `auto` detects each file's script from the image — Latin→`eng`, Cyrillic→`rus+eng`, CJK→`jpn+eng` — and adds any pack the file's existing text layer proves it needs. Or pass a spec: `eng+fra+spa+deu` |
| `--ocr-engine E` | `tesseract` | `tesseract` or `paddle` (PaddleOCR PP-OCRv6 — [OCR engines](docs/ocr.md#ocr-engines)). A file in a language `paddle` does not read still goes to Tesseract, and the report note says so |
| `--paddle-dpi N` | `0` | With `--ocr-engine paddle`: read pages scanned finer than N dpi at N — faster, and the text layer is still mapped onto the full page. `0` = native resolution, the most accurate |
| `--no-ocr` | off | Skip the searchable text layer |
| `--sauvola-k F` | `0.30` | Threshold sensitivity (lower = bolder ink, higher = thinner) |
| `--min-size N` | `10` | Drop black speckles smaller than N px (an area at 300 dpi, scaled by dpi²) |
| `--no-despeckle` | off | Skip speckle removal |
| `--photo-descreen F` | `0.6` | Descreen strength (gaussian σ, dpi-scaled); `0` = off |
| `--photo-threshold F` | `0.02` | Fraction of continuous-tone tiles that marks a page as a photo |
| `--photo-dpi N` | `150` | Downsample photo pages to this dpi (`0` = keep render dpi) |
| `--jpeg-quality Q` | `60` | JPEG quality for photo pages |
| `--min-savings F` | `0.25` | Keep the compressed file only if ≥ this fraction smaller |
| `--min-compress-mb N` | `5` | Don't re-image files smaller than this; they're passed through and reported `small size`. **OCR is still added** if missing. `0` = compress everything. See [the trade-off](docs/archive.md#the-size-floors) |
| `--no-decrypt` | off | Leave encrypted PDFs encrypted. Decryption is on by default so a file's lock doesn't depend on which lane it took; see [encryption](docs/cleanup.md#encrypted-pdfs) |
| `--no-dewatermark` | off | Leave a [re-distributor stamp](docs/cleanup.md#re-distributor-stamps) in place. Removal is on by default and costs a two-page sample on a file without one |
| `--no-fix-spaces` | off | Leave [box-shaped spaces](docs/cleanup.md#box-spaces) as they are. The fix is on by default and costs one font walk on a file without the fault |
| `--no-merge-fonts` | off | Leave [per-page font subsets](docs/cleanup.md#per-page-font-subsets-pdffontspy) as they are. Merging is on by default and costs one font walk on a file with none |
| `--no-fix-links` | off | Don't [rewrite browser-dead cross-file links](docs/links.md). The rewrite is on by default: `/GoToR` and `/Launch` become relative `/URI`, in annotations and bookmarks. Internal `/GoTo` is never touched, so re-runs are no-ops |
| `--no-lossless` | off | Don't [re-store](docs/lossless.md) born-digital PDFs — copy them byte-for-byte |
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

0. **Clean the source** — decrypt, [remove a re-distributor stamp](docs/cleanup.md#re-distributor-stamps),
   [fix box spaces](docs/cleanup.md#box-spaces), [merge per-page font subsets](docs/cleanup.md#per-page-font-subsets-pdffontspy).
   Each stage audits its own rewrite and only changes what the later stages *read*; nothing is
   written over the original until the finished file has been verified.
1. **Safety check** — a born-digital (vector/text) PDF is never rendered, binarized or
   OCR'd. It is copied byte-for-byte or [re-stored losslessly](docs/lossless.md). A corrupt one
   is repaired rather than reproduced unreadable.
2. **Render** the page (Ghostscript, interpolated, at the page's own source resolution).
3. **Classify** it into a page type and apply that type's strategy:

   | page type | what it is | strategy |
   |---|---|---|
   | `LINE` / `BLANK` | text, line-art, gray-wash pages | background-flatten + Sauvola → **generic JBIG2** |
   | `PHOTO_GRAY` | B&W photo, halftone, stipple | whiten paper, trim scan edges → **grayscale JPEG** |
   | `PHOTO_COLOR` | genuine colour — covers, colour diagrams | **colour JPEG** |
   | `COLOR_LINE` | colour **line-art** — wiring diagrams, schematics | **passed through losslessly**, never binarized |
   | `VECTOR` | a born-digital page inside a scanned PDF — TOC, index, type over a scan | **passed through untouched** (vector text, colour, links survive) |

4. **Merge** back in order; consecutive bitonal pages share one JBIG2.
5. **Skip** compression entirely if a sample projects it won't shrink the file.
6. **OCR** — the original page images are read by ocrmypdf with the chosen
   [engine](docs/ocr.md#ocr-engines) and the invisible text layer is grafted onto the compressed pages. A
   page that already has one keeps it — [carried across](docs/ocr.md#carried-ocr-layers) when it is
   separable, left alone when the whole file is already searchable.
7. **Fix links** — last, on the shipped file, so every lane gets it:
   [`/GoToR` → relative `/URI`](docs/links.md).

Add a page kind by adding a type + a classifier rule + a strategy (see the `PT_*` constants and
`classify_page`).

Nothing is written over an original until the finished file has been audited against it; a
failed check keeps the original and says why. See [Safety](docs/safety.md).

---

## Documentation

| page | what's in it |
|---|---|
| [Source clean-up](docs/cleanup.md) | encrypted PDFs, re-distributor stamps, box-shaped spaces, per-page font subsets, stripping named lines |
| [OCR](docs/ocr.md) | the two engines and how they measured, carried OCR layers, awkward sources, stamp-aware text checks, when OCR fails |
| [Lossless rewrite](docs/lossless.md) | re-storing born-digital PDFs smaller, why it can't damage a file, sweeping an existing archive |
| [Browser links](docs/links.md) | why `/GoToR` is dead in browsers, what the rewrite handles, why it never searches the tree |
| [Safety](docs/safety.md) | born-digital detection, output verification, resilience (timeouts, repair, duplicates, crashes) |
| [Working with a large archive](docs/archive.md) | in-place mode, the size floors and what they cost, finding what to compress |
| [Run report](docs/report.md) | the CSV's columns and their fixed vocabularies |
| [Companion tools](docs/tools.md) | `combine_manual.py`, Windows right-click menus |
| [Compression tuning](docs/tuning.md) | render, threshold, photo and JBIG2 details, size comparison |

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

## Why not just `ocrmypdf --optimize 3`?

[ocrmypdf](https://github.com/ocrmypdf/OCRmyPDF) is excellent and this tool uses it for the OCR
step. A general-purpose optimizer improves *the images it finds*; shrinking a scanned manual means
deciding what each *page* is, then proving the decision cost nothing.

- **It optimizes the wrong representation** — ocrmypdf JBIG2s only images that are *already*
  1-bit and won't binarize a grayscale scan, so the step that shrinks line-art 4–5× never
  happens: **~8% here vs ~37%**.
- **"Smaller" and "intact" aren't the same, and size can't tell them apart** — losing a page, a
  colour, a link or the text layer all make a PDF smaller. Hence page types, and
  [verification](docs/safety.md#verification) against the source.
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
