# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed (hairlines lost on high-resolution scans)

Reported as "slight loss of thin lines" on a 600 dpi wiring diagram, and it was two separate
faults compounding.

- **Ghostscript was point-sampling every reduction.** GS does not average pixels when it
  scales a raster down unless told to, so reducing a 600 dpi bitonal scan to 200 dpi kept 1
  of every 9 source pixels and a hairline survived only if it happened to land on the sample
  grid. Measured on the reported page: the render contained **0.00%** mid-grey pixels, and
  the shipped page had its lines broken into **2,455** connected components. `-dDOINTERPOLATE`
  makes the reduction average, so a hairline arrives as grey — which is exactly what the
  Sauvola pass is for, and it keeps it. Same page, same 200 dpi, through the real binarizer:
  **2,455 → 1,425 components** (42% fewer fragments), ink 4.19% → 5.50%, and the JBIG2 came
  out **7.7% smaller**, because continuous lines cost fewer contexts to encode than dotted
  ones. The two halves only work as a pair: averaging alone, judged with a fixed global
  cutoff, looks like it changes nothing — which is why this was missed.
- **A striped scan read as 73 dpi instead of 604.** `_largest_image_dpi` divides the largest
  image's pixel count by the whole page area, which is only valid when that image covers the
  page. This page was stored as **68 full-width strips** of 4961×105 px, so the reading was
  off by sqrt(68) = 8.2×, leaving a 600 dpi scan sitting **23 dpi above `VECTOR_DPI_FLOOR`** —
  one shorter strip away from being classified born-digital and passed through unprocessed. A
  strip's width does not care how short it is (4961 / 8.26 in = 600 dpi), so an image at least
  4:1 wider than tall now also contributes a width-based reading. The aspect gate matters:
  applied to every image the width reading over-reads anything wide but not placed across the
  page (measured: a Subaru page 341 → 525 dpi), and over-reading pushes a page toward the
  raster path — the unsafe direction.
- The corrected reading feeds **classification only**. Letting it raise the render dpi to
  native measured **2× the output bytes and 2× the runtime** on the affected file, while
  interpolation already fixes the hairlines for 7.7% *less* — so the render path reads the dpi
  with `include_strips=False`, deliberately, and says why. (Note for future edits: the render
  loop calls the PLURAL `_page_render_dpis`; the singular `_page_render_dpi` serves only the
  photo re-render, so changing one without the other silently does nothing.)

Verified end to end on the reported file and the 54-file corpus. Page 609, both renders at
200 dpi: **2,455 → 1,425 connected components (42% fewer fragments)**, ink 4.19% → 5.50%, and
the file itself **24 MB → 22.9 MB** with only 2 of its 1179 pages taking native resolution
(genuine foldouts). Corpus: 938 MB → 583 MB where it was 584 MB, **0 files lost anything**, 21
colour files all kept their colour, and **24** files gained a searchable text layer where 23
did before — measured by the independent auditor, which shares no code with the pipeline.

### Fixed (a run whose console noise was hiding four ways to damage a file)

A 61-file pass printed ~120 unattributed PDF-library warnings. Chasing them down found
that the noise was mostly benign and the real defects were silent.

- **Library warnings now name their file and reach the report.** Nothing configured
  `logging`, so Python's `logging.lastResort` printed bare messages to stderr — no logger,
  no level, no filename — from six worker processes sharing one unlocked stderr, while the
  only per-file anchor on screen (`[n/61] name`) is printed at *completion* order. Warnings
  were therefore unattributable in principle, and recorded nowhere: grepping a run log for
  every message family returned 0. They are now captured per file in the worker (the pool
  is processes, so the handler must live there), deduped with counts, and carried on the
  result into the report `.log` and a new `warnings` CSV column. The console shows a compact
  `[3 pdf warnings]` tag; `--verbose` echoes each one prefixed with its file.
- **Repair silently preferred corrupt duplicates of an object.** A download that stitches a
  repeated chunk into a file leaves two definitions of every object in that range, damaged
  in *different* places, and qpdf resolves duplicates by last-definition-wins. Measured on a
  real Nissan manual (objects 626–665 duplicated, 96,032 bytes — exactly the gap between its
  linearization `/L` and the real file size): it chose a `/Widths` array with 221 entries for
  a 119-slot range, mis-advancing every heading glyph so `SECTION` rendered as `SECT I ON`,
  and an unparseable footer Form XObject, dropping the footer from all 21 pages — while the
  intact copies sat in the same file. Duplicates are now scored and the sound copy kept
  (neither "prefer the xref copy" nor "prefer the last" is right; each gets half of them
  wrong), the losing copy is blanked in place so no byte offset shifts, and what repair did
  is reported instead of vanishing.
