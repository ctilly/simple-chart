from collections.abc import Sequence
from typing import Any, cast

from app.state import IndicatorState
from app.state import State
from data.cache import Cache
from indicators._store_registry import all_store_handlers
from simplechart.api import (
    IndicatorMutation,
    IndicatorStoreContext,
    IndicatorStoreHandler,
    IndicatorStoreRecord,
)


class IndicatorStore:

    def __init__(
        self,
        state: State,
        cache: Cache,
        handlers: Sequence[IndicatorStoreHandler] | None = None,
    ) -> None:
        context = AppIndicatorStoreContext(state, cache)
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

    def apply(self, mutation: IndicatorMutation) -> None:
        for handler in self._handlers:
            handler.apply(mutation)

    def prepare_active_indicators(self) -> None:
        for handler in self._handlers:
            handler.prepare_active_indicators()

    def params_for(
        self,
        indicator_name: str,
        base_params: dict[str, Any],
    ) -> dict[str, Any]:
        params = base_params
        for handler in self._handlers:
            params = handler.params_for(indicator_name, params)
        return params


class AppIndicatorStoreContext:

    def __init__(self, state: State, cache: Cache) -> None:
        self._state = state
        self._cache = cache

    def current_symbol(self) -> str | None:
        return cast(str | None, self._state.symbol)

    def get_indicator_records(
        self,
        store_key: str,
        symbol: str,
    ) -> list[IndicatorStoreRecord]:
        return self._cache.get_indicator_records(store_key, symbol)

    def put_indicator_record(
        self,
        store_key: str,
        symbol: str,
        sort_key: int,
        payload: dict[str, Any],
    ) -> IndicatorStoreRecord:
        return self._cache.put_indicator_record(store_key, symbol, sort_key, payload)

    def update_indicator_record(
        self,
        record_id: int,
        sort_key: int,
        payload: dict[str, Any],
    ) -> None:
        self._cache.update_indicator_record(record_id, sort_key, payload)

    def delete_indicator_record(self, record_id: int) -> None:
        self._cache.delete_indicator_record(record_id)

    def ensure_indicator_state(
        self,
        name: str,
        params: dict[str, Any],
    ) -> None:
        if self._state.get_indicator(name) is None:
            self._state.indicators.append(IndicatorState(name=name, params=params))

    def remove_indicator_state(self, name: str) -> None:
        self._state.indicators = [
            state for state in self._state.indicators
            if state.name != name
        ]

    def remove_series_visibility(
        self,
        indicator_name: str,
        series_key: str,
    ) -> None:
        ind_state = self._state.get_indicator(indicator_name)
        if ind_state is not None:
            ind_state.series_visibility.pop(series_key, None)
