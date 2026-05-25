from collections.abc import Callable

from simplechart.extensions._base import (
    ChartExtensionStoreContext,
    ChartExtensionStoreHandler,
)


StoreHandlerFactory = Callable[[ChartExtensionStoreContext], ChartExtensionStoreHandler]

_registry: list[StoreHandlerFactory] = []


def register_store_handler(factory: StoreHandlerFactory) -> None:
    if factory not in _registry:
        _registry.append(factory)


def all_store_handlers() -> list[StoreHandlerFactory]:
    return list(_registry)
