import importlib
import importlib.util
import pkgutil
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType


_DEFAULT_PLUGIN_PACKAGES: tuple[str, ...] = ("indicators",)
_USER_PLUGIN_DIR = Path.home() / ".simplechart" / "plugins"


def load_plugins(
    package_names: Sequence[str] = _DEFAULT_PLUGIN_PACKAGES,
    user_plugin_dir: Path | None = _USER_PLUGIN_DIR,
) -> None:
    for package_name in package_names:
        load_plugin_package(package_name)
    if user_plugin_dir is not None:
        load_plugin_directory(user_plugin_dir)


def load_plugin_package(package_name: str) -> None:
    package = importlib.import_module(package_name)
    package_paths = getattr(package, "__path__", None)
    if package_paths is None:
        return
    for module_info in pkgutil.iter_modules(package_paths, f"{package_name}."):
        if module_info.name.rsplit(".", 1)[-1].startswith("_"):
            continue
        importlib.import_module(module_info.name)


def load_plugin_directory(directory: Path) -> None:
    if not directory.is_dir():
        return
    for path in sorted(directory.iterdir()):
        if path.name.startswith("_") or path.suffix != ".py":
            continue
        _load_plugin_file(path)


def _load_plugin_file(path: Path) -> ModuleType:
    module_name = f"simplechart_user_plugins.{path.stem}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load plugin module from {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
