"""
simplechart/extensions/_registry.py

Chart-extension registry — maps extension names to ChartExtension classes.

Extensions (indicators and tools) are registered when their modules are
imported at app startup by the loader. Plugin authors register theirs the
same way.

Usage:

  Registering an extension plugin:
      from simplechart.api import register_extension
      register_extension(MyExtension)

  Looking up an extension by name:
      from simplechart.api import get_extension
      extension = get_extension("sma")   # returns an instance of SMAIndicator

  Listing all registered extensions:
      from simplechart.api import all_extensions
      for name, cls in all_extensions().items():
          print(name, cls.label())
"""

import sys
from dataclasses import dataclass
from types import FrameType

from simplechart.extensions._base import ChartExtension


# Internal registry: name -> class (not instance).
# Instances are created on demand by get_extension() so each call gets a fresh
# object.
_registry: dict[str, type[ChartExtension]] = {}
# Recorded at registration so the host can name and attribute an extension
# without calling plugin code: its label, and the module that owns it.
_labels: dict[str, str] = {}
_owners: dict[str, str] = {}


def register_extension(cls: type[ChartExtension]) -> None:
    """Register a ChartExtension class."""
    instance = cls()
    name = instance.name()
    label = instance.label()
    _registry[name] = cls
    _labels[name] = label
    _owners[name] = registering_module()


def get_extension(name: str) -> ChartExtension:
    """
    Return a new instance of the named extension.

    Raises KeyError if the name is not registered.
    """
    if name not in _registry:
        available = ", ".join(sorted(_registry))
        raise KeyError(
            f"Unknown extension {name!r}. Registered: {available}"
        )
    # A memoized singleton per name was considered (extensions are stateless
    # today, so one shared instance would serve every render/drag/hit-test and
    # avoid repeated construction). We deliberately return a fresh instance per
    # call instead: it guarantees isolation, so any accidental instance state
    # in an extension or plugin can never bleed across drawings, symbols, or
    # timeframes. State safety is worth the negligible allocation cost.
    return _registry[name]()


def all_extensions() -> dict[str, type[ChartExtension]]:
    """Return a copy of the full registry (name -> class)."""
    return dict(_registry)


def extension_label(name: str) -> str:
    return _labels.get(name, name)


def extensions_owned_by(owner: str) -> list[str]:
    return [name for name, module in _owners.items() if module == owner]


def registering_module() -> str:
    # The owner is the module whose module-level code is executing nearest on
    # the stack, so shared helpers and decorators called from a plugin's
    # module body attribute to that plugin. The script run as __main__ is
    # never an owner; a registration outside any import belongs to the
    # module that called the register function.
    caller = sys._getframe(2)
    frame: FrameType | None = caller
    while frame is not None:
        module = frame.f_globals.get("__name__")
        if (
            frame.f_code.co_name == "<module>"
            and isinstance(module, str)
            and module != "__main__"
        ):
            return module
        frame = frame.f_back
    module = caller.f_globals.get("__name__")
    return module if isinstance(module, str) else "<unknown>"


@dataclass(frozen=True)
class RegistrySnapshot:
    registry: dict[str, type[ChartExtension]]
    labels: dict[str, str]
    owners: dict[str, str]


def snapshot() -> RegistrySnapshot:
    return RegistrySnapshot(dict(_registry), dict(_labels), dict(_owners))


def restore(saved: RegistrySnapshot) -> None:
    # In place: every importer holds these same containers.
    _registry.clear()
    _registry.update(saved.registry)
    _labels.clear()
    _labels.update(saved.labels)
    _owners.clear()
    _owners.update(saved.owners)
