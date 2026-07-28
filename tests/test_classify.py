"""Page-type DETECTION tests.

For every fixture PDF under tests/fixtures/<type>/, render its page and run the
production classify_page(). The page must be routed to the PageType named by its
folder. These fixtures are real scanned pages (see extract_fixtures.py), so a
failure here is a genuine misclassification, not a synthetic artefact.

If there are no fixtures yet the whole module is skipped with instructions.
"""
import shutil

import pytest

import _util as U
from _util import TYPE_DIRS

_missing = U.tools_missing()
pytestmark = pytest.mark.skipif(_missing is not None, reason=str(_missing))

DPI = 200


def _all_fixtures():
    """(page_type, expected_PageType, pdf_path) for every extracted fixture."""
    cases = []
    for folder, expected in TYPE_DIRS.items():
        for pdf in U.fixture_pdfs(folder):
            cases.append(pytest.param(folder, expected, pdf, id=f'{folder}/{pdf.name}'))
    return cases


_FIXTURES = _all_fixtures()

if not _FIXTURES:
    pytest.skip(
        'No fixtures yet. Build them from real scans:\n'
        '  python tests/extract_fixtures.py "<path to scanned PDFs>"\n'
        'then confirm each label and set "verified": true in the manifest.',
        allow_module_level=True)


@pytest.mark.parametrize('page_type,expected,pdf', _FIXTURES)
def test_page_is_classified_correctly(page_type, expected, pdf):
    work = U.workdir()
    try:
        got, signals = U.classify(pdf, 1, DPI, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    assert got == expected, (
        f'{pdf.name}: expected {expected!r} but classify_page said {got!r}. '
        f'signals={signals}')


def test_every_type_has_fixtures():
    """Guard against a corpus that silently lost coverage for a whole type."""
    have = {t for t in TYPE_DIRS if U.fixture_pdfs(t)}
    missing = set(TYPE_DIRS) - have
    assert not missing, f'no fixtures for page type(s): {sorted(missing)}'


def test_manifest_fixtures_are_verified():
    """Every fixture should be human-confirmed. Unverified ones are auto-picked
    candidates whose label the classifier chose — assert-them-against-themselves
    is circular, so surface them until a human signs off."""
    unverified = [e['file'] for e in U.load_manifest() if not e.get('verified')]
    assert not unverified, (
        'unverified (auto-picked) fixtures — open each, confirm the label, then set '
        f'"verified": true in {U.MANIFEST.name}:\n  ' + '\n  '.join(unverified))


# ── native scan resolution on striped pages ──────────────────────────────────────
# `_largest_image_dpi` divides the largest image's pixel count by the whole PAGE area,
# which is only valid when that image covers the page. A scan stored as N full-width
# strips breaks the assumption and under-reads by sqrt(N): measured 73 dpi on a real
# 600 dpi page held in 68 strips, which suppressed the native-resolution render, reduced
# the page 3x by point-sampling, and broke its hairlines into 2,455 fragments.

def test_striped_scan_reports_its_true_resolution(tmp_path):
    from pypdf import PdfReader
    pdf = U.make_striped_scan_pdf(tmp_path / 'striped.pdf', dpi=300, strips=10)
    page = PdfReader(str(pdf)).pages[0]
    dpi = U.owm._largest_image_dpi(page)
    assert 290 <= dpi <= 310, f'striped 300 dpi page read as {dpi:.0f} dpi'
    # It must clear the vector floor by a wide margin: the real case read 73 dpi, only 23
    # above the floor, i.e. one shorter strip away from a 600 dpi scan being called
    # born-digital and passed through unprocessed.
    assert dpi > U.owm.VECTOR_DPI_FLOOR * 2
    # The render dpi deliberately does NOT follow it — that measured 2x bytes and 2x runtime,
    # while interpolation fixes the hairlines for 7.7% less.
    assert U.owm._largest_image_dpi(page, include_strips=False) < 200
    assert U.owm._page_render_dpi(page, 200) == 200
    # ...and assert it through the function the RENDER LOOP calls. Testing only the singular
    # helper above passed green while the real path still rendered the page at 400 dpi: the
    # loop uses the plural `_page_render_dpis`, and the singular one serves the photo
    # re-render alone. Verified against the actual scratch PNGs, not just this call.
    assert U.owm._page_render_dpis(pdf, 200) == [200]


def test_a_wide_figure_is_not_mistaken_for_a_strip(tmp_path):
    """The width reading must apply ONLY to strips. Used on every image it over-reads any
    image that is wide in pixels but not placed across the page (measured: a Subaru page
    went 341 -> 525 dpi), and over-reading pushes a page above VECTOR_DPI_FLOOR toward the
    raster path — the unsafe direction."""
    from pypdf import PdfReader
    # one image, 2.55:1 — wide, but under the strip gate
    pdf = U.make_striped_scan_pdf(tmp_path / 'figure.pdf', dpi=300, strips=1)
    page = PdfReader(str(pdf)).pages[0]
    full = U.owm._largest_image_dpi(page)          # strips=1 IS the page -> ~300 either way
    assert 290 <= full <= 310, full
    # a short-but-not-strip image: aspect below the gate keeps the area reading
    assert U.owm._STRIP_ASPECT >= 4.0, 'gate must stay conservative'


def test_render_uses_image_interpolation():
    """Ghostscript point-samples when it scales a raster down unless told otherwise, so a
    hairline survives a reduction only if it lands on the sample grid. With interpolation the
    same page went from 2,455 to 1,425 connected components and the JBIG2 got 7.7% smaller."""
    import inspect
    src = inspect.getsource(U.owm)
    assert U.owm._GS_INTERPOLATE == '-dDOINTERPOLATE'
    # every page-render call must carry it (the OSD probe deliberately does not)
    renders = [ln for ln in src.splitlines() if '-sDEVICE=png' in ln]
    assert renders, 'no render call sites found'
    for ln in renders:
        i = src.splitlines().index(ln)
        window = '\n'.join(src.splitlines()[i:i + 5])
        if "'-r200'" in ln and 'pnggray' in ln:
            continue                                # the OSD script probe
        assert '_GS_INTERPOLATE' in window, f'render without interpolation: {ln.strip()[:70]}'
