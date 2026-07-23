# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
