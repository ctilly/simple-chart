from collections.abc import Sequence
from typing import Any

from app.state import ChartExtensionState
from app.state import State
from data.cache import Cache
from simplechart.extensions._store_registry import all_store_handlers
from simplechart.api import (
    ChartExtensionMutation,
    ChartExtensionStoreHandler,
    ChartExtensionStoreRecord,
)


class ChartExtensionStore:

    def __init__(
        self,
        state: State,
        cache: Cache,
        handlers: Sequence[ChartExtensionStoreHandler] | None = None,
    ) -> None:
        context = AppChartExtensionStoreContext(state, cache)
        if handlers is not None:
            self._handlers = list(handlers)
        else:
            self._handlers = [
                factory(context)
                for factory in all_store_handlers()
            ]

    def load_for_symbol(self, symbol: str) -> None:
        for handler in self._handlers:
            handler.load_for_symbol(symbol)

    def apply(self, mutation: ChartExtensionMutation) -> None:
        for handler in self._handlers:
            handler.apply(mutation)

    def prepare_active_extensions(self) -> None:
        for handler in self._handlers:
            handler.prepare_active_extensions()

    def params_for(
        self,
        extension_name: str,
        base_params: dict[str, Any],
    ) -> dict[str, Any]:
        params = base_params
        for handler in self._handlers:
            params = handler.params_for(extension_name, params)
        return params


class AppChartExtensionStoreContext:

    def __init__(self, state: State, cache: Cache) -> None:
        self._state = state
        self._cache = cache

    def current_symbol(self) -> str | None:
        return self._state.symbol

    def current_timeframe(self) -> str | None:
        return self._state.timeframe.value

    def get_extension_records(
        self,
        store_key: str,
        symbol: str,
    ) -> list[ChartExtensionStoreRecord]:
        return self._cache.get_extension_records(store_key, symbol)

    def put_extension_record(
        self,
        store_key: str,
        symbol: str,
        sort_key: int,
        payload: dict[str, Any],
    ) -> ChartExtensionStoreRecord:
        return self._cache.put_extension_record(store_key, symbol, sort_key, payload)

    def update_extension_record(
        self,
        record_id: int,
        sort_key: int,
        payload: dict[str, Any],
    ) -> None:
        self._cache.update_extension_record(record_id, sort_key, payload)

    def delete_extension_record(self, record_id: int) -> None:
        self._cache.delete_extension_record(record_id)

    def ensure_extension_state(
        self,
        name: str,
        params: dict[str, Any],
    ) -> None:
        if self._state.get_extension(name) is None:
            self._state.extensions.append(ChartExtensionState(name=name, params=params))

    def remove_extension_state(self, name: str) -> None:
        self._state.extensions = [
            state for state in self._state.extensions
            if state.name != name
        ]

    def remove_series_visibility(
        self,
        extension_name: str,
        series_key: str,
    ) -> None:
        ind_state = self._state.get_extension(extension_name)
        if ind_state is not None:
            ind_state.series_visibility.pop(series_key, None)