- **The audit could not see dropped content or broken metrics.** Page count, link count and
  word recall all passed on that visibly wrong output. The audit now also fails a file whose
  page still paints an XObject the output no longer defines (a dangling `Do`), and one whose
  font `/Widths` contradicts its own `/FirstChar../LastChar`.
- **One unreadable page no longer blanks the whole text sample.** `_sampled_text` wrapped six
  pages in a single `try`, so a page whose content stream cannot be decoded returned `''` for
  the entire sample — silently disabling the text-survival check on the source side and
  faking `searchable text lost` on the output side, discarding a good file. Extraction is now
  per page, and pages neither side can read are excluded from *both* word sets.
- **Visible text nested in a Form XObject was invisible to the protection that exists for
  it.** `_largest_image_dpi` recursed into Form XObjects; `_visible_text_chars` did not. A
  producer that wraps a whole page in one form (measured on two iText Subaru manuals: page
  stream 27 bytes, `q /Xf1 Do Q`, with all 42 text ops and every raster nested inside) read
  as "full-page raster, no text" — a scan. One was rasterised to 39% and re-OCR'd, replacing
  ~590 chars/page of publisher type with an OCR guess. Both detectors now recurse to the same
  depth, and the hidden-text rule is evaluated within each stream so a 258 MB manual whose
  text is painted under its scan still compresses.
- **A born-digital file is no longer copied through without checking it opens.** That path
  copies bytes, so a corrupt source was reproduced as a file rendering nothing — the trap the
  OCR-only and kept-original paths were fixed for, which this one was missed by. It now
  repairs first, or fails the file loudly.
- **OSD could vote on a stale render.** `_detect_language` treated `png.exists()` as proof the
  render worked, so a leftover file from an earlier call in the same work dir passed it:
  measured, a PDF Ghostscript cannot open at all was labelled Cyrillic from a *different*
  manual's page.

### Changed

- **`--language` now defaults to `auto`**, and whatever the language came from, a source whose
  existing text layer proves another script gets that script's pack added. OCR'ing a Cyrillic
  manual as English does not read it worse — it replaces real text with Latin noise: a
  532-page Russian manual re-OCR'd as `eng` scored word recall 0.00 against its own source and
  the audit had to discard the whole file.
- **`looks_born_digital` is biased toward never damaging a file, not toward never skipping a
  scan.** A page carrying real *visible* text is a text page even when it also carries a
  full-page image. Visibility is judged by `_visible_text_chars`, never raw `extract_text()` —
  a scanned page with an invisible OCR layer has thousands of extractable chars, and keying
  off those would declare a genuinely scanned archive born-digital. Measured cost on a
  54-file corpus: 3 files stop compressing, 0.5 MB of savings, one of which is a file that
  should never have been rasterised in the first place.
- The report gains a machine-readable `page types` column (`line=12 vector=3`), so the
  classification that actually produced each output can be grouped on without regex-parsing
  the English `note`. With a review corpus grouped into per-type subfolders, the tool's
  existing per-folder rollup (`<report>_by_folder.csv`) becomes a per-type breakdown for free,
  because `--dest` mirrors source subfolders.
- **`verify_run.py` audits grouped corpora.** Pairs are matched by path relative to each tree
  rather than by a flat glob, so `before/` and `after/` may be organised into subfolders as long
  as they mirror each other; the report names each row by that relative path so its bucket is
  visible. A flat corpus takes the identical code path.
- `verify_run.py` no longer leaks pypdf warnings to stderr while auditing (a plain
  `NullHandler`, deliberately not the tool's own capture helper — this auditor shares no code
  with what it audits), and its summary percentage no longer misreports corpora under 1 MB: the
  divide-by-zero guard `max(total, 1)` was applied to a MEGABYTE total, so two identical 68 KB
  files were reported as "7%".
