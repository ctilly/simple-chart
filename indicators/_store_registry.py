from collections.abc import Callable

from indicators._base import IndicatorStoreContext, IndicatorStoreHandler


StoreHandlerFactory = Callable[[IndicatorStoreContext], IndicatorStoreHandler]

_registry: list[StoreHandlerFactory] = []


def register_store_handler(factory: StoreHandlerFactory) -> None:
    if factory not in _registry:
        _registry.append(factory)


def all_store_handlers() -> list[StoreHandlerFactory]:
    return list(_registry)
