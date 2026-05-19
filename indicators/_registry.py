"""
indicators/_registry.py

Indicator registry — maps indicator names to Indicator classes.

Indicators are registered when their modules are imported at app startup
by the loader. Plugin authors register their indicators the same way.

Usage:

  Registering an indicator plugin:
      from simplechart.api import register_indicator
      register_indicator(MyIndicator)

  Looking up an indicator by name:
      from simplechart.api import get_indicator
      indicator = get_indicator("sma")   # returns an instance of SMAIndicator

  Listing all registered indicators:
      from simplechart.api import all_indicators
      for name, cls in all_indicators().items():
          print(name, cls.label())
"""

from indicators._base import Indicator


# Internal registry: name -> class (not instance).
# Instances are created on demand by get() so each call gets a fresh object.
_registry: dict[str, type[Indicator]] = {}


def register(cls: type[Indicator]) -> None:
    """
    Register an Indicator class."""
    instance = cls()
    _registry[instance.name()] = cls


def register_indicator(cls: type[Indicator]) -> None:
    register(cls)


def get(name: str) -> Indicator:
    """
    Return a new instance of the named indicator.

    Raises KeyError if the name is not registered.
    """
    if name not in _registry:
        available = ", ".join(sorted(_registry))
        raise KeyError(
            f"Unknown indicator {name!r}. Registered: {available}"
        )
    return _registry[name]()


def get_indicator(name: str) -> Indicator:
    return get(name)


def all_indicators() -> dict[str, type[Indicator]]:
    """Return a copy of the full registry (name -> class)."""
    return dict(_registry)
