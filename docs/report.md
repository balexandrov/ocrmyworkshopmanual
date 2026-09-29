# Run report

[← README](../README.md) · [all docs](../README.md#documentation)

A run reports to the console and writes nothing but its output. **`--log`** keeps a report too,
never placed relative to the work being done. It is **one `.csv`**, flushed per file (openable
mid-run, and a killed run still has one), rewritten complete and sorted at the end.

One row per file: `file, action, reason, ocr, language, orig size (MB), new size (MB), %,
duplicate of, page types, scan signals, note, warnings, error`. `file` and `duplicate of` are
**full paths**, so a report read days later says which tree it came from; `--retry-failed` reads
them back but derives outputs from the path *relative* to the `src` you pass, so a retry can never
write over its source.

The **four decision columns** use fixed vocabularies, so a run over thousands of files sorts and
pivots without reading the prose `note`:

| column | values | answers |
|---|---|---|
| `action` | `compressed` · `kept original` · `FAILED` | What was done |
| `reason` | `compressible` · `lossless rewrite` · `born digital` · `already compressed` · `small size` · `error` | Why |
| `ocr` | `new ocr` · `re-ocr` · `kept existing` · `not requested` · `failed` | What became of the text layer |
| `language` | e.g. `eng`, `rus+eng` | Which packs OCR used (blank when none ran) |

`lossless rewrite` vs `compressible` is the distinction that matters for trust: both are
`compressed`, but the first means **no page rendered and no image re-encoded**. `re-ocr` vs
`new ocr` distinguishes replacing an existing text layer from giving a file its first.
`page types` tallies the classification (`line=12 vector=3`); `scan signals` carries the
born-digital scan's evidence (`scan_frac=0.033 scan_pages=1/30 text_pages=29 chars=8412`).

**Malformed-PDF warnings are attributed, not dumped.** Library messages carry no filename and
interleave across worker processes, so they're captured per file, tallied, and written to that
file's `warnings` column (`4x pypdf: incorrect startxref pointer`). Pre-scan warnings get one
`(pre-scan)` row whose `error` cell is blank, so `--retry-failed` never mistakes it for work. The
console shows a compact `[3 pdf warnings]`; `--verbose` echoes each.