- **Companion scripts moved to `helpers/`** — `verify_run.py` is now
  `helpers/verify_run.py`. `ocrmyworkshopmanual.py` stays at the root with `scan_candidates.py`
  and `combine_manual.py`, which are `[project.scripts]` console entry points and would need a
  packaging change to move. Scripts under `helpers/` resolve `reports/` against the REPO ROOT,
  not their own folder: the `/reports/` rule in `.gitignore` is root-anchored, so a
  `helpers/reports/` would have started getting committed.

### Changed (OCR now reads the source, not our output)

- **OCR runs on the ORIGINAL at full resolution, before compression.** Every
  compression here is lossy, so OCR'ing the compressed result reads a degraded
  image: measured on a real page, OCR of the source made ~1 word error per 70
  where OCR of the shipped 150-dpi page made ~5, and re-rendering that page at
  higher dpi cannot recover the discarded detail. ocrmypdf writes its text layer
  as a self-contained Form XObject, which is grafted onto the compressed pages —
  so text quality is fully decoupled from image size.
- **Scan pages are OCR'd in their own document.** ocrmypdf's mode is per file but
  the requirement is per page: one vector page in a mixed manual forced
  `--skip-text`, which then skipped every scan page carrying a hidden text layer
  and shipped 37 characters where the source had 31,693. Scan pages are now
  separated, force-OCR'd (the only mode that reliably writes a harvestable text
  layer), and mapped back; vector pages keep the real text they already carry.
- **Pages with real text drawn ON TOP of a scan are never rasterized** — that is
  publisher content OCR cannot faithfully reproduce. Draw order decides it: text
  painted *before* a full-page image is hidden underneath and is treated as a
  regenerable layer (verified — such a page renders pixel-identically to its
  background image alone), so those files still compress.
- A missing language pack, a page holding only a page number (which `--skip-text`
  skipped entirely, shipping an English manual unsearchable), and OSD script
  misdetection are all handled; validated across English and Russian manuals.

### Fixed (data integrity — found by auditing a 64-file sample against its originals)

- **Silent page loss.** The output was verified against the RENDERED page count — the
  same number that had already lost the pages — so a corrupt 21-page manual whose
  repair salvaged 1 page passed the check and was shipped as a 1-page file. The source
  page count is now captured up front and everything is checked against it; a render or
  repair that loses pages fails the file and keeps the original.
- **Links, bookmarks and named destinations were dropped.** Rebuilding a PDF from
  rendered pages ships pages and nothing else: measured, a 36-page manual kept 5 of 249
  links, and a file lost all 248 bookmarks (their destinations point at other documents,
  so they resolve to no page number). The compressed pages are now grafted back INTO the
  original document (pikepdf/qpdf) — each page's content and resources are swapped and
  everything else is inherited, with the orphaned images dropped on write. Falls back to
  the plain rebuild, which the report then flags.
- **Corrupt PDFs are repaired properly.** Repair preferred Ghostscript, which salvaged
  1 of 21 pages from a real corrupt-at-source manual that qpdf recovers whole. Repair now
  tries qpdf first and REJECTS any repair that returns fewer pages than the source.
- **A corrupt PDF is no longer copied through untouched.** The paths that pass the source
  through byte-for-byte (`--ocr-only`, keep-original) faithfully reproduced broken files
  — the copy rendered nothing. They now probe whether the source renders, repair it if
  not, and fail rather than emit a copy that opens nowhere. Repairs are reported in the
  per-file note, never silent.

### Added

- `verify_run.py` — audit a run against the originals: point it at a folder with
  `before/` and `after/` copies and it compares every pair on page count, colour, links,
  bookmarks and searchable text, flagging anything that lost something. Compression is
  only trustworthy if you can show what survived.

### Changed

- **`--timeout` is now a STALL timeout, not a time budget** (default 7200 → 600).
  A flat wall-clock limit cannot tell "slow" from "stuck", so it killed healthy
  work purely for being big — a 6,855-page manual was marked FAILED at 2h while
  it was OCR'ing correctly. The value now means *max seconds a step may make no
  progress*: the page-render is killed only if no new page appears, the PDF
  repair only if the output stops growing. A slow-but-working file is never
  killed for its size, however long it takes; detection is size-independent.
  OCR (ocrmypdf) has no timeout at all — measured, it emits no usable progress
  signal (silent for ~90% of a run), so any bound there would only punish big
  files. Transient external-tool **crashes are retried** (3 attempts, backoff)
  and reported; a **stall is never retried**, since a hung or genuinely slow
  step behaves the same way next time and retrying just burns the time again.

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
