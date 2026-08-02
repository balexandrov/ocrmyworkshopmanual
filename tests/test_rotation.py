"""Rotation must not be applied twice.

Ghostscript BAKES a page's /Rotate into the render, so the compressed page image is
already upright. `_graft_into_source` puts that image back into the ORIGINAL document,
which still carries the source /Rotate — and if that value survives, the viewer rotates
the already-upright image a second time and the page ships sideways.

This went undetected in production for a whole archive pass (a 533-page Daihatsu manual
came out entirely sideways) precisely because every existing guard still passes: the page
count, colour depth, text layer and links are all identical after a rotation. Only the
geometry changes. Hence a dedicated test.
"""
from __future__ import annotations

import pytest

import _util as U
from _util import owm

pikepdf = pytest.importorskip('pikepdf')


def _make_pdf(path, *, w, h, rotate=None, inherited=False, npages=2):
    """A minimal PDF whose pages are `w`x`h` with an optional /Rotate, set either on
    each page or (when `inherited`) only on the parent /Pages node."""
    pdf = pikepdf.Pdf.new()
    for _ in range(npages):
        pdf.add_blank_page(page_size=(w, h))
    if rotate is not None:
        if inherited:
            pdf.Root.Pages['/Rotate'] = rotate
        else:
            for pg in pdf.pages:
                pg.obj['/Rotate'] = rotate
    pdf.save(str(path))
    pdf.close()


@pytest.mark.parametrize('inherited', [False, True], ids=['on-page', 'inherited'])
def test_graft_clears_baked_in_rotation(tmp_path, inherited):
    """Source is rotated; the 'compressed' page is upright with a swapped box and no
    /Rotate. After the graft the page must be upright — /Rotate 0 — not rotated again."""
    src = tmp_path / 'src.pdf'
    comp = tmp_path / 'comp.pdf'
    # Source: landscape box + /Rotate 90  ->  displays portrait.
    _make_pdf(src, w=842, h=595, rotate=90, inherited=inherited)
    # Compressed: what Ghostscript+img2pdf produce — the upright portrait raster, no /Rotate.
    _make_pdf(comp, w=595, h=842, rotate=None)

    assert owm._graft_into_source(src, comp) is not False

    with pikepdf.open(str(comp)) as out:
        for pg in out.pages:
            assert int(pg.obj.get('/Rotate', 0)) == 0, (
                'source /Rotate survived onto an already-upright render — '
                'the page will display rotated twice'
            )
            mb = [float(v) for v in pg.obj['/MediaBox']]
            assert (mb[2] - mb[0], mb[3] - mb[1]) == (595, 842)


def test_graft_keeps_rotation_of_passthrough_page(tmp_path):
    """A losslessly passed-through page (vector / colour line art) is NOT re-rendered, so
    its own /Rotate is real and must be preserved — the fix must not blanket-zero it."""
    src = tmp_path / 'src.pdf'
    comp = tmp_path / 'comp.pdf'
    _make_pdf(src, w=842, h=595, rotate=90)
    # Pass-through: the compressed page IS the original page, /Rotate and all.
    _make_pdf(comp, w=842, h=595, rotate=90)

    assert owm._graft_into_source(src, comp) is not False

    with pikepdf.open(str(comp)) as out:
        for pg in out.pages:
            assert int(pg.obj.get('/Rotate', 0)) == 90, (
                'a passed-through page lost its genuine /Rotate'
            )
