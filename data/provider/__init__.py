"""
data/provider/__init__.py

Provider registry. Maps string names to DataProvider implementations so the
active provider can be selected by configuration without importing every
provider unconditionally.

Usage:
    from data.provider import get_provider
    provider = get_provider("yfinance")

Third-party providers can register themselves:
    from data.provider import register_provider
    register_provider("my_provider", MyProvider)
"""

from data.provider.base import DataProvider, UnsupportedTimeframeError
from data.provider.yfinance_provider import YFinanceProvider

_registry: dict[str, type[DataProvider]] = {
    "yfinance": YFinanceProvider,
}


def register_provider(name: str, cls: type[DataProvider]) -> None:
    """Register a DataProvider implementation under the given name."""
    _registry[name] = cls


def get_provider(name: str) -> DataProvider:
    """
    Instantiate and return the named provider.

    Raises KeyError if the name is not registered.
    """
    if name not in _registry:
        available = ", ".join(sorted(_registry))
        raise KeyError(
            f"Unknown provider {name!r}. Available: {available}"
        )
    return _registry[name]()
