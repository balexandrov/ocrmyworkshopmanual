# Safety

[← README](../README.md) · [all docs](../README.md#documentation)

What stops the tool damaging a file: which files it refuses to rasterise, how every
output is audited against its source, and how a run survives bad input.

- [Born-digital safety](#born-digital-safety)
- [Verification](#verification)
- [Resilience](#resilience)

## Born-digital safety

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

## Verification

Always on. After writing each output it is re-opened and audited against the **source**, because
size alone can't tell success from damage — losing a page, a colour, a link or the text layer all
make a file *smaller*. Checked: exact page count; a colour page wasn't binarised to 1-bit; no page
paints an XObject the output no longer defines; font `/Widths` match their own
`/FirstChar`..`/LastChar`; text survived by **word recall** on sampled pages (a legitimate re-OCR
differs in character count); links and bookmarks didn't shrink. Any failure keeps the original and
is reported on the console and in the CSV.

Two rules keep the audit honest in both directions:

- **Differential where a fault can pre-exist.** A fault the source already carries isn't damage
  this run did, and failing the file over it throws away a good output *and* leaves the file
  unsearchable. The colour check is judged on **collapse** — fatal only when an output page is
  bilevel and its source page was not — so an MRC scan (8-bit tiles plus a 1-bit mask, passed
  through untouched) is no longer called binarised: six such files went from FAILED to 85 → 42 MB
  with a text layer. Flattening that same page to 1-bit still fails.
- **Fail closed when the source can't be measured.** The source page count is asked of pypdf,
  then pikepdf; if neither can count it, the audit fails rather than comparing the output against
  itself. Measured: a 1,904-page manual pypdf cannot open had been replaced by a 1-page stub while
  the run reported a 56 MB saving.

For an independent second opinion, `helpers/` has two auditors that deliberately share no code
with the tool — `verify_run.py` (colour from rendered pixels, text by word recall, structure via
pypdf) and `verify_lossless.py` ([Lossless rewrite](lossless.md#sweeping-an-existing-archive)).

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
