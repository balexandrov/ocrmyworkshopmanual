"""pytest bootstrap: make `tests/` (for `import _util`) and the repo root (for
`import ocrmyworkshopmanual`) importable regardless of the invocation directory."""
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
for p in (str(TESTS_DIR), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
