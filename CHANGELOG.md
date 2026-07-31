# Changelog

All notable changes to this project are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Removed (`helpers/combine_sections.py`, `helpers/find_split_manuals.py`)

Both were one-time tools for a job that is finished: the split USDM manuals have been
consolidated, so nothing is left to scan for or combine section-by-section. Their README
section and the 17 tests covering the delete-after-verify gates go with them. `combine_manual.py`
— which combines loose pages into one PDF and is what the Explorer right-click wrapper calls —
is unaffected, and the page-ordering work described below still applies to it.

The entries further down this file that describe those two scripts are left as written: they
record what was true when the work was done.

### Fixed (one huge manual was the whole run: 4h20m of 4h53m, at 12% CPU)

A 405-file batch took **293 minutes**, of which a single 2910-page manual
(`1st Gen Mazda3_Mazdaspeed3_Workshop_Manual.pdf`, 143 MB) was **~4h20m** — the other 404 files
finished in about half an hour. Measured during that tail: **12% CPU (~1.4 of 12 logical
cores)**, C: 99% idle, disk queue 0. Serial work, not I/O.

Both per-file stages were single-threaded. `_render_all` issued **one** Ghostscript call
(`-dFirstPage=1 -dLastPage=2910`, ~2h20m); `ocrmypdf` ran at `--jobs 1` for ~2h at 0.39 pages/s.

The dynamic budget meant to prevent exactly this already existed and never fired.
`_ocr_jobs_now()` divides cores by files in flight, but it is read **once**, immediately before
launching a subprocess that then runs for hours: that OCR started while ~50 files were still
queued, so `6 // 6` = 1, and `--jobs` cannot be changed on a running process. By the time one
file was left it would have returned 6, but nothing re-consults it. The report proves it — the
`jobs N` note is written only when N > 1 and no row carried it.

The budget is now keyed on the **file's own cost**, not on queue state at an arbitrary instant:
`_ocr_jobs_for(pages, size_bytes)` gives ≥800 pages the whole box, ≥200 half, ≥50 two, else one,
capped at physical cores. Page count is a property of the input, so the same file gets the same
budget on every run. `_ocr_jobs_budget` takes the **max** of that and `_ocr_jobs_now()`, keeping
the old queue-drain rule alive as a floor. Page count comes from `_page_count`, which returns 0
on sources that cannot be opened at all (measured: pypdf raises `AttributeError('NullObject')`
on the 1990 RX7 section manuals) — so it falls back to bytes, then to the static hint. A
thread-count guess must never be the thing that fails a file.

`_render_all` now renders **bands concurrently**. `_render_bands` is split out as a pure
function of `(dpis, shards)` so the split is testable without invoking Ghostscript, and
`shards=1` reproduces the previous bands exactly; a band shorter than two shards' worth of
pages is not split at all, so the hundreds of small files in a real run are untouched. Safety
comes free from machinery that already existed for another reason: each band renders into its
own `r{n:03d}` directory and is renumbered to `p{first + k:04d}.png`, because Ghostscript
restarts its `%d` counter per invocation — so output page numbers come from the band, never from
the counter. Each band keeps its own `_run_stalled` watchdog, and a stall in any band is
re-raised as `TimeoutExpired`, so stalls stay distinguishable from crashes.

Six workers deriving budgets independently could each claim the whole machine, so a shared
`multiprocessing.Value` tally (`_claim_threads` / `_threads`) clamps the total to
`OVERSUBSCRIBE` (1.5) × physical cores. It **clamps and never blocks**: a step that finds the
budget spent runs narrow, because a process that has not been allowed to start produces no
progress for the stall watchdog to read. The grant is released in a `finally`, so a crashed or
killed step cannot leak its share and starve the rest of the run.

Two smaller things fell out of the same measurement. Files are now submitted **biggest first**
(size, path as tie-break), so the long pole starts at t=0 and the small files fill in around it
instead of it surfacing alone at hour four. And the ETA is **byte-weighted**: per-file cost spans
0.06 MB to 405 MB here, so averaging over files completed made it collapse — `[ETA 1m]` was
displayed for over two hours while that one manual finished.

