# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added (consolidate a manual published as thousands of small PDFs)

Some manuals ship as one small PDF per topic — `USDM Forester FSM 2006\BODY SECTION\AIRBAG
SYSTEM AB\1. General Description.pdf` and 28,775 siblings. They are unusable as documents
and they defeat compression. New `helpers/combine_sections.py` turns each section folder
into one PDF named after it, verifies it, and (with `--delete`) removes the folder it came
from. Run over one real make: **28,776 small PDFs became 308 section PDFs, 170,010 pages,
303 source folders deleted, 0 failures.**

- `combine_manual.py` gained **`--recursive`** — its `collect` was non-recursive, so a
  section whose pages live in per-topic subfolders yielded *nothing at all*. Files and
  subfolders are now ordered together by one natural key at every level, so a subfolder
  takes its pages' place in the sequence rather than being appended last. Verified on the
  real tree: 1,913 pages of one BODY SECTION come out as contiguous blocks in order.
- It also gained **verification**, which this work cannot do without, since the driver
  deletes sources: the result must reopen and carry exactly the sum of its inputs' page
  counts. It is staged to a `.part` and moved into place only once that passes.
- Deletion is additionally gated on combined bytes ≥ 0.90× the *merged* inputs (measured
  0.996–1.007 across flat, nested and 714-file sections) and on none of 8 sampled pages
  being blank — a page can merge as an empty page without changing the count. Anything
  that fails leaves the folder and its PDF untouched. The **root** is never deleted, only
  sections, so loose files already at a root survive.
- An existing `<SECTION>.pdf` is **verified against its folder** rather than blindly
  skipped: 46 sections had been combined by another tool years earlier and all 46 matched
  exactly, so those folders were redundant; a mismatch is reported `CONFLICT` and nothing
  is touched. The size ratio is deliberately not applied there — another tool made those
  files and one is legitimately 0.877, so only the page count and blank sample are evidence.
