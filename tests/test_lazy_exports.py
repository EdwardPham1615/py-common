"""The three packages whose ``__init__`` resolves re-exports on first access.

`runtime`, `http` and `persistence` re-export lazily so that importing one of
their submodules does not charge the caller for the whole package's dependency
set — the bug that made four extras unimportable in 0.2.0 and `[migrations]`
unimportable after it. Nothing tested the mechanism itself until now: the 0.2.1
fix was verified only through `scripts/check-extras-isolation.sh`, which needs a
throwaway environment per extra and therefore cannot run in this suite.

These tests are cheap and cover the contract the mechanism has to keep.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest

LAZY_PACKAGES = ["py_common.runtime", "py_common.http", "py_common.persistence"]

# package -> a submodule that costs real dependencies, which importing the
# package must therefore *not* pull in.
HEAVY_SUBMODULE = {
    "py_common.runtime": "py_common.runtime.app",
    "py_common.http": "py_common.http.client",
    "py_common.persistence": "py_common.persistence.engine",
}


@pytest.mark.parametrize("package", LAZY_PACKAGES)
def test_every_exported_name_resolves(package: str) -> None:
    """``__all__`` is a promise; a typo in the lazy map would break it silently."""
    module = importlib.import_module(package)

    for name in module.__all__:
        assert getattr(module, name) is not None


@pytest.mark.parametrize("package", LAZY_PACKAGES)
def test_an_unknown_name_raises_attribute_error(package: str) -> None:
    """``__getattr__`` must not swallow a genuine typo into an import error."""
    module = importlib.import_module(package)

    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        _ = module.nope  # type: ignore[attr-defined]


@pytest.mark.parametrize("package", LAZY_PACKAGES)
def test_dir_matches_all(package: str) -> None:
    """Otherwise tab-completion and `dir()` disagree with the documented surface."""
    module = importlib.import_module(package)

    assert dir(module) == sorted(module.__all__)


@pytest.mark.parametrize("package", LAZY_PACKAGES)
def test_importing_the_package_does_not_import_its_heavy_submodule(package: str) -> None:
    """The property the whole mechanism exists for, asserted directly.

    In a subprocess because this suite has already imported everything: a check
    against the current ``sys.modules`` would pass or fail on test ordering rather
    than on the package's behaviour.
    """
    submodule = HEAVY_SUBMODULE[package]
    code = (
        f"import {package}, sys; "
        f"assert {submodule!r} not in sys.modules, {submodule!r} + ' was imported eagerly'"
    )

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