Verified on a 199-file run: **9 concurrent Ghostscript processes** (exactly the 1.5 × 6 cap)
with contiguous non-overlapping bands — `2015 Suzuki Vitara - RUS.pdf` rendering pages 1-178,
179-356 and 357-532 at once — at **96% CPU** against the 12% above. Band coverage is also
property-checked for 1-399 pages × 1-8 shards, uniform and mixed dpi: no gap, no overlap, no
page rendered at another page's dpi, and a sharded render is asserted **byte-identical** to the
sequential one page for page.

### Fixed (every run had been at NORMAL priority, silently)

`set_below_normal_priority()` never worked on Windows. `ctypes.windll.kernel32.GetCurrentProcess`
had no declared `restype`, so its pseudo-handle came back as a 32-bit `-1` and reached the
64-bit `HANDLE` parameter as `0x00000000FFFFFFFF`. Measured: `SetPriorityClass` returned **0**
with `GetLastError()` **6 (ERROR_INVALID_HANDLE)** — and `except Exception: pass` hid it, so
long runs competed with foreground work at Normal priority. Sampled mid-run: 13 of 13 worker,
Ghostscript and tesseract processes at base priority 8.

The Win32 types are now declared, and the function **returns a bool** instead of swallowing
failure. Pinned by a test that *observes* the result rather than trusting the call: it runs in a
subprocess (so it does not renice the test runner) and checks a spawned child too, since
Ghostscript and tesseract inherit the class rather than calling this themselves.

### Fixed (a publisher's `..._COVER.pdf` sorted last instead of first)

In `…\Carisma\Manuals\1996 Body Repair Manual` the cover landed at the **end** of the combined
PDF — the worst possible place:

```
PBGE95E1_FOR_EUROPE_CARISMA_96_BRM_0.pdf … _8.pdf … _COVER.pdf   <- last
```

`_lead_rank` rejected it on the tag budget: `words` carries **24 characters besides `cover`**,
and the budget is 10. That budget is not an accident — it is what keeps **135 parts photos**
(`Medium_2002-CHEVY-TRUCK-COLUMN-COVER.jpg` is a photo of a steering-column cover) and
`DISCOVER.PDF` (a Land Rover Discovery manual) out of the cover rank. The publisher's file is
structurally identical to those: a long descriptive name ending in `COVER`. **Name length
cannot tell them apart.**

The folder can. Every sibling here shares `PBGE95E1_FOR_EUROPE_CARISMA_96_BRM_`, and the part
that actually distinguishes the files is `0`…`8` against `COVER`; the parts photos share
nothing with each other, and many of them end in `COVER` because it is their *subject*. So the
keyword now gets a **second chance against the folder-relative remainder** — the same principle
as the stamped-boilerplate fix above: what every sibling shares carries no information.

`_folder_affix` computes the longest common prefix and suffix over a folder's stems, **cut back
to a separator** so `ABC_1` and `ABC_12` do not "share" `ABC_1` and leave one file with an empty
remainder, and returns nothing for a folder of fewer than two files. `_lead_rank`, `pagekey` and
`natkey` take it as an optional argument that defaults to today's behaviour exactly, so
`combine_sections.sections_of` and `order_by_docid` are untouched. `collect` computes it per
directory as it walks; `dedupe_pages` computes the same one per parent group so an identity key
and a sort key can never disagree about the same file.

**The full name is always judged first**, so this can only ever add a hit, never remove one —
and the tag budget still applies to the remainder, which is what keeps the parts photos out even
in a folder where they do share a `Medium_` prefix. Measured over the same archive sweep used to
design it, 578 folders / 18,640 files: **51 covers newly ranked, 0 lost.** All 51 are genuine
Mitsubishi publisher covers (`..._BRM_COVER.pdf`, `..._CHASSIS_COVER.pdf`, `..._EW_COVER.pdf`),
including one — `PHDE9608-C_GALANT_2001_COVER_ELECTRICAL_WIRING.pdf` — that needs *both* ends
stripped before the keyword stands alone.

