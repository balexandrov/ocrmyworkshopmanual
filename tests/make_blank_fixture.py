#!/usr/bin/env python3
"""
make_blank_fixture.py — synthesize realistic scanned-BLANK fixture pages.

The Land Cruiser corpus this suite is built from has no true blank page (ink <
0.0008): the downloaded manuals were cropped tight and blank versos removed. The
BLANK branch of classify_page() is nonetheless real (near-empty pages fold into
the JBIG2 run), so we generate a couple of realistic empty scanned pages —
near-white paper with faint grain and a few dust specks, kept under the ink
threshold — rather than leave the type uncovered. Deterministic (fixed seed) so
the fixtures are reproducible.

    python tests/make_blank_fixture.py        # writes tests/fixtures/blank/*.pdf + manifest rows
"""
from __future__ import annotations

import numpy as np
from PIL import Image
import img2pdf

import _util as U

DPI = 200
H, W = 2339, 1654  # ~A5 at 200 dpi, matching the small scanned pages


def make_page(seed: int, paper: int, specks: int) -> np.ndarray:
    """Near-white page: flat paper tone + faint grain + a few dark dust specks,
    kept well under the blank ink threshold (fraction of px <100)."""
    rng = np.random.default_rng(seed)
    g = np.full((H, W), paper, dtype=np.int16)
    g += rng.normal(0, 3.0, (H, W)).astype(np.int16)          # paper grain
    for _ in range(specks):                                    # scan dust
        y, x = rng.integers(0, H), rng.integers(0, W)
        r = int(rng.integers(1, 3))
        g[max(0, y - r):y + r, max(0, x - r):x + r] = rng.integers(20, 90)
    return np.clip(g, 0, 255).astype(np.uint8)


def main():
    out_dir = U.FIXTURES_DIR / 'blank'
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = U.load_manifest()
    have = {e['source'] for e in entries}
    variants = [
        ('synthetic_blank_white', 42, 250, 25),    # clean white paper
        ('synthetic_blank_cream', 7, 244, 40),     # slightly toned/older paper
    ]
    for name, seed, paper, specks in variants:
        arr = make_page(seed, paper, specks)
        ink = float((arr < 100).mean())
        img = Image.fromarray(arr)
        # embed a JPEG (a near-white page as lossless PNG is ~2 MB; JPEG is ~tens of KB)
        jpg = out_dir / f'{name}.jpg'
        img.save(jpg, 'JPEG', quality=85, dpi=(DPI, DPI))
        pdf = out_dir / f'{name}.pdf'
        with open(pdf, 'wb') as f:
            f.write(img2pdf.convert(str(jpg), dpi=DPI))
        jpg.unlink(missing_ok=True)
        # confirm it classifies BLANK before recording
        work = U.workdir()
        try:
            t, sig = U.classify(pdf, 1, DPI, work)
        finally:
            import shutil; shutil.rmtree(work, ignore_errors=True)
        src_id = f'synthetic:{name}'
        if src_id in have:
            entries = [e for e in entries if e['source'] != src_id]  # replace
        entries.append({
            'file': f'blank/{name}.pdf', 'type': 'blank', 'source': src_id,
            'source_page': 1, 'signals': sig, 'classifier_said': t,
            'verified': True,
            'note': f'SYNTHETIC blank (paper={paper}, ink={ink:.6f}); corpus has no '
                    f'real blank verso. Reproducible via make_blank_fixture.py.',
        })
        print(f'  + {name}.pdf  ink={ink:.6f}  classifier={t}')
        assert t == U.owm.PT_BLANK, f'{name}: expected blank, got {t} (raise paper/lower specks)'
    U.save_manifest(entries)
    print(f'manifest -> {U.MANIFEST}')


if __name__ == '__main__':
    main()
