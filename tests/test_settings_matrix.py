"""
Settings-matrix harness — run every fixture through the pipeline across a GRID of
settings and record the result, so tuning is data-driven instead of "looked fine
the first time".

Two ways to use it:

  # 1) Full sweep -> CSV + eyeball artifacts (run this when tuning):
  python tests/test_settings_matrix.py
      writes tests/reports/settings_matrix.csv   (size + re-classification per combo)
      writes tests/reports/artifacts/<fixture>/  (binarized PNG / JPEG per combo)

  # 2) Light sanity gates under pytest (run in CI):
  pytest tests/test_settings_matrix.py
      asserts each fixture still produces a valid, non-empty, smaller output and
      that its detection is stable across the reasonable settings in the grid.

The CSV is the point: open it, sort by size or eyeball the artifacts, then change a
default in ocrmyworkshopmanual.py with evidence in hand.
"""
from __future__ import annotations

import csv
import shutil
import subprocess

import numpy as np
import pytest
from PIL import Image

import _util as U
from _util import TYPE_DIRS

DPI = 200

# ── the grids ────────────────────────────────────────────────────────────────
# Bitonal (line/blank): binarization knobs. Each combo -> a binarized PNG + its
# generic-JBIG2 size (bytes) — the two things that decide crispness vs file size.
BITONAL_COMBOS = [
    dict(mode='adaptive', sauvola_k=0.20),
    dict(mode='adaptive', sauvola_k=0.30),   # current default
    dict(mode='adaptive', sauvola_k=0.40),
    dict(mode='global', threshold=110),
    dict(mode='global', threshold=125),      # legacy default
    dict(mode='global', threshold=140),
]

# Grayscale photo/mixed: quality x descreen x paper-clean.
PHOTO_GRAY_COMBOS = [
    dict(quality=q, descreen=d, clean=c)
    for q in (40, 60, 80) for d in (0.0, 0.6) for c in (True,)
] + [dict(quality=60, descreen=0.6, clean=False)]  # what paper-clean actually buys

# Colour: quality x downsample dpi.
PHOTO_COLOR_COMBOS = [
    dict(quality=q, photo_dpi=pd) for q in (40, 60, 80) for pd in (110, 150)
]


