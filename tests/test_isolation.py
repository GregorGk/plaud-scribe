"""Guard: the suite must never touch the operator's real data directories."""

import importlib
from pathlib import Path

from conftest import _PATHS

REAL_ROOTS = [
    Path.home() / ".config" / "plaud-scribe",
    Path.home() / ".local" / "share" / "plaud-scribe",
    Path.home() / ".local" / "state" / "plaud-scribe",
]


def test_every_bound_path_is_redirected():
    for module_name, names in _PATHS.items():
        module = importlib.import_module(module_name)
        for name in names:
            path = Path(getattr(module, name)).resolve()
            for real in REAL_ROOTS:
                assert not path.is_relative_to(real.resolve()), (
                    f"{module_name}.{name} still points at the real {real}"
                )


def test_no_module_binds_a_path_the_fixture_misses():
    """Catch a new `from .config import SOME_DIR` before it leaks."""
    config = importlib.import_module("plaud_scribe.config")
    path_names = {n for n in dir(config) if n.isupper() and isinstance(getattr(config, n), Path)}
    for module_name in ("plaud_scribe.sync", "plaud_scribe.summary", "plaud_scribe.cli",
                        "plaud_scribe.drive", "plaud_scribe.speakers", "plaud_scribe.plaud"):
        module = importlib.import_module(module_name)
        bound = {n for n in path_names if hasattr(module, n)}
        covered = set(_PATHS.get(module_name, ()))
        assert bound <= covered, f"{module_name} binds {sorted(bound - covered)} unpatched"
