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

from simplechart.extensions._base import ChartExtension


# Internal registry: name -> class (not instance).
# Instances are created on demand by get_extension() so each call gets a fresh
# object.
_registry: dict[str, type[ChartExtension]] = {}


def register_extension(cls: type[ChartExtension]) -> None:
    """Register a ChartExtension class."""
    instance = cls()
    _registry[instance.name()] = cls


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
    return _registry[name]()


def all_extensions() -> dict[str, type[ChartExtension]]:
    """Return a copy of the full registry (name -> class)."""
    return dict(_registry)
