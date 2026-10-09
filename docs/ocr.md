# OCR

[← README](../README.md) · [all docs](../README.md#documentation)

How the searchable text layer is made: which engine reads the page, when an existing
layer is kept, and what happens when OCR fails.

- [OCR engines](#ocr-engines)
- [Carried OCR layers](#carried-ocr-layers)
- [OCR on awkward sources](#ocr-on-awkward-sources)
- [Text-layer decisions](#text-layer-decisions)
- [When OCR fails](#when-ocr-fails)

## OCR engines

Tesseract is the default. `--ocr-engine paddle` swaps in PaddleOCR PP-OCRv6 (the medium model),
run through [RapidOCR](https://github.com/RapidAI/RapidOCR) on onnxruntime by the
`ocrmypdf_paddle.py` plugin that ships beside the tool. It is an ordinary
[OCRmyPDF plugin](https://ocrmypdf.readthedocs.io/en/latest/plugins.html), so everything else —
which pages are OCR'd, the graft, the audit, the report — is unchanged, and it also works on its
own: `ocrmypdf --plugin ocrmypdf_paddle.py -l jpn+eng in.pdf out.pdf`.

Measured on two scanned Japanese wiring manuals, against text read off the page images by eye:

| | diagram labels found (28) | wrong Japanese chars | printed lines read exactly (63) |
|---|---|---|---|
| **PaddleOCR PP-OCRv6** | **25** | **3** | **57** |
| Tesseract `jpn+eng` | 14 | 13 | 46 |
| ABBYY FineReader (the files' own layers) | 19 | 45 | 42 |

Its one measured weakness is dense, leader-dotted contents pages, where it can drop whole lines
that Tesseract keeps. It reads Japanese, Chinese and English — not Cyrillic, so a Russian file
stays on Tesseract. Speed: ~75 s a page on a CPU, ~12 s on a GTX 1060 through DirectML (same
text). Language detection still runs through Tesseract, so Tesseract stays installed either way.

Pages reach the engine at their native resolution. On a 600 dpi scan that costs ~22 s a page on
the GTX 1060 and fills its 6 GB; `--paddle-dpi 300` reads them at 300 instead (12–18 s a page),
for a small measured loss — Japanese recall 0.805 against 0.819, the same 25 of 28 hand-checked
diagram labels.

```bash
pip install rapidocr onnxruntime-directml      # or onnxruntime (CPU) / onnxruntime-gpu (CUDA)
python ocrmyworkshopmanual.py SRC --ocr-engine paddle --paddle-dpi 300
```

The plugin gives vertical Japanese lines a text angle: without one, OCRmyPDF's renderer drops a
line whose box doesn't fit its text, and vertical text would silently vanish from the layer. The
engine's availability is checked at startup through the ocrmypdf the tool will actually run, so a
missing package fails the run up front rather than every file in turn.

### YomiToku, for Japanese

`--ocr-engine yomitoku` reads pages with [YomiToku](https://github.com/kotaro-kinoshita/yomitoku),
a Japanese-only OCR on PyTorch, through the `ocrmypdf_yomitoku.py` plugin. Measured on 16 pages
of 1998–2002 Japanese service, diagnosis, body-repair and wiring manuals (200–400 dpi scans),
against 354 strings read off the page images by eye — a string counts when it appears verbatim:

| | printed lines (109) | table cells (136) | diagram labels (109) | all (354) | s/page |
|---|---|---|---|---|---|
| **YomiToku 0.15** (`yomitoku`) | **104** | **133** | **106** | **343** | **1.3** (CUDA) |
| PaddleOCR PP-OCRv6 (`paddle`) | 83 | 125 | 99 | 307 | 3.6 (DirectML) |
| NDLOCR-Lite 1.2 | 91 | 89 | 29 | 209 | 1.4 (CPU) |

Paddle reads Chinese and Japanese with one model and puts simplified-Chinese forms into Japanese
words (交换 for 交換, 電压 for 電圧, 基準值 for 基準値); YomiToku makes single-kana slips instead
(サプ for サブ). YomiToku garbles a page printed sideways — a connector table read 28 of 40 as it
lies — so a page whose text reads mostly vertical is also read turned a quarter each way, and a
turned reading that is clearly more confident replaces it: that table then reads 40 of 40 (the
page takes three reads, 6.2 s). Without the turn YomiToku scores 331.

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu128   # NVIDIA GPU; skip for CPU
pip install yomitoku
python ocrmyworkshopmanual.py SRC --ocr-engine yomitoku
```

It needs **Python 3.13 or older**: every YomiToku since 0.5.2 declares `<3.14`, and pip on 3.14
quietly installs 0.4.1, which the plugin refuses. It reads Japanese and English; any other
language goes to Tesseract. YomiToku is licensed **CC BY-NC-SA 4.0 — non-commercial use only**;
it is an optional dependency, not part of this repository.

## Carried OCR layers

A scan that a desktop engine already made searchable used to be re-OCR'd on the compress path,
because compressing replaces each page's content stream and that engine's text lives in it — and
the audit then refused the file whenever Tesseract read worse. Measured on a 138-page, 89 MB
Japanese manual OCR'd by ABBYY FineReader: word recall 0.20, FAILED, original kept.

Now a page whose text is **separable** — every glyph it shows is invisible (`Tr 3`) and no Form
it draws holds text of its own — has that text lifted out before the swap and drawn back over the
compressed image, scaled from the source page's box to the new one. That manual now compresses to
**5.3 MB (6%)** with its ABBYY text intact on all 137 pages that had any; only the one page
without a layer is sent to OCR. The note says `(source text layer kept on 137 of 138 pg)`.
Rotated or cropped pages, Type 3 fonts, visible text and stamp-only layers take the ordinary OCR
path. `--no-ocr` keeps a carried layer too: "run no OCR" isn't "drop the text this file has".

## OCR on awkward sources

A file that isn't re-imaged (under the size floor, or not worth compressing) is OCR'd where it
sits. Two source shapes used to leave such a file with no text layer at all:

- **A page box that doesn't match its raster.** ocrmypdf rasterises at no less than 400 dpi on
  any page carrying text or vector content, and no option lowers that. One producer laid its
  images out at a pixel per *point*, so a 101-page manual's pages declare 70.78 × 97.19 in while
  holding ~29 MP of scan; at 400 dpi that is 1.1 gigapixels — past PIL's 500 MP limit, so a hard failure.
  The raster is now projected first, and a file over budget is rendered here at each page's own
  native dpi, OCR'd, and its text grafted onto the untouched original: 0 → 4,769 characters with
  every image byte-for-byte unchanged.
- **A fillable form.** ocrmypdf refuses `--redo-ocr` on one. Such a file is OCR'd as a *text
  donor* and only the donor's text is grafted on, so the shipped file keeps its own images
  (measured: its 1-bit CCITT scans kept, +14%, against 5× if the donor were shipped).

## Text-layer decisions

**A text layer says something different on every page; a stamp says the same thing.** Text
repeating on ~every sampled page is discounted wherever the tool asks "is this already
searchable?", so a paywall watermark can't answer yes for a file with no text layer. Deliberately
narrow: ≥3 pages must carry text, a line must appear on ~90% of them, and the whole thing must be
at most 4 lines / 400 chars — twenty identical lines is a form template, and a template is
content. Applied **line-wise**, so a page with a repeated running header plus body text keeps its
body. This check removes nothing — it only stops a stamp answering the question; the file is
reported with `boiler=2ln/66c` in `scan signals`. (A link-like stamp *is* removed, by the
separate [de-watermark stage](cleanup.md#re-distributor-stamps).)

## When OCR fails

A file whose OCR produces no text layer is **FAILED**, and nothing is written for it: the text
layer is the half of the output that cannot be added later without the original, so a file
without one is not finished. The row names the cause — the exit code is translated, so a native
crash reads `exit 0xc0000005 (access violation: the OCR engine crashed)`, not `exit 3221225477`
— and the ways out: clear the cause and `--retry-failed`, `--ocr-engine tesseract` if the paddle
engine was the one that failed, or `--no-ocr` to process the file without a text layer.