The affix changes only the front-matter **rank**, never the sort tokens: pages sharing a folder
prefix already sort correctly on the part they do not share, so re-tokenising the remainder
would be risk without gain.

### Fixed (the last file of a batch OCR'd on one core while the rest of the machine idled)

A 5,243-file run sat at `[5242/5243]` for over twenty minutes with no output. Measured live,
mid-run, rather than guessed at:

- one file outstanding — a 57.9 MB Russian book — which had already rendered every page to
  **1,522 MB** of scratch PNGs and was inside ocrmypdf;
- the command actually running: `ocrmypdf --language rus+eng --skip-text --quiet --jobs 1`;
- the machine: **6 physical cores, ~1 busy**.

The thread budget was computed **once**, before the pool, from the **total** job count —
`_ocr_jobs = cores // min(workers, N)`, i.e. `6 // min(6, 5243)` = 1 — passed to
`_init_worker` through `initargs`, and never revisited. `--jobs 1` is right for the first
5,242 files, because the batch is already parallel across files. It is wrong for the one file
that decides when the run ends, and that is precisely the file it was still applied to.

The intent was already documented — `OCR_JOBS`' own comment names "the tail of a batch" as a
case it means to cover, and measures the cost (*17.3s at `--jobs 1` vs 7.3s at `--jobs 4` on
an 8-page file*). The adaptation simply never happened, because `N` is the size of the run and
not the number of files in flight.

**The fix:** the parent publishes how many files are still outstanding in a shared
`multiprocessing.Value`, updated in the completion loop, and the new `_ocr_jobs_now()` divides
the physical cores by `min(workers, remaining)` at the moment ocrmypdf is about to be spawned
— not when the pool was built. While the pool is saturated it returns exactly what it returned
before; the budget only opens up as the batch drains. Verified end to end on a 3-file run with
`--workers 2`: the two concurrent files got `jobs 3`, the tail file got `jobs 6`.

That a `multiprocessing.Value` survives Windows *spawn* was proven before the design relied on
it — it works because `initargs` is passed through process creation — and a test pins the
property the whole fix rests on: workers see the parent's **later** writes, not a snapshot from
when the pool was built. If the counter is ever missing or unreadable the static value is used
instead; a thread-count hint must never be the thing that fails a file.

The chosen value now appears in the note when it is above the saturated default
(`(lang:rus+eng, mode --skip-text, jobs 6)`), so the tail's behaviour is checkable from the
report alone.

Two honest limits. A file that reaches OCR *early* keeps the small budget it was given —
ocrmypdf's `--jobs` cannot be changed once it is running — but the case that matters starts its
OCR when few files remain. And two concurrent runs of the tool will each see "one file left"
and each claim the whole machine; workers already run at below-normal priority, so that
contends rather than thrashes, and cross-process coordination is not worth building for it.

Not addressed here, and still true: nothing is printed while a file is in flight, so a long
file is indistinguishable from a hang; files are submitted in alphabetical order rather than
biggest-first; and the ETA divides elapsed time by completed count, so it reads "a few minutes"
right before a long tail.

### Fixed (a stamped watermark was read as a text layer, so the one thing the file needed was skipped)

A 256-page Mitsubishi Carisma supplement (`Supplement_A.pdf`, 21,273,852 bytes) came out of a
compress+OCR run **unsearchable**. Every page carries exactly **66 chars** and nothing else:

```
www.WorkshopManuals.co.uk
Purchased from www.WorkshopManuals.co.uk
```

`has_text` counts raw `extract_text()` against a 40-char floor, so 66 chars of paywall stamp on
all 8 sampled pages meant "already searchable" → `_ship_original` returned `OCR_KEPT` →
**ocrmypdf was never run**. `looks_born_digital` got the *same file* right — it uses
`_visible_text_chars` against a 100-char floor, and 66 < 100, so it reported
`scan_frac=1.0 scan_pages=8/8 text_pages=0`. **The two gates disagreed and the dumber one won**,
because `_ship_original` only ever asks `has_text`.

