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

1. **Render** the page (Ghostscript).
2. **Classify** it by tiling and measuring continuous-tone content:
   - **line-art / text** → threshold + despeckle → **generic JBIG2** (tiny, crisp)
   - **photo / halftone** → keep as **grayscale JPEG** (or **color** if the page has color, e.g. covers)
3. Merge pages back in order.
4. **Skip it entirely** if a quick sample projects that compression won't shrink the
   file (already-efficient PDFs are kept as-is, never re-encoded/degraded).
5. **OCR** — add an invisible text layer with ocrmypdf, unless the file already has one.

Runs one worker process per file (uses all cores), at below-normal priority so your
machine stays responsive. Originals are never modified; output mirrors the source
tree under a sibling `"<src> (COMPRESSED)"` folder (or `--dest`). Skip-if-exists, so
it's resumable.

> ⚠️ For **scanned / image** PDFs only. Born-digital/vector PDFs would be rasterised.

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
| `--threshold T` | `125` | `gray < T` ⇒ ink (keep low, ~120–130) |
| `--min-size N` | `10` | Drop black speckles smaller than N px |
| `--no-despeckle` | off | Skip speckle removal |
| `--photo-threshold F` | `0.02` | Fraction of continuous-tone tiles that marks a page as a photo |
| `--photo-dpi N` | `150` | Downsample photo pages to this dpi (`0` = keep render dpi) |
| `--jpeg-quality Q` | `50` | JPEG quality for photo pages |
| `--min-savings F` | `0.10` | Keep the compressed file only if ≥ this fraction smaller; else keep original + OCR |
| `--precheck-threshold F` | `0.90` | Skip full compression if a sample projects the result ≥ this fraction of the original |
| `--no-precheck` | off | Always fully compress (disable the sample pre-check) |
| `--symbol` | off | Shared-dictionary JBIG2: ~30% smaller but **blank in Chrome/Edge** (Ghostscript/Acrobat only) |
| `--limit N` | `0` | Process only the first N files (testing) |

---

## Tuning notes (learned the hard way on real scans)

- **Keep `--threshold` low (~125).** Pages with a gray shaded background wash
  (common on foldout wiring diagrams) turn into salt-and-pepper **noise** at high
  thresholds like 190. A low threshold sends the wash to white and keeps ink crisp.
- **Generic (default) vs `--symbol`.** Chrome/Edge use PDFium, which renders a large
  shared JBIG2 dictionary as blank pages. Generic mode has no shared dictionary, so
  it works everywhere — at ~30% more size than symbol mode.
- **Photos are detected per-page and kept as images.** Pure-bitonal wrecks
  photographs; the tiled detector catches even a photo that fills only part of a page.
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
