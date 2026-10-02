# Companion tools

[← README](../README.md) · [all docs](../README.md#documentation)

## Combining loose pages

Some manuals arrive as a folder of loose page images (`1-1.jpg`, `2a-1.jpg`, …) and/or per-section
PDFs. `combine_manual.py` merges such a folder into a **single PDF named after the folder**, then
by default compresses and OCRs it.

```bash
python combine_manual.py "…\Honda\--Engines--\Haynes_ZC_Manual"
python combine_manual.py FOLDER --dry-run              # print the page order, write nothing
python combine_manual.py FOLDER --no-compress          # raw combined PDF only
python combine_manual.py FOLDER --recursive            # include subfolders, add bookmarks
python combine_manual.py FOLDER --recursive --outline tree   # bookmark every folder and file
python combine_manual.py FOLDER --skip-unrecoverable   # combine readable parts, name the rest
python combine_manual.py FOLDER --no-repair            # refuse instead of repairing
```

- Uses images and PDFs **directly** in the folder; HTML-asset subdirs and stray `.htm`/`.txt` are
  ignored. A PDF is recognised by its **header, not its extension**.
- **Natural-sort page order** that forgives how scans get named: separators carry no order
  (`EM11` is page 11), a `0` after a letter is an `O` (`B0-4`/`BO-2` are one chapter), mojibake
  bytes are ignored. It **always prints the order first** — `--dry-run` to check before writing.
- **Front matter leads its folder**: cover → foreword → index/contents → numbered pages. Matched
  on whole words, and the keyword must dominate the name, so `…-STEERING-COLUMN-COVER.jpg` and
  `DISCOVER.PDF` don't qualify. A publisher's shared prefix is stripped for a second attempt, so
  `PBGE95E1_…_COVER.pdf` leads instead of sorting last. For a print-captured manual whose
  filenames are topic names rather than page numbers, `--order docid` orders by the captured
  source URL instead.
- **The same page scanned twice** is merged once, by pixel area, and only if one copy is ≥2×
  smaller — similar resolutions may be two different pages, so both are kept and flagged.
  Grouping is per folder. Every drop is printed and **nothing is deleted from disk**.
- **An unreadable part is repaired, not fatal** — qpdf then Ghostscript, on a scratch copy. A
  repair is accepted only if it recovers at least as many pages as the original's raw bytes say it
  held, so a partial salvage can't pass as complete. Otherwise the merge is refused, listing every
  such file; `--skip-unrecoverable` combines the rest and prints an `INCOMPLETE:` summary before
  *and* after the result.
- Images are wrapped **losslessly** (img2pdf embeds the JPEG as-is).
- `--recursive` orders files and subfolders together at every level, so a subfolder's pages take
  their place in the sequence, and each section folder gets a **bookmark** at its first page.
  `--outline tree` bookmarks **every folder at every depth and every file by its name**, nested —
  for an export of one PDF per document, whose folder and file names are the only structure it
  has (a Lexus IS350 TIS export: 3,456 documents under system/subject folders, which one bookmark
  per top folder left unfindable). Titles are names on disk and no pages are added; the finished
  file is read back and the run prints how many of the outline items it carries.
- **The result is verified before the tool exits**: it must reopen and carry exactly the sum of
  its inputs' page counts, counted over the pages actually merged. Staged to a `.part` and moved
  into place only once that passes.

```
bertone\general info\GI-1.jpg …            21 sections, 1246 files
  ->  bertone.pdf   1220 pages, 135.0 MB, 26 low-res duplicates dropped, 21 bookmarks
```

## Repairing missing page resources (`pdfrepair.py`)

A manual assembled from many prints by a re-distributor can have pages whose content draws with a
font, image or graphics state their own `/Resources` don't hold — `/F4 12 Tf` on a page whose
`/Font` has only `/F5`. A viewer then substitutes: on a 2022–2024 Honda Civic FE/FL download, page
1501 rendered as letter-shifted gibberish ("+RZ WR XVH WKLV PDQXDO" for "How to use this manual")
and lost two images. `pdfrepair.py` is a standalone tool, not a pipeline stage:

```bash
python pdfrepair.py book.pdf                        # report only
python pdfrepair.py book.pdf --apply --dest out/    # write the repaired copy into out/
python pdfrepair.py book.pdf --apply                # repair in place
```

For each name a page uses and lacks, the same name is looked for on the nearest pages (`--reach`,
default 10 either side), and each candidate is **tried**: it is kept only if the page then lacks
fewer names and, for a font, its extracted text carries more real words — a wrong font gives
gibberish again, so it cannot win. Only the page's resource dictionary changes, and only to point
at objects already in the file. A page whose `/Contents` points at something that isn't content
(19 such pages in the same Civic) has lost its content; those are listed, not guessed at.

## Windows right-click menus

All three `.reg` files write only under `HKEY_CURRENT_USER`, so **no admin rights**; double-click to
install, and each has an `-uninstall.reg`. On Windows 11 look under *Show more options*.

`tools\compress-pdf-context-menu.reg` adds **Compress + OCR (searchable)** to a `.pdf`. Defaults
mean your original is never touched — the result lands beside it as `<name> (COMPRESSED).pdf`, no
report files appear next to it, and the window stays open so you can read the log. It installs
under `SystemFileAssociations\.pdf`, so it survives changing your default viewer.

`tools\compress-folder-context-menu.reg` adds **Compress + OCR this folder (in place)** to any
folder. It runs the tool with defaults plus `--in-place`, over the folder **and all its
subfolders**: every scanned PDF is overwritten with its verified, searchable result. Because that
is destructive and one stray right-click away, `tools\compress_folder_here.cmd` asks you to type
`YES` before it starts. No `--log` is passed, so nothing but the PDFs is written and the summary
stays in the console window.

`tools\combine-pdf-context-menu.reg` adds a **Combine PDF** submenu to any folder:

| menu item | runs |
|---|---|
| Preview page order (writes nothing) | `--dry-run` |
| Combine into one PDF | `--no-compress` |
| Combine including subfolders | `--recursive --no-compress` |
| Combine, then compress + OCR | the full default pipeline (slow) |

Each registry entry holds one path — its `tools\*_here.cmd` — which locates the script and the
repo's virtualenv relative to itself. If you move the checkout, edit that path and re-import.