- **`--skip-unrecoverable`** combines a section whose parts are damaged beyond repair
  instead of refusing it, moving the damaged originals to `<SECTION> (UNRECOVERABLE)\`
  (subfolder paths preserved) before the folder goes, so skipping never destroys the only
  copy. Opt-in, so an unattended run never ships a section with pages missing.
- New **`helpers/find_split_manuals.py`** finds candidates before anything is touched
  (read-only). Two detection signals, because neither is universal: a name marker, or ≥2
  `*SECTION*` children. Each root gets a verdict — `SPLIT`, `CONTAINER` (children are
  model years, so combining means one PDF per year), `ALREADY-SECTIONED`, `HTML-DUMP`
  (more non-PDF files than PDFs — combining and deleting would destroy a browsable HTML
  manual), `THIN`.

**Three hazards the pre-flight caught, each of which would have destroyed pages:**

- A page stored as a file named **`null`** — a genuine 1-page wiring diagram with no
  extension and stray newlines before `%PDF`. The extension filter skipped it and the
  folder was then to be deleted. PDFs are now recognised by header, anchored at the start
  so a log merely mentioning `%PDF` is not mistaken for a page.
- A section containing an **already-merged copy of itself** (`Wiring_diagram.pdf`, exactly
  the 146 pages of its own nine parts); combining would have emitted every page twice.
  Dropped on exact page-count identity — a 40%-of-total heuristic tried first flagged 31
  genuine chapters and one real duplicate, so only identity is safe.
- A **repair taken at face value**. The page-count gate is computed from the files handed
  to the merge, so once a broken part was replaced by its repaired copy the count agreed
  with itself. Three truncated parts holding 3, 9 and 6 page objects each yielded exactly
  one salvaged page — ~15 pages would have gone silently. A repair must now recover at
  least the page count read from the file's **raw bytes** (`/Type /Page` objects, readable
  when no parser can open the file); `_repair_pdf` rejects a partial salvage when told what
  to expect. Ghostscript's complaints are also captured rather than reaching a shared
  console unattributed, and every broken part is named by its path relative to the section,
  because one section holds two different `General Description.pdf` files.

### Changed (the report says what happened AND why; small files are no longer re-imaged)

The report had one `action` column, so `kept original` covered three unrelated situations and
whether a manual had been re-OCR'd was not in the CSV at all — you had to read the prose `note`
of every row. Measured on a real 78-file review folder: 60 rows said `kept original`, 4 of them
had been re-OCR'd, and nothing but the note distinguished them.

- **`action` is split into four columns**: `action` (`compressed` / `kept original` / `FAILED`),
  `reason` (`compressible` / `born digital` / `already compressed` / `small size` / `error`),
  `ocr` (`new ocr` / `re-ocr` / `kept existing` / `not requested` / `failed`) and `language`
  (the packs OCR actually used). Each takes values from a fixed vocabulary, so a run over
  thousands of files sorts, filters and pivots instead of being grepped. `born-digital (copied
  untouched)` is gone as an *action* — it was a reason wearing an action's clothes; it is now
  `kept original` + `born digital`. The `.log` per-file lines and the summary tally carry the
  same breakdown, and the console prints
  `kept because: 67 small size (12.7 MB), 8 born digital (216.0 MB), …`. The **MB, not just the
  count**, because a count hides what a decision costs: `67 small size` does not say whether the
  size floor left 12 MB uncompressed or 12 GB, and that is the number that tells you whether
  `--min-compress-mb` is set right for an archive.
- **`re-ocr` vs `new ocr` is read from the SOURCE, not from the ocrmypdf mode.** The note used
  to say `re-ocr` whenever `--redo-ocr` ran — but that mode is chosen to protect the images and
  runs on files with no text layer to redo, so it labelled first-time OCR as a redo. The note now
  names the mode (`mode --redo-ocr`) and the column reports the outcome.
- **New `--min-compress-mb N` (default 5): files under it are not compressed at all**, only
  checked for OCR. **OCR is not skipped**: too small to be worth compressing is not too small to
  be worth making searchable, and that is the half that cannot be redone later once the original
  is gone. `--dry-run` applies the same floor (so a preview of a folder of small files is nearly
  free), and `0` compresses everything.

  **This is a deliberate CPU-for-size trade-off, not a free win — the numbers, so it can be
  revisited without re-deriving them.** Measured across 14 past run reports (1,031 scanned
  file-rows) joined to a 375,241-file archive scan (40,041 scanned PDFs, 51.4 GB):

  | band (MB) | median % of orig | archive files | archive GB | est. GB saveable |
  |---|---|---|---|---|
  | 0 – 0.25 | **99%** | 26,094 | 2.4 | 0.02 |
  | 0.25 – 0.5 | 56% | 3,342 | 1.1 | 0.34 |
  | 0.5 – 1 | 57% | 3,465 | 2.3 | 0.52 |
  | 1 – 2 | 48% | 2,917 | 3.9 | 1.25 |
  | 2 – 5 | 45% | 2,214 | 6.7 | 1.51 |
  | 5 + | 32% | 2,009 | 35.1 | 19.87 |

  So a 5 MB floor skips 38,032 files — 95% by count, **31.7% of scanned bytes** — and forfeits
  **~3.6 GB**, because files from 0.25 to 5 MB do compress (median 45–57% of original). Only the
  sub-0.25 MB band is genuinely worthless (median 99% of original: 26,094 files, 0.02 GB), and it
  is most of the wasted CPU. `0.25` would therefore recover almost all the savings while still
  skipping the bulk of the work; **5 was chosen knowingly.** Note the floor also means those files
  get no visual clean-up (`_flatten_bg`, Sauvola, paper-whitening, descreen), since in this
  pipeline the cleaned image *is* the compressed image.

  Where the floor **is** free, measured serially with `ocr=False` on a raw Nissan Primera folder
  whose sources are already CCITT 1-bit: `floor=5` vs `floor=0` produced **byte-identical output
  on all 20 files** (0.000 MB difference) while cutting per-file time **82.1s → 0.3s**,
  **68.0s → 0.3s** and **60.0s → 0.3s**. An earlier draft of this entry justified the floor with
  "0.9% forfeited" from `_compress_sample_review/3000GT`; that folder is an *output* tree whose
  page images are already `/JBIG2Decode`, so the figure did not generalise and has been replaced
  by the archive-wide table above.

### Fixed (the audit blamed us for defects the source already had)

Found by running the floor over a real folder: **18 of 20 files were reported FAILED** with
`page 1 has broken font metrics: /dgp0 256!=224`. Measured before diagnosing: the *source* files
already carry that font. Nothing was damaged — and nothing was OCR'd either.

- **The font-`/Widths` and dangling-XObject checks are now DIFFERENTIAL**, like every other check
  in `_audit_output` (page count, colour, text recall, links are all compared against the source).
  A defect present in the source is reported as a warning and the file ships; one that appears
  only in the output is still fatal, and a test asserts both directions. Font names are compared
  on the metric signature (`256!=224`) rather than the name, since a rebuild may rename `/dgp0`
  and a renamed pre-existing fault is still pre-existing.
- Inherited defects are **tallied once per file** (`6 of 6 sampled pages have broken font metrics
  … — already so in the source`) instead of once per sampled page, which buried the rest of the
  note under six copies of itself.

### Changed (losing links is now refused, not warned about)

Link preservation worked; noticing if it ever stopped did not.

- **Losing link annotations or bookmarks is now FATAL**, where it was a warning
  (`links 249->5` used to ship). The original is kept instead, the same treatment page loss and
  colour loss already got — losing navigation is losing content, and it is precisely the damage
  the graft exists to prevent (a plain rebuild from rendered pages kept 5 of 249 links on one
  manual and lost all 248 bookmarks on another). Checking the OUTCOME catches every route to the
  loss, not just one code path. A file with nothing to lose is unaffected: `la < lb` cannot fire
  when `lb == 0`, so a plain scan still compresses — asserted by a test, because it looks like an
  omission and invites a well-meaning guard.
- **A failed graft now says why.** `_graft_into_source` funnelled every failure into one bare
  `return False` — a page-count mismatch, a missing pikepdf and a save error were
  indistinguishable, and it even raised `RuntimeError('empty graft')` only to swallow its own
  exception. It now raises `GraftFailed` carrying the reason, and the note reads
  `rebuilt — graft failed: page count 5 vs compressed 4 — links/bookmarks not carried over`.
  The call-site comment previously advertised the silence outright.
  Measured before changing anything: across **18 run reports, 1,173 file-rows and 318 files that
  took the compress path, this fallback has fired ZERO times** — so the silence protected nothing
  and only discarded diagnostics.
- **Committed tests, because no corpus file can provide this coverage**: every archive file
  carrying links is born-digital and copied untouched, so the compress path never sees one. New
  `make_linked_toc_pdf` builds a scan with a vector TOC page of internal `/GoTo` links (no
  `/URI` — a URI is a self-contained string that survives anything, while a `/GoTo` points at a
  page object the compressor rewrites). The test asserts that every destination still resolves to
  its expected page **in order**, not merely that the links exist: surviving link objects with
  dangling targets pass any count-based check. It also asserts the targets really became JBIG2,
  so a pass cannot come from nothing having happened. The failure branch is covered too, and was
  confirmed to FAIL when the fatal check is reverted.

Verified on the real sample: 32 pages, 31/31 `GoTo`, 0 `URI`, 18 bookmarks, destinations
resolving to pages 2..32 in order, with the target pages going `/CCITTFaxDecode` ->
`/JBIG2Decode`; and a zero-link file still compresses (318,119 -> 24,042 B). 112 tests pass.

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
