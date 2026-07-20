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
