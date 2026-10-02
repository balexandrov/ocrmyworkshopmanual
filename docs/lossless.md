# Born-digital optimisation

[← README](../README.md) · [all docs](../README.md#documentation)

A born-digital PDF is never rasterised, but it is optimised in every other way. It is big because
of **how its bytes are stored**, not what it draws — and storage can change without touching a
drawing operator. One option, `--born-digital LEVEL`, picks how far to go; each level adds a step
to the one before:

| level | adds | on the manual below |
|---|---|---|
| `copy` | nothing — byte-for-byte | 537.6 MB |
| `lossless` | Flate the **unfiltered** streams, bundle objects into `/ObjStm`, re-Deflate at level 9, delete the **per-illustration** authoring XMP, [merge duplicate images](#duplicate-images-and-jpeg) | 537.6 → 195.1 MB (measured before the merge existed) |
| `full` (default) | re-encode losslessly stored images as [JPEG q85](#duplicate-images-and-jpeg) | not measured on this file — see [below](#duplicate-images-and-jpeg): 252 → 67 MB |
| `max` | re-Deflate everything with **zopfli** — ~700× the CPU, a one-time archive pass | → ~172 MB |

Measured on `2020 WRX - WRX STI SERVICE MANUAL G1740BE.pdf` (537,575,830 bytes, 7,376 pages,
FrameMaker 7.2 → Distiller 9, PDF 1.4, 489,674 loose objects): **−62% in 3.2 minutes**, every
page's decoded content byte-for-byte identical.

**Where the bytes are.** Half that file — 254.6 MB — was XMP metadata stored with **no filter at
all**, and it was *per-illustration* metadata: each drawing's source `.ai`/`.eps` provenance hung
off its marked-content property dictionary (`Page /Resources /Properties /MC0…`), 44% of it a
base64 JPEG preview thumbnail. Two families exist and both are handled — 7,711 typed
`/Type /Metadata` packets and 9,898 untyped ones. The document-level packet in `/Root /Metadata`
is **kept**, and only the `/Metadata` key is deleted from each carrier, never the carrier itself,
since page content streams name those dictionaries via `BDC`.

**Expect the result to vary by producer, not by size** — it depends entirely on whether that
authoring chain wrote its metadata compressed:

| file | XMP found | result |
|---|---|---|
| 513 MB Subaru WRX (Distiller 9) | 7,710 **unfiltered** + 9,898 compressed | **−62%** |
| 236 MB Mitsubishi L200 (Distiller 6) | 0 unfiltered, 10,235 compressed | **−8%** |

**What is verified.** No page is rendered and no operator touched. Re-Deflating changes only
compression, and a recompressed stream is accepted only if it decodes to identical bytes. Dropping
XMP and merging duplicates are the only steps that alter the object graph; the JPEG step is the
only one that changes pixels. The output is discarded and the original bytes copied unless it is
at least 3% smaller **and** matches the source on:

- page, annotation, bookmark and named-destination counts
- **document-wide decoded content bytes and stream-part count** — not a sample; this is the check
  that caught an early version dropping 9,898 XMP streams while every count and sampled page
  still matched
- per-page content-stream + XObject fingerprints across a spread of pages (with the JPEG step on,
  an image is held to its size and colour space rather than its bytes)
- docinfo and document XMP, compared as **parsed fields** (pikepdf renormalises the packet on
  save, so bytes would differ on every file)

If the baseline can't be captured from the source, the rewrite is **skipped**, never treated as
passed. Under `--in-place` the source is fingerprinted, a temp written beside it, verified, then
atomically swapped — any failure leaves the original byte-identical. A corrupt-but-repairable file
still refuses in place, since repairing changes content rather than storage.

**Not preserved:** Fast Web View. The linearization hint stream (a pure index) is dropped;
relinearizing costs ~6 MB and 6× the save time and only matters for byte-range HTTP streaming.

## Duplicate images and JPEG

Some born-digital files are big for a reason none of the tiers above can reach: their
illustrations. A browser **"Print to PDF"** of a web manual (cairo) decodes every website JPEG
and stores it back as lossless 8-bit Flate RGB, and embeds an illustration afresh each time it is
placed. On a 252 MB, 1,941-page Acura RDX chapter, 240 MB was images:

| storage of those images | size |
|---|---|
| as found (Flate, no predictor) | 217.4 MB + 22.2 MB of exact duplicates |
| exact PNG predictors + zlib 9 | 92% |
| JPEG 2000 reversible | 144% — bigger |
| **JPEG quality 85** | **29%** |

- **Duplicates are merged** from the `lossless` level up. Exact — the kept copy has the same
  bytes and dictionary as every one it replaces — so the guard is unchanged.
- **Losslessly stored images become JPEG at quality 85** at the `full` level, the default
  (`--born-digital lossless` keeps every image exact). These are diagrams, not photographs: at q85 the labels and link
  text baked into an illustration were indistinguishable from the original at 3× zoom. 8-bit
  gray/RGB images are re-encoded. Left alone: alpha masks and stencils, colour-keyed, indexed,
  CMYK and Lab images, anything already JPEG/JPX/JBIG2/CCITT, images under 64×64, and any image
  whose JPEG is not 10% smaller. Each JPEG must decode back within 30 dB PSNR of the original or
  the original is kept — on real data that turns away small text-heavy charts, where JPEG rings.
  The guard compares each image's size and colour space instead of its bytes; everything else it
  checks is unchanged, and the row's `reason` reads `images recompressed`.

On that chapter, with the defaults: **240.7 → 67.0 MB (−72%) in 46 s**, 2,086 images
re-encoded and 262 duplicates merged. An independent pypdf comparison found every page's text,
all 7,760 link annotations, all 100 bookmarks and every image placement identical to the source.

## Sweeping an existing archive

Every born-digital file a past run touched was reported as `born digital`, so those rows already
are the inventory — no re-scan needed:

```bash
# 1. build the work list from the run reports you already have
python helpers/lossless_candidates.py --min-mb 50 --sample 8

# 2. rewrite into a staging tree, so a "before" copy still exists
python ocrmyworkshopmanual.py --from-list reports/lossless_list.txt --dest OUT --no-ocr --log reports \
    --born-digital lossless   # verify_lossless.py compares pixels, so keep the images exact

# 3. audit the pairs independently of the code that produced them
python helpers/verify_lossless.py --before SRC_ROOT --after OUT --render 3

# 4. replace the originals with the verified outputs
python helpers/promote_lossless.py --before SRC_ROOT --after OUT --audit reports/lossless_audit.csv --apply
```

`--in-place` skips steps 2–4 and is fully verified per file, but leaves **no before copy**, so
step 3 becomes impossible. On the first band that independent audit found three bugs — all of them
in the audit code, none in the rewrite — which is the argument for staging a large pass.

**`verify_lossless.py`** asks what the run's own guard never does: *of everything that
disappeared, what was it?* It pairs streams by the hash of their decoded bytes (qpdf renumbers
objects), tests **reachability** from the trailer so dead objects aren't mistaken for losses, and
classifies every removal as XMP packet, linearization hint stream, or **failure**. `--render N`
also compares raw pixels (Ghostscript) and per-page text (poppler `pdftotext`, which reaches deep
pages Ghostscript can't on flat-page-tree files).

**`promote_lossless.py`** promotes a file only if its audit row says `ok`, the output is genuinely
smaller, the page count still matches when re-checked, and the copy beside the original is
byte-identical to the audited output — then one atomic `os.replace`. `--audit` is optional.
Read-only originals (mode 444 / Windows `R`, common on files copied off a CD) would fail with
`PermissionError`; the flag is cleared and **left cleared**.

Two bands of one archive swept in full, the third a sample estimate:

| band | files | total | saving | cost |
|---|---|---|---|---|
| ≥ 50 MB | 127 | 17.0 GB | **−29%** (4.99 GB) — whole band | 27 min |
| 5–50 MB | 774 | 12.1 GB | **−12%** (1.49 GB) — whole band | 28 min |
| < 5 MB | 282,676 | 19.2 GB | ≈−12% (≈2.3 GB) — *300-file sample* | ~10 h |

**Don't size a sweep from a small sample.** A 10-file rewrite of the 5–50 MB band projected −36%;
all 774 gave **−12%** — median 14%, min 3%, max 94%, and 189 files with nothing to gain. A
signature scan mispredicts the *opposite* way: those files show 0% unfiltered bytes and almost no
XMP, which reads as "nothing here" while they yield 1.5 GB from object streams alone. The sub-5 MB
band is the largest pool and the worst value, and its cost is per-file **I/O**: 28 files/s on the
archive drive regardless of thread count, 8.1 files/s end-to-end.
