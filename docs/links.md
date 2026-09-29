# Browser links

[← README](../README.md) · [all docs](../README.md#documentation)

These manuals cross-reference each other constantly — "(See page IN-27)" — and each of those is
a `/GoToR` action: *go to a remote file*, naming a sibling by a relative filename.

```
/A << /S /GoToR  /F << /Type /Filespec /F (m_in_0027.pdf) >>  /D [0 /FitH 845] >>
```

**No browser follows it.** Chrome's PDFium ignores it and Firefox's pdf.js won't follow it to a
local file, both deliberately — an action that sends the viewer to an arbitrary path on the
reader's disk is an attack surface. Desktop readers do honour it, so the links aren't broken;
they're addressed to a viewer these files aren't read in. They're rewritten to the one action a
browser does follow, and the URL stays **relative** so the tree can be served from anywhere:

```
/A << /S /URI  /URI (m_in_0027.pdf#page=1) >>
```

What this handles, each because it silently broke something:

- **Both places links live.** Page `/Annots` *and* the `/Outlines` tree — the sidebar menu. They
  are separate objects even when they say the same thing, so fixing only annotations leaves a
  contents page whose body works and whose menu beside it is dead.
- **`/Launch` too**, which no browser runs either. It carries no destination, so its URL gets no
  fragment.
- **Symbolic destinations.** Most links address the target by name (`/D (E.B0010439)`), which a
  URL fragment can't carry. The target is opened once, its name tree read, and the name reduced
  to a page — otherwise every one of them lands on page 1 of a 400-page section.
- **Case.** Authored on Windows, these links say `FWD.pdf` where the file is `fwd.pdf`.
  `os.path.exists` says yes and the URL then 404s on a case-sensitive server, turning an inert
  link into a visible error. Every segment is matched against the real directory listing and the
  on-disk spelling is what goes in.
- **Renamed sections.** Newer sets renamed `GI.pdf` to `gi-general_information.pdf` but left the
  links naming the old code; accepted only when exactly one file carries that code.
- **Page numbers out of range.** `/GoToR` pages are 0-based and `#page=` is 1-based; a source
  index one past the end of a foldout becomes `#page=7` of 6, which makes Chrome drop the
  fragment and open page 1. Clamped to the last page instead.
- **Internal `/GoTo` and existing `/URI` are never touched**, so a second pass is a no-op.

**A target that isn't beside the linking file is left alone, and counted.** This is the one
design decision worth stating, because the obvious alternative is worse. Walking up the tree for
a same-named file and taking it when only one candidate survives sounds safe and isn't — section
names repeat across a whole collection. Measured on a 347,821-file archive (1,200 files sampled,
31 carrying such links, 370 links):

| | |
|---|---|
| resolved as a plain sibling | 273 (74%) |
| resolved at the relative path the link gives | 2 |
| the walk: resolved to the same model | **0** |
| the walk: resolved into **another car's manual** | **5** |
| the walk: ambiguous or nowhere | 92 |

On one brand's tree the walk produced 767 unique hits of which all 767 were cross-model — a 2001
Prius wiring diagram's `../../../../ewdsourc/2001/01priuse/electric/parts.pdf` resolving into the
2000 Land Cruiser EWD. Under one brand `gi.pdf` names 436 different files. **No later check can
catch this**, because a wrong-car link points at a file that really exists — so the only defence
is not to guess. A link that quietly does nothing beats one that confidently opens the wrong car,
and the report's `links left` column names the files where it happened.

Everything written *is* verified: the result must open, keep every page and link, and every URL
must resolve case-sensitively on disk with its `#page=` inside the target's length. A failure
leaves the file exactly as it was and says why.

`pdflinks.py` is also runnable on its own — `python pdflinks.py <file-or-dir>` to report,
`--apply` to rewrite.
