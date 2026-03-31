"""
indicators/registry.py

Indicator registry — maps indicator names to Indicator classes.

Built-in indicators are registered when their modules are imported at
app startup. Plugin authors register their indicators the same way.

Usage:

  Registering an indicator (in the indicator's own module):
      from indicators.registry import register
      register(MyIndicator)

  Looking up an indicator by name:
      from indicators.registry import get
      indicator = get("sma")   # returns an instance of SMAIndicator

  Listing all registered indicators:
      from indicators.registry import all_indicators
      for name, cls in all_indicators().items():
          print(name, cls.label())
"""

from indicators.base import Indicator


# Internal registry: name -> class (not instance).
# Instances are created on demand by get() so each call gets a fresh object.
_registry: dict[str, type[Indicator]] = {}


def register(cls: type[Indicator]) -> None:
    """
    Register an Indicator class.

    cls must be a concrete subclass of Indicator (i.e. it implements all
    abstract methods). Registering the same name twice overwrites the
    previous entry — plugin authors can use this to replace a built-in.

    Typically called at the bottom of the indicator's module:
        register(SMAIndicator)
    """
    instance = cls()
    _registry[instance.name()] = cls


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


def all_indicators() -> dict[str, type[Indicator]]:
    """Return a copy of the full registry (name -> class)."""
    return dict(_registry)