The cost is the whole point. That file's images are already `/CCITTFaxDecode` G4 at **595.7 dpi**,
so the sample projects **103% of original** and it cannot be compressed at all. A text layer was
the only thing this tool had to offer it, and that is precisely what it silently withheld — and
there are more such files in the archive.

**The fix: a text layer says something different on every page; a stamp says the same thing.**
Text repeating on ~every sampled page is discounted wherever the tool asks whether a file is
already searchable. No watermark-specific knowledge, no new flag, and nothing about the page is
altered — the stamp stays exactly where it is, the file just gets the text layer it needed.

Deliberately narrow, because the alternative failure is writing off a real text layer:
`_BOILER_MIN_PAGES = 3` (two pages agreeing is a coincidence), `_BOILER_COVERED = 0.9` (not 1.0,
so one blank or unreadable page cannot hide a stamp), and `_BOILER_MAX_LINES = 4` /
`_BOILER_MAX_CHARS = 400` — a stamp is short, while twenty identical lines is a form template and
a template is content. Applied **line-wise**, so a page carrying a repeated running header *plus*
body text keeps its body and still counts as searchable.

The same blind spot two floors up is closed by the same discount, and both are worse than the
reported bug:

- `_visible_text_chars` is a visibility *gate* followed by a *raw* count, so one visible stamp
  word made a whole page's text count as publisher type. A stamp over 100 chars therefore pushed
  every scanned page over `looks_born_digital`'s floor, dropped `scan_frac`, and **skipped the
  entire manual** — never compressed *and* never OCR'd. Verified on a 129-char fixture:
  `_visible_text_chars(page, 100)` reads 129 without the discount and 0 with it.
- `classify_page` uses the same floor and returns `PT_VECTOR`, which is passed through
  uncompressed **and** dropped from the OCR render (`skip_pages`) — so a long stamp made every
  page silently un-OCR'd while the report row still claimed `new ocr`. `sample_projection` gets
  the discount too, or it would charge every page its original size and skip a compressible file.

A show op whose bytes cannot be decoded or matched still counts as real text, so a stamp in a
CID/Identity-H font is simply not discounted and the old behaviour cannot regress. A watermark
living in an annotation appearance stream never reached `extract_text()` in the first place, so
it never caused this bug; stamps inside Form XObjects *are* handled, because both
`extract_text()` and `_stream_visible_text` already recurse into forms.

`has_text`, `has_any_text` and `looks_born_digital` each used to open the file and extract the
same 8 pages independently — four opens and up to 32 `extract_text()` calls per file for three
answers about the same pages. They now share one `TextSample`, cached on (path, size, mtime), so
adding the discount to all of them costs **less** than the old code, not more; a file rewritten
in place cannot be served a stale answer.

Reported, not silently handled: `scan signals` gains a `boiler=2ln/66c` token and the note reads
`(65 chars of boilerplate repeated on every page discounted — a stamp, not a text layer)`, so a
row whose `ocr` cell changed from `kept existing` to `new ocr` explains itself. No new column —
`signals` is attached only when a stamp was found, so every other row in a normal archive is
byte-for-byte what it is today. Note also that a slightly-over-100% row on this path is now the
correct outcome: adding a text layer makes a ship-the-original file a little *larger*.

One recorded caveat: `scan_candidates.py` keeps its own private copies of `has_text` and
`looks_born_digital` and will now disagree with the tool about files like this. Out of scope for
this change.

### Changed (the report's `file` column holds the FULL path)

A regression from collapsing three report files into one. The prose `.log` recorded
`Source : <root>` in its header, so a path relative to that root had an anchor. The `.csv`
never carried the root — and now that the `.csv` *is* the report, `DAEWOO\Tico.pdf` in a file
sitting in some other folder, read a week later beside three other reports, does not say which
tree it came from. `file` and `duplicate of` are now full paths; a twin you cannot go and look
at is not a useful flag either.

`--retry-failed` reads that column, and the fix there is not the obvious one. `dest_root / p`
with an **absolute** `p` discards `dest_root` and yields `p` itself, so letting pathlib resolve
the recorded path would have made a non-in-place retry write its output **on top of the source
it was reading**. `_retry_jobs` derives the dest from the path relative to `src_root` in both
forms instead, and returns what it could not place:

