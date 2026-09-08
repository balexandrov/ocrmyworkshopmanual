# ocrmyworkshopmanual

Turns a tree of scanned PDFs into small, searchable ones: render → page-type router →
generic JBIG2 → an invisible OCR text layer. `README.md` is the user-facing write-up;
the module docstring in `ocrmyworkshopmanual.py` carries the design rationale.

## The governing rule

**Never damage a file.** Compression is opportunistic — a missed compression is fine,
a rasterised vector page or a lost link is not. Every guard exists because it once
shipped damage:

- `_audit_output` compares the result against the **source** before anything is
  overwritten, and keeps the original when a check is fatal. Damage and success look
  identical on file size — losing a page, a colour, a link or the text layer all make
  the file *smaller* — so nothing here is judged on the number the run prints.
- The audit is **differential** where a defect can pre-exist. A fault the source already
  carries is not damage this run did, and failing the file over it throws away a good
  output *and* leaves the file unsearchable. Ask "could we be the cause?" before
  asserting a check.
- A FAILED file is the tool working. Report it with the cause; never widen a guard just
  to make a file pass.

Bookmarks and link annotations are not decoration: these manuals navigate file-to-file
through `/GoToR` and `/URI` bookmarks, and that inter-file table of contents *is* part of
the product. Losing it is content loss even when every page survives.

When you fix something, record **what was measured** in the comment — the failure mode and
the numbers. Every comment in this codebase that cites a specific manual earned its place
that way; keep doing it.

## Working on a real archive

- **Sample first, then scale.** Confirm before launching a batch over thousands of files.
- Screening candidate files on PDF structure alone over-predicts badly. Gate them with the
  tool's own predicates (`has_text` first — it short-circuits the great majority of files —
  then `looks_born_digital`), because only files it will really OCR can hit an OCR bug.
- To prove a fix on real data, run the same file list against the pre-fix code
  (`git stash push -- ocrmyworkshopmanual.py`) and diff the two report CSVs. A predicted
  trait is a candidate set; the before/after diff is the measurement.
- Before an `--in-place` pass, capture per-file page / bookmark / link / bit-depth numbers,
  then verify against them afterwards. In place destroys the only thing an audit could
  otherwise compare against.
- `--from-list` is **in place** unless you also pass `--dest`.
- `--timeout` is seconds *without progress*, never a wall-clock budget.
- An in-place pass changes files that no repo version-controls. Whatever consumes the
  output has no way to see that from git, so record what was rewritten somewhere the
  downstream will look.

## Tests

`python -m pytest -q` — ~5 minutes, real Ghostscript/Tesseract/ocrmypdf. Fixtures live in
`tests/_util.py`; when a real file exposes a bug, add a fixture that reproduces its
*shape* rather than asserting against a private archive. The suite must be green before an
archive pass.

## This repo is PUBLIC

`github.com/balexandrov/ocrmyworkshopmanual`. Nothing personal goes in a commit: no local
drive paths or machine names, no private hostnames or URLs, no private project names, no
credentials, and no real archive paths in code, comments, tests or fixtures. Keep local
context in `CLAUDE.local.md` (gitignored), not here. Check `git diff` before committing.
