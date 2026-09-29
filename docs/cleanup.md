# Source clean-up

[← README](../README.md) · [all docs](../README.md#documentation)

Stages that run on the **source**, before any lane reads it, so born-digital and in-place files
get them too. Each is on by default, audits its own rewrite, and only changes what the later
stages read — nothing replaces the original until the finished file has been verified. The stamp,
space and font stages also run on their own (`pdfwatermark.py`, `pdfspaces.py`, `pdffonts.py`):
report by default, `--apply` to rewrite.

- [Encrypted PDFs](#encrypted-pdfs)
- [Re-distributor stamps](#re-distributor-stamps)
- [Box spaces](#box-spaces)
- [Per-page font subsets (`pdffonts.py`)](#per-page-font-subsets-pdffontspy)
- [Lines a caller names (`pdfwatermark.py --strip-line`)](#lines-a-caller-names-pdfwatermarkpy---strip-line)

## Encrypted PDFs

Any encrypted PDF is re-stored **decrypted**, and its owner permission flags (`extract`,
`modify_*`) are dropped — on **every** lane (`--no-decrypt` opts out). It is done once, up front,
because the lanes did not agree: anything that re-saves the PDF through qpdf or ocrmypdf dropped
the encryption as a side effect, while the byte-copy paths — a born-digital PDF copied untouched,
a lossless rewrite under its floor, an in-place file left as-is — preserved it and shipped a file
with text extraction and accessibility still withheld. Whether a file stays locked should not
depend on which lane it happened to take.

Measured by probing one file per folder across two brands (2,393 folders, 15 encrypted): **every
one opens with an empty user password**, so the encryption holds permission flags, not a lock,
and refusing those files bought no safety. Revisions found were 14 × RC4-40 (`/V 1 /R 2`) and
1 × RC4-128 (`/V 2 /R 3`) — RC4-40 is the common case here, and 10 of the 15 did not even
withhold extraction. Passwords tried are `''` and `vector`; a file that fits neither is skipped and
reported as `encrypted: none of the known passwords fit`, never as unreadable or damaged.

It is a real change to the file, so it is recorded per file in the report's `note` column. Page
content is untouched and still audited — a permission flag cannot change what a page draws. On
the [lossless lane](lossless.md), where bytes are not what these files buy, a decrypted file is
exempt from `--lossless-min-savings`: the bar is only *not bigger than the source*. On a 26-file
measurement the median was 3.6% smaller with 11 under the 3% default, so a size bar would have
left half of them encrypted for a rounding error.

## Re-distributor stamps

A file that has passed through a download site often carries that site's signature — a domain
or an email address — drawn onto every page in one pass. It's content nobody asked for, OCR
reads it out on every page, and on a landscape page it was measured sitting on top of a table
row. `pdfwatermark.py` removes it on the **source**, before anything renders it: after a render
the stamp is pixels. `--no-dewatermark` opts out.

A stamp is text that passes **all three** of these, because dropping any one was measured to
cause damage:

1. **A marker** — it is link-like (`example.org`, `https://…`) or a converter's licence nag
   (`trial version`). Repetition alone takes section titles, running footers, `HINT:` labels and
   wiring-diagram terminal names out of manuals that carry no stamp at all.
2. **It repeats** character for character, from the same operator, on consecutive pages. A
   manual cites a real URL once; it doesn't cite it identically on every page.
3. **It is on a line of its own.** Without this, `: www.motul.fr` in a lubricant maker's own
   address block passes the other two tests: other text on the line came to 0–3 characters for
   four real stamps, and 124 for that footer.

Where it sits on the page is **not** a test. It used to be ("the bottom 12%"); over 3,007 archive
files that condition prevented zero false positives, and its one effect was hiding a real stamp
in the top margin. Nothing about a particular site is hard-coded.

Removal is exact because the stamp is *drawn*, not painted in: the show-text or `Do` operator that
draws it is deleted and every other byte of the page stays — no re-render, no re-compression.
Only an operator whose whole text is the confirmed repeating string goes; where a stamp shares an
operator with page content it is left, and counted. The rewrite is audited against the source
(page, bookmark and annotation counts, every token that vanished, rendered ink on a sample of
pages); if any check fails the stamp stays, the file is still processed, and the report says the
stamp was found and not removed. Otherwise the note reads
`(watermark 'example.org' removed from N pages, N operators)`.

    python pdfwatermark.py book.pdf                # report
    python pdfwatermark.py book.pdf --apply        # rewrite in place
    python pdfwatermark.py book.pdf --pages 1-8 --dest out/

## Box spaces

Some Interleaf-era converters subset their TrueType fonts at `/FirstChar 37`, so the subset's
cmaps stop above code 32: every **literal** space maps to glyph 0, `.notdef`, and a strict viewer
draws a box at each word gap. The same page can be clean where the converter emitted a kerned
array and boxed where it drew each word separately with `( )Tj` between them, so the fault looks
random. Over one make's tree of such manuals it was in 4,991 of 21,524 files.

`pdfspaces.py` (also runnable on its own) makes three edits per affected font: descriptor
`/Flags` Symbolic instead of Nonsymbolic, `/Encoding` dropped, and the embedded font's `(1,0)`
format-0 cmap entry for code 32 set to glyph 32 — which is already present and **empty**, so it
draws nothing. Steps 1–2 alone change nothing; step 3 is one byte and no table length. Code 32
is outside `/FirstChar..LastChar`, so its width was and stays `/MissingWidth` (0) and all the
spacing still comes from the `Tw` the converter emitted: the layout cannot move.

Only a font with exactly the fault is touched (simple `/TrueType`, `/FirstChar > 32`, embedded
`/FontFile2`, a format-0 cmap, glyph 32 present, empty and unmapped), in page resources and in
Form XObjects. It runs on the **source**, before anything renders it, like de-watermarking, and a
rewrite forces the write on the byte-copy lanes. The rewrite is audited — page count, every page's
content stream byte-identical, font count, no font still faulty — and dropped if any check fails.

After the fix a whole-page text extraction reads those gaps as spaces, but a *clipped* one
(PyMuPDF's `get_textbox`) returns U+FFFD for them; code that parses text inside a rectangle should
treat U+FFFD as a gap.

## Per-page font subsets (`pdffonts.py`)

Some producers embed a fresh subset of the same face on every page — measured on a 1,449-page
chapter, 733 simple TrueType fonts from 4 faces, 18.3 MB of a 29.3 MB file, because every subset
carries the face's full hinting programs. `pdffonts.py` merges each face's subsets into one font.
The main pass does this on every file's source, before any lane reads it (`--no-merge-fonts`
opts out); on its own:

    python pdffonts.py book.pdf                  # report
    python pdffonts.py book.pdf --apply          # rewrite

Two subsets are one face only when their unitsPerEm, glyph count, table set and hinting programs
(fpgm, prep, cvt) are identical, every code both draw has a byte-identical outline and the same
PDF width, and they share at least five codes. The merge is **by glyph ID**: those subsets keep
the face's IDs but name only the slots they fill, so a slot takes the glyph of whichever member
draws it and the merged font's IDs are the face's own. Font dictionaries keep their `/Widths` and
`/Encoding`; no content stream is touched. The rewrite is kept only if the file got smaller, every
page's text is identical and a spread of pages renders pixel-identical. On the chapter above:
29.3 → 11.1 MB.

## Lines a caller names (`pdfwatermark.py --strip-line`)

Detection only finds a stamp that carries a marker — a link, a licence nag. A third-party
printout of a maker's pages can add text that carries none: measured on a 23,970-page repair
manual, the service's two-line running header on every page (in two to four show-text pieces,
drawn twice over itself on 247 pages) and 13,461 "Courtesy of …" credit lines under figures.
For those the caller names the lines:

    python pdfwatermark.py book.pdf --strip-line "2010 Make Model" --strip-line "2010 [A-Z].*" --band top:0.08
    python pdfwatermark.py book.pdf --strip-line "Courtesy of MAKER SALES, INC\." --apply --dest out/

The unit is a whole displayed **line**: a baseline goes only when all of its text, joined in
reading order, fully matches a rule (one or more copies of it). A phrase inside a longer line —
"Courtesy Light Switch Assembly Connector" — stays. `--band` confines rules to the top or bottom
of the page; a report lists what the band held that no rule matched. Removal is the stamp
remover's own operator-level code (nothing is rasterised), and the audit re-derives from the
source, not from the matcher, that only whole named lines went: every checked page's text must
be the source's minus exactly those lines, and every rendered pixel that changed must lie inside
a removed line's box. Line boxes come from the fonts' own widths: the text walker advances the
text position after every piece, as a viewer does.

Taking a header's words off can leave its frame. `--band-graphics` (with `--band`) removes the
vector paths drawn entirely inside the band — never one reaching the side margins, where a page
border runs, and never a clipping path. Its audit: every page's text unchanged, and no rendered
pixel changed outside the band.

    python pdfwatermark.py book.pdf --band top:0.09 --band-graphics --apply