- an entry no longer on disk is skipped **and counted** — a retry that silently does less than
  the report asked for looks exactly like a retry that worked;
- an entry that is not under `src` means one of the two arguments is wrong, so it is reported
  loudly and skipped. Guessing which tree was meant is how you compress the wrong archive.

Reports written before this still work: a relative `file` value is resolved against `src_root`
as before. `_read_failed_rels` is now `_read_failed_files` (it no longer returns rel-paths).

### Fixed (`combine_manual.py`: page order, the same page scanned twice, and bookmarks)

Pointed at a folder of scanned page images — which it has always accepted; `IMAGE_EXTS` and
the img2pdf wrap have been there from the start — it produced a PDF whose **pages were in the
wrong order**. Measured on a 21-section Daihatsu manual, 1216 `.jpg` + 30 `.tif`: **14 of the
21 sections came out mis-ordered**, and one section's pages appeared **twice**.

The filename sort kept punctuation inside the alpha prefix, so it read one chapter as several:

- `engine mechanical` held `EM11.jpg` beside `EM-2.jpg`. Prefixes `em` and `em-` are not the
  same string, so **page 11 sorted second**, right after the cover and 78 pages early.
- `body` held `B0-4`, `B040`, `BO169`, `BO-2` — whoever named the scans typed capital O for
  zero and dropped hyphens at random. That made **four bogus chapters out of one** 183-page
  section, page 4 ahead of page 2. A `0` that follows a letter is an `O`.
- `general info` sorted all nine `GIn.tif` ahead of every `GI-n.jpg`.
- Every section leads with an unnumbered cover and most hold an index page. Alphabetically
  `GI-COVER.jpg` landed near the **end** of its section. **Front matter now leads its folder
  in the order it is printed** — cover, foreword (`foreword`, `fwd`), index/contents, then the
  numbered pages.

  `fwd` ranks **only as the bare name** (`fwd.pdf`, `FWD.pdf`), never with a tag or a page
  number. Three letters is not much to go on, and it is measurably ambiguous in this archive:
  all **274** bare `fwd.pdf` hits are a drivetrain section rather than front matter —
  `…\2009 Maxima A34\fwd.pdf` sits among the Nissan FSM section codes (`ADP, BR, CHG, CO, EC,
  EM, FAX, FSU, GI, LU, MA…`) and `…\LX570\EWD\system\pdf\fwd.pdf` among 60 EWD systems
  including `mm4wd`. The bare-name rule does not resolve that — those files are the bare name —
  but it does keep numbered `FWD-12.jpg` series and `fwd2.pdf` out of the rank, and it excludes
  10 further names such as `PWEE9508-ABCDEFG_FWD_AT_E-W_21B.pdf`. All 40 genuine forewords in
  the archive are spelled `foreword`, which needs none of this. If you combine one of those
  Nissan or Lexus folders, `fwd.pdf` will sort first — the printed page order and `--dry-run`
  are the check.

  The match is on whole **words**, split on separators, and the keyword has to dominate the
  name: at most a short section tag besides it. Both halves of that are load-bearing, and
  each was settled by scanning **1.11M image/PDF names** in the archive rather than by
  guessing. Word matching is what keeps `DISCOVER.PDF` — a Land Rover Discovery manual, ending
  in `cover` — out of the cover rank. The tag budget is what keeps out the **135 parts photos**
  (`Medium_2002-CHEVY-TRUCK-COLUMN-COVER.jpg` is a steering-column cover) and
  `tyrerotation_fwd.jpg` (front-wheel-drive tyre rotation, not a foreword).

  The budget is **10**, which is a deliberate loosening of the 2 this entry was first written
  against. At 10 a short topic tag fits, so `DTC Index.pdf` / `dtc_index.gif` now DO rank as
  front matter — topic names in print-captured manuals, whose filename order is arbitrary by
  definition and is exactly what `--order docid` exists to override, so hoisting them costs
  nothing there. What 10 still excludes is the wordy end, which is where the false hits live:
  the parts photos carry 24+ characters besides the keyword.

  This replaces a cruder first attempt from earlier in this entry, which matched a 5-character
  prefix/suffix and excused itself by ranking **images only**. That was wrong twice over: it
  hoisted all 135 parts photos, and the images-only escape hatch would have excluded the
  foreword pages entirely, since in practice they are `Foreword.pdf`, not scans.
