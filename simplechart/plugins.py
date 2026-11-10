import functools
import importlib
import importlib.util
import logging
import pkgutil
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType

from data.errors import describe
from simplechart.extensions import _registry, _store_registry
from simplechart.extensions._guard import queue_notice


_DEFAULT_PLUGIN_PACKAGES: tuple[str, ...] = ("indicators", "tools")
_USER_PLUGIN_DIR = Path.home() / ".simplechart" / "plugins"
_USER_PLUGIN_NAMESPACE = "simplechart_user_plugins"

_LOGGER = logging.getLogger(__name__)


def load_plugins(
    package_names: Sequence[str] = _DEFAULT_PLUGIN_PACKAGES,
    user_plugin_dir: Path | None = _USER_PLUGIN_DIR,
) -> None:
    for package_name in package_names:
        load_plugin_package(package_name)
    if user_plugin_dir is not None:
        load_plugin_directory(user_plugin_dir)


def load_plugin_package(package_name: str) -> None:
    package = _load_isolated(
        package_name,
        package_name,
        lambda: importlib.import_module(package_name),
    )
    if package is None:
        return
    package_paths = getattr(package, "__path__", None)
    if package_paths is None:
        return
    for module_info in pkgutil.iter_modules(package_paths, f"{package_name}."):
        if module_info.name.rsplit(".", 1)[-1].startswith("_"):
            continue
        _load_isolated(
            module_info.name,
            package_name,
            functools.partial(importlib.import_module, module_info.name),
        )


def load_plugin_directory(directory: Path) -> None:
    if not directory.is_dir():
        return
    for path in sorted(directory.iterdir()):
        if path.name.startswith("_") or path.suffix != ".py":
            continue
        _load_plugin_file(path)


def _load_plugin_file(path: Path) -> ModuleType | None:
    module_name = f"{_USER_PLUGIN_NAMESPACE}.{path.stem}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    return _load_isolated(
        path.name,
        _USER_PLUGIN_NAMESPACE,
        lambda: _execute_plugin_file(module_name, path),
    )


def _execute_plugin_file(module_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin module from {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_isolated(
    display_name: str,
    namespace: str,
    load: Callable[[], ModuleType],
) -> ModuleType | None:
    # A plugin that raises while loading is rolled back as a unit: both
    # registries are restored in place (overwritten entries included), and
    # every module this load added under the plugin namespace, or that
    # registered something, leaves sys.modules so a retry runs it afresh
    # instead of returning a partial module. Third-party modules it imported
    # stay loaded.
    modules_before = set(sys.modules)
    extensions_before = _registry.snapshot()
    stores_before = _store_registry.snapshot()
    try:
        return load()
    except Exception as exc:
        owners = set(_registry.snapshot().owners.values())
        owners.update(owner for _factory, owner in _store_registry.snapshot())
        _registry.restore(extensions_before)
        _store_registry.restore(stores_before)
        for name in set(sys.modules) - modules_before:
            if name == namespace or name.startswith(f"{namespace}.") or name in owners:
                _forget_module(name)
        _LOGGER.error("Plugin %r could not be loaded: %s", display_name, describe(exc))
        queue_notice(f"Plugin '{display_name}' could not be loaded and was skipped.")
        return None


def _forget_module(name: str) -> None:
    # Also detach it from a surviving parent package: `from . import child`
    # reads the parent's attribute first and would get the rolled-back module
    # without running it again.
    module = sys.modules.pop(name)
    parent_name, _, child = name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if parent is not None and getattr(parent, child, None) is module:
        delattr(parent, child)
