# Working with a large archive

[← README](../README.md) · [all docs](../README.md#documentation)

- [In-place mode](#in-place-mode)
- [The size floors](#the-size-floors)
- [Finding what to compress](#finding-what-to-compress)
- [Sweeping an existing archive](#sweeping-an-existing-archive)

## In-place mode

`--in-place` compresses a library **where it sits** instead of mirroring it. It overwrites source
PDFs, so back up first.

- **PDFs that compress** → overwritten with the smaller, searchable version
- **Born-digital** → never rasterised; replaced by its optimised copy (images JPEG'd) when that verifies, else left alone
- **Already-optimal or unchanged** PDFs, **non-PDFs**, folder structure → untouched
- Reports go only where `--log` says, never among your manuals

Each file is built in the **system temp dir** (not on your manuals drive), verified, then
**atomically swapped** (`os.replace`); the only thing written beside a manual is a short-lived
`.part`. Re-runs are safe: already-compressed files project ≥100% and are skipped.

## The size floors

`--min-compress-mb` (default 5) prices a **lossy re-encode of every page**; `--born-digital-min-mb`
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

## Finding what to compress

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

## Sweeping an existing archive

A lossless pass over a whole archive — building the work list from past run reports, staging,
auditing and promoting — is in [Born-digital optimisation](lossless.md#sweeping-an-existing-archive).