def _bitonal_row(pdf, combo, art_dir):
    """Binarize page 1 with `combo`, JBIG2 it, return metrics + save the PNG."""
    work = U.workdir()
    try:
        png = work / 'p.png'
        if not U.render_gray(pdf, 1, DPI, png):
            return None
        raw_black = float((np.asarray(Image.open(png).convert('L')) < 128).mean())
        adaptive = combo['mode'] == 'adaptive'
        U.owm.binarize_png(png, adaptive, combo.get('threshold', 125), 10, True, DPI,
                           combo.get('sauvola_k', 0.30))
        ink_kept = float((np.asarray(Image.open(png)) == 0).mean())
        r = subprocess.run([U.owm.JBIG, '-p', '-a', '-D', str(DPI), png.name],
                           cwd=work, capture_output=True)
        jb2 = len(r.stdout)
        tag = (f"{combo['mode']}_k{combo['sauvola_k']}" if adaptive
               else f"global_t{combo['threshold']}")
        art_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(png, art_dir / f'{tag}.png')
        return {'combo': tag, 'out_bytes': jb2, 'raw_black_frac': round(raw_black, 4),
                'ink_kept_frac': round(ink_kept, 4)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _photo_row(pdf, page_type, combo, art_dir):
    """Run the photo strategy on page 1 with `combo`; return JPEG size + save it."""
    work = U.workdir()
    try:
        png = work / 'p.png'
        if not U.render_gray(pdf, 1, DPI, png):
            return None
        photo_dpi = combo.get('photo_dpi', 150)
        pc = U.owm.classify_page(png, 1, pdf, work, DPI, True, 0.02, photo_dpi)
        if pc.color_png is None:
            return None  # classifier no longer sees a photo here — recorded as skip
        d = photo_dpi or DPI
        out_pdf = work / 'seg.pdf'
        U.owm.photo_seg_pdf(pc, out_pdf, work, 1, d, combo['quality'],
                            combo.get('clean', True), combo.get('descreen', 0.6))
        jpg = work / 'photo1.jpg'
        jpg_bytes = jpg.stat().st_size if jpg.exists() else 0
        if page_type == 'photo_gray':
            tag = f"q{combo['quality']}_ds{combo['descreen']}_clean{int(combo['clean'])}"
        else:
            tag = f"q{combo['quality']}_dpi{combo['photo_dpi']}"
        art_dir.mkdir(parents=True, exist_ok=True)
        if jpg.exists():
            shutil.copyfile(jpg, art_dir / f'{tag}.jpg')
        return {'combo': tag, 'out_bytes': jpg_bytes}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_matrix() -> list[dict]:
    """Full sweep over every fixture. Returns flat rows for the CSV."""
    rows: list[dict] = []
    for page_type in TYPE_DIRS:
        for pdf in U.fixture_pdfs(page_type):
            art_dir = U.REPORTS_DIR / 'artifacts' / page_type / pdf.stem
            orig = pdf.stat().st_size
            if page_type in ('line', 'blank'):
                combos, runner = BITONAL_COMBOS, \
                    lambda c: _bitonal_row(pdf, c, art_dir)
            elif page_type == 'photo_gray':
                combos, runner = PHOTO_GRAY_COMBOS, \
                    lambda c: _photo_row(pdf, page_type, c, art_dir)
            else:
                combos, runner = PHOTO_COLOR_COMBOS, \
                    lambda c: _photo_row(pdf, page_type, c, art_dir)
            for combo in combos:
                m = runner(combo)
                if m is None:
                    continue
                rows.append({'type': page_type, 'fixture': pdf.name,
                             'orig_bytes': orig, **m,
                             'pct_of_orig': round(100 * m['out_bytes'] / orig, 1)
                             if orig else ''})
    return rows


def write_csv(rows: list[dict], path=None):
    path = path or (U.REPORTS_DIR / 'settings_matrix.csv')
    U.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    cols = ['type', 'fixture', 'combo', 'orig_bytes', 'out_bytes', 'pct_of_orig',
            'raw_black_frac', 'ink_kept_frac']
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    return path


# ── pytest sanity gates (cheap; full sweep is the __main__ path) ──────────────
_missing = U.tools_missing()
pytestmark = pytest.mark.skipif(_missing is not None, reason=str(_missing))


def _fixture_params():
    out = []
    for t in TYPE_DIRS:
        for pdf in U.fixture_pdfs(t):
            out.append(pytest.param(t, pdf, id=f'{t}/{pdf.name}'))
    return out


_FX = _fixture_params()
if not _FX:
    pytest.skip('no fixtures yet — see extract_fixtures.py', allow_module_level=True)


@pytest.mark.parametrize('page_type,pdf', _FX)
def test_default_settings_produce_a_valid_output(page_type, pdf):
    """At the shipped defaults each fixture must yield a non-empty output. For
    BITONAL pages (line/blank) the generic-JBIG2 stream must also be much smaller
    than the source page — that's the whole point on line-art. For PHOTO pages we
    do NOT assert 'smaller': re-rendering at full dpi + re-JPEG can legitimately
    exceed an already-compressed source page, which is exactly why the real pipeline
    has the min-savings 'keep the original' fallback. Whether a photo page is worth
    recompressing is a tuning question — that's what the settings-matrix CSV answers,
    not a pass/fail gate here."""
    art_dir = U.workdir()
    try:
        if page_type in ('line', 'blank'):
            m = _bitonal_row(pdf, dict(mode='adaptive', sauvola_k=0.30), art_dir)
        elif page_type == 'photo_gray':
            m = _photo_row(pdf, page_type, dict(quality=60, descreen=0.6, clean=True), art_dir)
        else:
            m = _photo_row(pdf, page_type, dict(quality=60, photo_dpi=150), art_dir)
    finally:
        shutil.rmtree(art_dir, ignore_errors=True)
    assert m is not None, f'{pdf.name}: pipeline produced no output at defaults'
    assert m['out_bytes'] > 0, f'{pdf.name}: empty output'
    if page_type in ('line', 'blank'):
        assert m['out_bytes'] < pdf.stat().st_size, (
            f'{pdf.name}: bitonal output {m["out_bytes"]}B not smaller than source '
            f'{pdf.stat().st_size}B')


def main():
    err = U.tools_missing()
    if err:
        print(f'ERROR: {err}'); return
    if not _FX:
        print('No fixtures yet. Build them first with extract_fixtures.py'); return
    print(f'Running settings matrix over {len(_FX)} fixture(s)...')
    rows = run_matrix()
    path = write_csv(rows)
    print(f'\n{len(rows)} rows -> {path}')
    print(f'artifacts -> {U.REPORTS_DIR / "artifacts"}')
    # compact per-fixture best/worst so the terminal is useful on its own
    by_fx: dict[str, list] = {}
    for r in rows:
        by_fx.setdefault(f'{r["type"]}/{r["fixture"]}', []).append(r)
    print('\nsize range per fixture (bytes):')
    for fx, rs in by_fx.items():
        lo = min(rs, key=lambda r: r['out_bytes'])
        hi = max(rs, key=lambda r: r['out_bytes'])
        print(f'  {fx:40s} {lo["out_bytes"]:>8d} ({lo["combo"]})  ..'
              f'  {hi["out_bytes"]:>8d} ({hi["combo"]})')


if __name__ == '__main__':
    main()