- One file is mojibake, `╡FS-2.jpg` (U+2561), which sorted after every ASCII name.

Two properties of the old key were load-bearing and are preserved deliberately, each pinned by
a test: a separator **between two digits** carries order (strip it and `1-1` becomes the number
11, breaking the documented `1-1, 1-2, 1-11, 2-1, 2a-1, 2b-1`), and the empty strings
`re.split` emits keep digit-leading names sorting ahead of alpha-leading ones. `_base` strips
only a **known** page extension rather than using `Path.stem`, because these keys sort folder
names too and `Path('4.2 ENGINE').stem` is `'4'`.

**The same page scanned twice.** `general info` held 26 of its pages as *both* a 2550×3508 JPG
and a 637×877 TIF, so both went into the manual. Now the higher-resolution copy wins. The rule
is pixel area and not format, because **4 of that folder's 30 TIFs are the only copy of their
page** — "drop the TIFs" loses four pages silently, which is the failure the page-count check
exists to catch. A copy is dropped only when it is ≥2× smaller (the 26 real pairs measured
**4.0×–16.9×**); a collision at a *similar* resolution may be two different pages, so
everything is kept and flagged. Grouping is **per folder**: grouping globally put 17 of the 21
sections' `cover.jpg` in one group and would have thrown away 16 covers. Every drop is printed
with both pixel sizes and the ratio, on dry runs too, and **nothing is deleted from disk**.

**Bookmarks.** Under `--recursive`, one per section folder at that section's first page — a
1220-page manual with no outline cannot be navigated. The page index is measured during the
merge, so a section whose first part is a 9-page PDF does not land its bookmark 8 pages early;
an index past the end can only mean an input contributed no pages (a 0-page PDF, which
`expected_pages` counts as 0 and which would otherwise merge invisibly), so it is refused. The
outline is read back out of the finished file and reported, and a missing one is called lost
*navigation*, never lost pages.

Result on that manual: **1246 files → 1220 pages**, 26 duplicates dropped, 21 bookmarks,
every source file still in place.

API: `natkey` is now `pagekey` plus a name tiebreak (so a run is reproducible even though
`pagekey` is deliberately lossy) and returns a tuple; `dedupe_pages`, `report_dups`,
`section_bookmarks`, `bookmark_preview` and `outline_pages` are new; `combine` takes
`bookmarks=None`. `helpers/combine_sections.py` picks up the ordering fix and the bookmarks,
but **not** `dedupe_pages` — its `--delete` removes the source folder, and a ratio gate
calibrated on one measured folder is evidence enough to leave a page out of a PDF, not evidence
enough to destroy the file. There is a comment at that call saying so, and what wiring it in
would additionally require.

One thing to expect downstream: a combined PDF now carries an outline, which **activates** the
main tool's bookmark-preservation audit that was previously passing trivially at 0→0. If the
graft cannot carry them over, the compress step says `rebuilt — links/bookmarks not carried
over` — reported, not silent.

### Changed (BREAKING: the report is one `.csv` — the prose `.log` and `_by_folder.csv` are gone)

`--log` wrote three files: a prose `_ocrmyworkshopmanual_report_<ts>.log`, the per-file
`.csv`, and a `_by_folder.csv` rollup. Only the `.csv` was worth keeping, and only the `.csv`
can feed `--retry-failed`; the other two were files to delete after every run.

- the **`.log`** duplicated in prose what the console already prints — the same header, the
  same per-file lines, the same closing tally. Nothing consumed it.
- the **`_by_folder.csv`** was a group-by over rows the main `.csv` already has. A pivot, kept
  as a file.

So `--log` now writes exactly one file, and a path with a suffix is used as `.csv`
(`--log run.log` → `run.csv`) rather than producing a `.log` the tool no longer writes.

Two things lived **only** in the prose report; neither was dropped with it:

- the born-digital scan's evidence is now a **`scan signals` column**
  (`scan_frac=0.033 scan_pages=1/30 text_pages=29 chars=8412`). This is the answer to *why
  did you refuse to compress this?*, and under the never-damage rule that is the one decision
  most worth being able to audit — it does not belong in a file nobody keeps.
- warnings raised while **scanning** the source (attached to no single file) now get one
  labelled `(pre-scan)` row. Its decision columns are blank, `error` included, so
  `--retry-failed` cannot mistake it for a file to redo.

The run's thresholds (`--min-savings`, `--min-compress-mb`, `--jpeg-quality`,
`--photo-descreen`, `--min-size`, `--timeout`) used to be recorded only in the `.log` header
and are now printed at startup with the rest of the settings, so a per-file table is not asked
to carry run-level facts.

`write_run_log` is now `write_run_csv(csv_path, results, prescan_warns)` — no `dest_root`,
`src_root`, `settings`, timings or counters, because a table of files needs none of them. It
still rewrites the live-flushed CSV at the end, which is what makes it sorted by path and
duplicate-aware (dedup only knows the full picture once the run is over).

### Changed (BREAKING: no report file by default, and `--log` decides where one goes)

Reports were placed relative to the WORK, which scattered
`_ocrmyworkshopmanual_report_<ts>.log` / `.csv` / `_by_folder.csv` triplets through the
archive wherever the tool had been pointed. Two of the three branches wrote into the source
tree, and one of them made `--dry-run` leave three files behind **despite documenting that it
writes nothing**. Right-clicking a single PDF dropped three reports next to it, which is what
surfaced this.

A run is now **console-only** and writes nothing but its output. `--log` is the single control:

| invocation | result |
|---|---|
| *(omitted)* | no report file |
| `--log` | timestamped report in the **current folder** |
| `--log DIR` | timestamped report in `DIR` |
| `--log FILE` | exactly that file (as `.csv` — see the entry above) |

A value is a folder when it is an existing directory or has no suffix, otherwise it is the
exact file path — so `_report_path` is that one decision, and the `report_dir` variable with
both of its archive-writing branches is gone. Where a report lands is the caller's business,
never derived from the work.

**`--no-log` is removed, not deprecated.** Once "no log" is the default the flag says nothing,
and a no-op kept for compatibility is a thing to explain forever. The four places that passed
it (`combine_manual.py`, `helpers/batch_compress.py`, the single-file CLI test, and the PDF
context-menu `.reg`) no longer do. An external script still passing it now fails with
argparse's "unrecognized arguments" — loud and a one-line fix, rather than silently doing
something else.

Also: when files FAILED and no report was written, one line now says so and points at
`--log`, since that is exactly when the report matters and `--retry-failed` can only read a
report `.csv`.

### Added (Explorer right-click "Compress + OCR" on a PDF)

`tools\compress-pdf-context-menu.reg` adds **Compress + OCR (searchable)** to a `.pdf` file's
right-click menu, with an uninstall `.reg` beside it. `HKEY_CURRENT_USER` only, so no admin
rights. Default options, so the original is never touched — the result lands beside it as
`<name> (COMPRESSED).pdf`.

Installed under `SystemFileAssociations\.pdf` rather than under a ProgID, so it appears
whatever program owns the `.pdf` association and does not need reinstalling when the default
viewer changes.

No report files are left beside the PDF: a run is console-only unless you pass `--log` (see
the logging change below, which this menu is what prompted), and the console window `pause`s so
the summary survives long enough to read. The wrapper also refuses a non-`.pdf`, a missing file
or a missing argument with a readable message rather than handing them to the tool.

### Added (Explorer right-click "Combine PDF")

`tools\combine-pdf-context-menu.reg` puts a **Combine PDF** submenu on every folder's
right-click menu, with an uninstall `.reg` beside it. Installs under `HKEY_CURRENT_USER`, so
no admin rights and no machine-wide change. Four entries: preview the page order (writes
nothing), combine, combine including subfolders, and combine then compress + OCR.

