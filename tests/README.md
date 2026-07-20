# tests/ — page-type detection & settings-tuning corpus

Real scanned pages, 5 per `PageType`, each from a **different source file**, used to
(1) prove the router classifies pages correctly and (2) sweep pipeline settings so
tuning is evidence-driven instead of "looked fine the first time".

```
tests/
  fixtures/
    line/  blank/  photo_gray/  photo_color/   # single-page PDFs, one page each
    manifest.json                              # provenance + measured signals + verified flag
    DIVERGENCES.md                             # pages where a human read differs from classify_page()
  reports/           (git-ignored) settings_matrix.csv + artifacts/  from the sweep
  extract_fixtures.py   build the corpus from real scans
  make_blank_fixture.py synthesize blank pages (corpus has no real blank verso)
  test_classify.py      DETECTION test: classify_page() == folder label
  test_settings_matrix.py  SETTINGS sweep (script) + sanity gates (pytest)
  test_born_digital.py  SAFETY test: born-digital PDFs detected + copied untouched
```

Current corpus (real Land Cruiser FSM pages unless noted): **line 7** (7 sources),
**photo_gray 7** (5 sources), **photo_color 5** (5 sources), **blank 2** (synthetic —
these tightly-cropped scans contain no true blank verso; see make_blank_fixture.py).
Several fixtures are deliberately tricky/borderline (sepia halftone that must stay
gray, mixed text+diagram, stipple drawings, gray-band foldout) — see each entry's
`note` in manifest.json. One human-vs-classifier divergence is logged in
DIVERGENCES.md (a low-saturation green cover the colour test calls gray).

## 1. Build the corpus (once, from your real scans)

Point it at scanned PDFs or a folder tree. It samples pages, classifies them, and
picks 5 per type from **distinct source files**:

```bash
.venv/Scripts/python.exe tests/extract_fixtures.py "M:\path\to\scanned\manuals"
# or specific files:
.venv/Scripts/python.exe tests/extract_fixtures.py a.pdf b.pdf --per-type 5
# force a specific known page in:
.venv/Scripts/python.exe tests/extract_fixtures.py --add "a.pdf:37:photo_color"
.venv/Scripts/python.exe tests/extract_fixtures.py --list
```

Auto-picked labels come from the very classifier under test, so they're
**candidates**. Open each extracted PDF, confirm the label, and set
`"verified": true` in `fixtures/manifest.json` (or delete/re-file a wrong one).
`test_classify.py` fails while any fixture is still unverified.

## 2. Run the detection tests

```bash
.venv/Scripts/python.exe -m pytest tests/test_classify.py -v
```

## 3. Sweep settings and tune

```bash
.venv/Scripts/python.exe tests/test_settings_matrix.py
```

Writes `tests/reports/settings_matrix.csv` (output size + re-classification per
combo) and `tests/reports/artifacts/<type>/<fixture>/` (binarized PNG / JPEG per
combo) to eyeball. Grids live at the top of `test_settings_matrix.py`
(`BITONAL_COMBOS`, `PHOTO_GRAY_COMBOS`, `PHOTO_COLOR_COMBOS`) — edit them, re-run,
then change a default in `ocrmyworkshopmanual.py` with the numbers in hand.

Requires Ghostscript + jbig2enc on PATH (same as the main tool); tests skip
cleanly if they're missing.
