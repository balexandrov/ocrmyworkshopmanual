# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- **Colour loss on colour line-art** (colour wiring diagrams / schematics). The
  page router gated its colour test behind continuous-tone *photo* coverage, so
  flat-colour line art (coverage ~0) was routed to the bitonal path and
  binarized to 1-bit b&w — destroying the colour — without the colour test ever
  running. Now a cheap colourspace pre-filter + a Ghostscript colour probe route
  genuine colour line art to a new lossless source-page pass-through
  (`PT_COLOR_LINE`); plain b&w and sepia pages are unaffected.
- **Colour, hyperlinks and quality loss on MIXED (part-vector, part-scanned)
  PDFs** — e.g. owner's manuals with a vector TOC/nav page plus scanned content:
  - Born-digital *vector* pages inside an otherwise-scanned file are now detected
    **per page** and passed through losslessly (`PT_VECTOR`), instead of the
    whole file being rasterized — so their vector text, colour and hyperlinks
    survive.
  - High-resolution scans are re-rendered **per page at their native DPI**
    instead of a fixed 200 dpi, so a 300/400-dpi scan is no longer downsampled
    (a visible quality loss); low-res pages keep the fast batched render so they
    don't bloat.
  - **Bookmarks** (document outline) are cloned onto the rebuilt PDF (1:1 page
    mapping) instead of being dropped.
- **OCR language detection robustness:**
  - Tesseract OSD mislabelled sparse English pages (wiring diagrams) as Cyrillic
    at low confidence, yielding slow, lower-quality `rus+eng` OCR. A per-page OSD
    confidence floor now ignores that noise; genuine Russian still detects.
  - A detected/requested language whose pack isn't installed silently failed OCR
    and dropped the whole text layer. The language is now filtered to installed
    packs, degrading to `eng`/available instead of failing.
- Very-high-page-count PDFs (multi-thousand-page manuals) failed with a Windows
  `WinError 206` ("filename or extension too long"): the JBIG2 wrapper was
  called with one command-line argument per page, overflowing the OS
  command-line length limit (~32K chars). Root fix (not a chunk/cap workaround):
  the wrapper now reads its page list from **stdin** (`jbig2topdf.py -s -`, one
  path per line) and the tool feeds it that way — a single call of any length,
  so the limit is eliminated as a failure mode. Verified end-to-end on a real
  3,573-page manual that previously failed outright.

### Changed

- Hardened the batch run against partial-failure modes found in the field:
  - A worker dying (`BrokenProcessPool` from an OOM/OS-kill/native crash) no
    longer aborts the whole run with an opaque traceback — the affected files
    are marked FAILED (originals untouched) and the run finishes with a
    complete report you can `--retry-failed`.
  - Console output is now crash-safe: if stdout closes (e.g. a `| head`
    reader exits), progress printing is swallowed instead of killing the run —
    the per-file report CSV is the durable record.
  - `Ctrl-C` writes the partial report for work done so far and exits 130.
  - Startup sweeps stale render-scratch dirs (`jb_*` / `jbprev_*`) left in the
    temp dir by earlier killed runs (a killed worker skips its cleanup), age-
    gated so a concurrently-running instance's active scratch is never touched.
  - A failed atomic swap no longer leaves a stray `.part` file behind.
  - The end-of-run summary calls out failures and points at `--retry-failed`.

### Added

- `--from-list FILE`: compress+OCR in place exactly the PDF paths listed in
  FILE, as one global worker pool spanning all of them. For hand-picking a
  subset of a huge tree without walking the whole thing — note plain folder
  mode already runs one global pool over every PDF under the tree (concurrency
  was never folder-limited in the tool itself). `src` is now optional when
  `--from-list` is given.
- `combine_manual.py`: combine a folder of loose page images (and/or small
  per-section PDFs) into one PDF named after the folder, in natural page order
  (`1-2` before `1-11`, `2a` before `2b`), written as a sibling of the folder,
  then compressed + OCR'd via the main tool by default. `--dry-run` prints the
  page order without writing; `--no-compress` leaves the raw combined PDF.
  Console-script entry point `combine-manual`.

