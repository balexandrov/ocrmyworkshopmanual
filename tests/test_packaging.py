"""The packaging metadata has to keep up with the code, or an install is broken.

THE BUG. `ocrmyworkshopmanual.py` imports `pdflinks` and `pdfwatermark` at module level,
but `py-modules` in pyproject.toml still listed only the three modules that existed when it
was written. A wheel or an editable install therefore shipped without them, and the console
entry point died on its first line:

    $ ocrmyworkshopmanual --version
    File "ocrmyworkshopmanual.py", line 106, in <module>
        import pdflinks
    ModuleNotFoundError: No module named 'pdflinks'

Every install was broken from the commit that added those modules. Nothing caught it: the
tests all run from the source tree, where the imports resolve by directory, and CI's
compile-check named two files by hand. Only `pip install -e .` followed by running the
entry point sees it, and that step reached CI for the first time several commits later.

So the list is checked against the repo rather than maintained by memory. Adding a module
and forgetting the metadata now fails here, next to the code, instead of in someone's
install.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Modules that are deliberately not shipped: test helpers, one-off scripts.
NOT_SHIPPED = {'conftest'}


def _declared():
    try:
        import tomllib
    except ModuleNotFoundError:                    # 3.10: tomllib arrived in 3.11
        pytest.skip('tomllib needs Python 3.11+')
    with open(ROOT / 'pyproject.toml', 'rb') as fh:
        return set(tomllib.load(fh)['tool']['setuptools']['py-modules'])


def _in_repo():
    return {p.stem for p in ROOT.glob('*.py')} - NOT_SHIPPED


def test_every_top_level_module_is_packaged():
    missing = _in_repo() - _declared()
    assert not missing, (
        f'{sorted(missing)} sit beside the tool but are not in py-modules, so an install '
        f'will not ship them')


def test_py_modules_does_not_name_something_that_is_gone():
    extra = _declared() - _in_repo()
    assert not extra, f'{sorted(extra)} are declared but no longer exist'


def test_the_entry_points_name_modules_that_are_shipped():
    """A console script pointing at an unshipped module fails the same way, at run time."""
    try:
        import tomllib
    except ModuleNotFoundError:
        pytest.skip('tomllib needs Python 3.11+')
    with open(ROOT / 'pyproject.toml', 'rb') as fh:
        scripts = tomllib.load(fh)['project'].get('scripts', {})
    declared = _declared()
    for name, target in scripts.items():
        mod = target.split(':')[0]
        assert mod in declared, f'script {name!r} runs {mod}, which is not packaged'


def test_what_the_tool_imports_at_module_level_is_packaged():
    """The direct check on the failure that happened: whatever `ocrmyworkshopmanual`
    imports from beside itself must be shipped with it."""
    src = (ROOT / 'ocrmyworkshopmanual.py').read_text(encoding='utf-8')
    siblings = _in_repo() - {'ocrmyworkshopmanual'}
    imported = {m for m in siblings
                if f'\nimport {m}\n' in src or f'\nfrom {m} import' in src}
    assert imported, 'expected the tool to import at least one sibling module'
    assert imported <= _declared(), (
        f'{sorted(imported - _declared())} are imported at module level but not packaged')
