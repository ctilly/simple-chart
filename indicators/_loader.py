"""
indicators/_loader.py

Auto-discovery loader for indicator plugins.

Scans the indicators package directory for .py files and sub-packages,
skipping anything whose name starts with '_'. Each discovered module is
imported via importlib.import_module, which triggers the register() call
at the bottom of each indicator file.

Module naming uses standard package import paths ("indicators.sma",
"indicators.ema", etc.). Python's import machinery prevents double-loading
automatically via sys.modules.
"""

import importlib
from pathlib import Path


def load_indicators(indicators_dir: Path) -> None:
    """
    Discover and import all indicator modules in indicators_dir.

    Files and directories whose names start with '_' are skipped (this
    excludes _base.py, _registry.py, _loader.py, __init__.py, and any
    private helpers). Modules are imported in alphabetical order.
    """
    for path in _indicator_paths(indicators_dir):
        module_name = f"indicators.{path.stem if path.suffix == '.py' else path.name}"
        importlib.import_module(module_name)


def _indicator_paths(directory: Path) -> list[Path]:
    """Return indicator .py files and packages, sorted by name, skipping '_'-prefixed."""
    if not directory.is_dir():
        return []
    results: list[Path] = []
    for p in directory.iterdir():
        if p.name.startswith("_"):
            continue
        if p.suffix == ".py" or (p.is_dir() and (p / "__init__.py").exists()):
            results.append(p)
    return sorted(results)