The registry holds a single path — `tools\combine_pdf_here.cmd` — and that wrapper carries
everything awkward: it finds the repo and `combine_manual.py` relative to its own location
(`%~dp0..`), prefers `.venv\Scripts\python.exe` since a bare system Python lacks pypdf /
img2pdf / Pillow, quotes a folder name containing spaces, rejects a missing or non-folder
argument with a readable message, and `pause`s so the page order and any traceback survive
long enough to read. Keeping that logic in a `.cmd` rather than an escaped registry string is
the point: it can be read and fixed.

Both files are ASCII with CRLF, pinned by a new `.gitattributes` (`*.cmd`/`*.reg` →
`eol=crlf`, everything else untouched): cmd.exe can mis-parse `goto`/labels in an LF-only
`.cmd`, so a clone with `core.autocrlf=input` would otherwise ship a broken script.

### Added (`--order docid`: publisher page order for a manual printed from the web)

Consolidating the Mitsubishi Outlander 2022 repair manual (9 sections, 109 parts) hit a case
the Subaru manuals never did. Those parts were named `1. Foreword.pdf`, `2. How to Use…` —
the filename sort *was* the page order. These are named by TOPIC (`CONSULT Function.pdf`,
`DTC Index.pdf`, `Reference Value.pdf`), so sorting by name is arbitrary: it put the
diagnostics tooling ahead of the DTC index and split `System Description` from
`Shift system description`.

Every part is a browser print-to-PDF capture, and its page-1 print header still carries the
source URL with the publisher's document id (`…/2022/06/HTML/N5060302G0000900USA.htm`).
Sorting on that restores the manual's own sequence — measured 100% coverage (109/109), and
the shipped `AT.pdf` now runs Component Description → Component Parts Location → DTC Index →
the three System Descriptions together → TCM → the Electric Shift components → CONSULT
Function → Reference Value.

- `combine_manual.py`: `docid_key()` / `order_by_docid()`, and `--order {natural,docid}`
  (default `natural`, so existing behaviour is untouched). Discovery stays in `collect()`;
  ordering is a separate step.
- **All-or-nothing per section.** Unless *every* part has a doc id, the section keeps natural
  order. A half-publisher, half-alphabetical sequence cannot be reviewed and nothing in the
  result would reveal which half you are looking at. This also makes the flag safe to leave
  on for a numbered-parts manual: coverage is zero, so nothing is reordered.
- `helpers/combine_sections.py` forwards `--order` and records `order` / `docid_missing` per
  row, because "did this section get publisher order or fall back?" is exactly the question
  to ask of a combined manual.

### Fixed (the size gate condemned three perfect merges)

Three of those nine sections failed on `combined bytes >= 0.90x the inputs` (0.829, 0.854,
0.844) with their page counts verifying exactly. Measured before changing anything: word
recall **1.0000** (3663 of 3663 words), 0 blank pages, all 60 images and all 13 embedded font
programs present. Nothing was missing — a print-captured manual arrives as many small browser
PDFs that each carry their own catalogue, metadata and xref, and pypdf writes the merge more
compactly. The 0.90 floor was calibrated on scanned sections (0.996–1.007) and does not
describe this class of file.

The size ratio was only ever a cheap **proxy** for "did we lose content", so when it trips it
now defers to the thing it stands in for: word recall against the inputs (reusing the main
tool's `_words`). Below `MIN_WORD_RECALL` (0.98) the section still fails and its folder
survives; above it, the file ships with both the ratio and the recall recorded in the new
`word_recall` column.

That change exposed a second gap. A test asserting "a broken part keeps its folder" had been
passing only because of this false positive: its fixture is
`b'%PDF-1.4 but truncated garbage'`, which **Ghostscript repairs into a one-page PDF**. With
no `/Type /Page` objects in the bytes and no object streams to hide them in, a count of 0 is
trustworthy — nothing survives to recover, and accepting that salvage appends an *invented*
page to the manual. `raw_pages()` now reports whether its count can be trusted (`/ObjStm`
present means "cannot tell", and keeps the lenient path), and an unreadable file with a
trustworthy 0 is reported unrecoverable instead of repaired.

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