## [0.1.0] - 2026-07-23

First versioned baseline. The project had already been through several
rounds of real-world hardening (see git history for the full detail) before
this tag; this entry summarizes where it landed.

### Added

- Core pipeline: render (Ghostscript) → per-page-type strategy → generic
  self-contained JBIG2 for bitonal pages → OCR text layer (ocrmypdf).
- Adaptive (background-flatten + Sauvola) binarization, tuned so faint
  strokes/dotted leaders survive on low-contrast/yellowed scans and gray
  washes resolve instead of speckling.
- Page-type router: LINE/BLANK (bitonal) vs. PHOTO_GRAY vs. PHOTO_COLOR, each
  with its own strategy; cast-robust colour detection (a sepia B&W page stays
  grayscale, not a yellow "colour" JPEG).
- Photo-page cleanup: paper whitening, dark scan-edge trim, descreen.
- Born-digital safety check (`looks_born_digital`) — vector/text PDFs are
  detected and copied through byte-for-byte, never rasterised.
- `--in-place` mode: atomic compress-to-scratch → verify (page count) →
  `os.replace`, so a failed verify never overwrites the original.
- `--dry-run` preview, `--ocr-only`, single-file input, config file support
  (`--config`/`ocrmyworkshopmanual.toml`), `--retry-failed`, duplicate
  flagging, PDF repair-and-retry, per-file timeout, disk-space guard,
  human-readable + CSV run reports (with a per-folder rollup).
- `--language auto`: per-file OCR language detection from the rendered image
  via Tesseract OSD script detection (Latin → eng, Cyrillic → rus+eng).
- `scan_candidates.py`: a companion script that ranks folders in a large
  archive by how much they'd benefit from compression (big and/or missing
  OCR), reusing the tool's own detection heuristics.
- Test suite: a committed real-scan fixture corpus (5 pages per page type),
  a settings-matrix tuning harness, and resilience/safety tests (timeout,
  output verification, born-digital, config file, in-place).
- Packaging: `pyproject.toml` (console-script entry points, `--version`),
  GitHub Actions CI (Python 3.10 + 3.11, builds Ghostscript/jbig2enc from
  scratch since no Debian/Ubuntu apt package ships the `jbig2` CLI), a
  contributor dev container, `CONTRIBUTING.md`.

### Changed

- CLI surface deliberately shrunk from ~35 flags to ~22: removed options
  whose only effect was to weaken a safety guarantee (born-digital bypass,
  output-verify skip, repair skip) or offer a legacy/strictly-worse mode
  (shared-dictionary JBIG2, which renders blank in Chrome/Edge; fixed-
  threshold binarization) — the underlying code paths were deleted, not just
  hidden behind a flag. See `CONTRIBUTING.md` for the reasoning.
- `--workers` now defaults to one per physical core (not logical/hyperthread)
  — the binarize step is memory-bandwidth-bound, so hyperthreads added little
  and oversubscribing them thrashed.

### Fixed

- A bright, low-saturation colour page (e.g. an orange cover) was
  mis-classified as blank and destroyed as bitonal; the coverage guard now
  keeps it on the photo/colour path.
- `--config`/TOML loading imported `tomllib` (Python 3.11+ stdlib)
  unconditionally on every run, so the tool couldn't start at all on Python
  3.10 even without using a config file — the import is now deferred until a
  config file is actually confirmed present, with a clear error (not a
  crash) if `tomllib` is unavailable when one is used.
- `--no-ocr` and `--ocr-only` could be combined with no warning, silently
  producing a no-op "copy but do nothing" result — now rejected with a clear
  error.
- No bounds validation on numeric options (`--dpi`, `--workers`,
  `--jpeg-quality`, etc.) — invalid values used to fail confusingly deep in a
  subprocess instead of a clear error at startup.
