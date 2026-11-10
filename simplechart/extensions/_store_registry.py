from collections.abc import Callable

from simplechart.extensions._base import (
    ChartExtensionStoreContext,
    ChartExtensionStoreHandler,
)
from simplechart.extensions._registry import registering_module


type StoreHandlerFactory = Callable[[ChartExtensionStoreContext], ChartExtensionStoreHandler]

# (factory, owner module) in registration order. A failing handler is
# attributed to the module that registered its factory.
_registry: list[tuple[StoreHandlerFactory, str]] = []


def register_store_handler(factory: StoreHandlerFactory) -> None:
    if factory not in [registered for registered, _owner in _registry]:
        _registry.append((factory, registering_module()))


def all_store_handlers() -> list[tuple[StoreHandlerFactory, str]]:
    return list(_registry)


def store_handler_owner(factory: object) -> str | None:
    for registered, owner in _registry:
        if registered is factory:
            return owner
    return None


def snapshot() -> list[tuple[StoreHandlerFactory, str]]:
    return list(_registry)


def restore(saved: list[tuple[StoreHandlerFactory, str]]) -> None:
    # In place: every importer holds this same list.
    _registry[:] = saved
