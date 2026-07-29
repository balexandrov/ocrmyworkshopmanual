"""pytest bootstrap: make `tests/` (for `import _util`) and the repo root (for
`import ocrmyworkshopmanual`) importable regardless of the invocation directory."""
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
for p in (str(TESTS_DIR), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def _no_size_floor(monkeypatch):
    """Disable the MIN_COMPRESS_MB floor for every test by default.

    Fixtures are deliberately tiny (a few pages, well under a MB), so the production
    5 MB floor would route every one of them down the keep-original path and the
    compression machinery — routers, binarization, graft, audit — would go untested
    while the suite still passed. Tests that are ABOUT the floor set it back
    themselves; everything else gets the pre-floor behaviour it was written against."""
    import ocrmyworkshopmanual as owm
    monkeypatch.setattr(owm, 'MIN_COMPRESS_MB', 0.0)
