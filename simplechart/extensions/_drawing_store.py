from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Generic, TypeVar

from simplechart.extensions._base import (
    ChartExtensionMutation,
    ChartExtensionStoreContext,
)


class AxisPolicy(Enum):
    """
    How a drawing tool treats one persistence axis.

    FIXED_ON   — always persists on that axis; no user toggle.
    FIXED_OFF  — never persists on that axis; no user toggle.
    USER       — the per-drawing choice decides; the tool surfaces a toggle in
                 its Configure dialog and the choice rides in the record.
    """

    FIXED_ON = "fixed_on"
    FIXED_OFF = "fixed_off"
    USER = "user"


R = TypeVar("R")


class DrawingStore(ABC, Generic[R]):
    """
    Generic persistence handler for a drawing tool's records.

    A tool subclasses this, sets the class-level configuration, and implements
    the record hooks. The base honors the tool's persistence policy on two
    independent axes:

      - timeframe: whether a drawing shows on every timeframe (no tag) or only
        the timeframe it was created on (tagged).
      - session: whether a drawing survives an app restart (durable, stored via
        the context's SQLite-backed records) or lives only for the current run
        (volatile, kept in process memory).

    Each axis is FIXED_ON, FIXED_OFF, or USER (see AxisPolicy). For a USER axis,
    the base consults the per-drawing hook (wants_timeframe_persistence /
    wants_session_persistence) and the tool surfaces that choice as a toggle.

    Records flow through the standard mutation vocabulary:
        operation "add"     payload {"record": R}  (no id yet)
        operation "update"  payload {"record": R}  (id set)
        operation "delete"  payload {"record": R}  (id set)

    The base assigns ids: durable records receive the positive SQLite row id;
    volatile records receive a negative id so the two backings never collide and
    a record's backing can be told from the sign of its id.
    """

    extension_name: str
    store_key: str
    params_key: str
    timeframe_axis: AxisPolicy
    session_axis: AxisPolicy

    def __init__(self, context: ChartExtensionStoreContext) -> None:
        self._context = context
        self._active_symbol: str | None = None
        # Durable records for the active symbol, reloaded on each load_for_symbol.
        self._durable: list[R] = []
        # Volatile records per symbol, kept for the lifetime of the process.
        self._volatile: dict[str, list[R]] = {}
        self._next_volatile_id = -1

    # ------------------------------------------------------------------
    # Hooks the tool implements
    # ------------------------------------------------------------------

    @abstractmethod
    def to_payload(self, record: R) -> dict[str, Any]: ...

    @abstractmethod
    def from_payload(
        self,
        record_id: int,
        symbol: str,
        sort_key: int,
        payload: dict[str, Any],
    ) -> R: ...

    @abstractmethod
    def sort_key(self, record: R) -> int: ...

    @abstractmethod
    def record_id(self, record: R) -> int | None: ...

    @abstractmethod
    def with_id(self, record: R, record_id: int) -> R: ...

    @abstractmethod
    def series_key(self, record: R) -> str: ...

    @abstractmethod
    def created_timeframe(self, record: R) -> str: ...

    def wants_timeframe_persistence(self, record: R) -> bool:
        """Per-drawing choice for the timeframe axis; consulted only when USER."""
        return True

    def wants_session_persistence(self, record: R) -> bool:
        """Per-drawing choice for the session axis; consulted only when USER."""
        return True

    # ------------------------------------------------------------------
    # Store handler interface
    # ------------------------------------------------------------------

    def load_for_symbol(self, symbol: str) -> None:
        self._active_symbol = symbol
        self._durable = [
            self.from_payload(record.record_id, record.symbol, record.sort_key, record.payload)
            for record in self._context.get_indicator_records(self.store_key, symbol)
        ]

    def params_for(
        self,
        extension_name: str,
        base_params: dict[str, Any],
    ) -> dict[str, Any]:
        if extension_name != self.extension_name:
            return base_params
        params = dict(base_params)
        params[self.params_key] = self._active_records()
        return params

    def apply(self, mutation: ChartExtensionMutation) -> None:
        if mutation.extension_name != self.extension_name:
            return
        if mutation.operation == "add":
            self._add(mutation.payload["record"])
            self._reconcile_state()
        elif mutation.operation == "update":
            self._update(mutation.payload["record"])
        elif mutation.operation == "delete":
            self._delete(mutation.payload["record"])

    def prepare_active_indicators(self) -> None:
        self._reconcile_state()

    # ------------------------------------------------------------------
    # Policy resolution
    # ------------------------------------------------------------------

    def _is_durable(self, record: R) -> bool:
        if self.session_axis == AxisPolicy.FIXED_ON:
            return True
        if self.session_axis == AxisPolicy.FIXED_OFF:
            return False
        return self.wants_session_persistence(record)

    def _timeframe_tag(self, record: R) -> str | None:
        # None means "show on every timeframe"; a value pins the record to the
        # timeframe it was created on.
        if self.timeframe_axis == AxisPolicy.FIXED_ON:
            return None
        if self.timeframe_axis == AxisPolicy.FIXED_OFF:
            return self.created_timeframe(record)
        if self.wants_timeframe_persistence(record):
            return None
        return self.created_timeframe(record)

    def _visible_on_current_timeframe(self, record: R) -> bool:
        tag = self._timeframe_tag(record)
        return tag is None or tag == self._context.current_timeframe()

    # ------------------------------------------------------------------
    # Record operations
    # ------------------------------------------------------------------

    def _active_records(self) -> list[R]:
        if self._active_symbol is None:
            return []
        records = self._durable + self._volatile.get(self._active_symbol, [])
        return [record for record in records if self._visible_on_current_timeframe(record)]

    def _add(self, record: R) -> None:
        symbol = self._context.current_symbol()
        if symbol is None:
            return
        if self._is_durable(record):
            persisted = self._context.put_indicator_record(
                self.store_key,
                symbol,
                self.sort_key(record),
                self.to_payload(record),
            )
            self._durable.append(
                self.from_payload(persisted.record_id, persisted.symbol, persisted.sort_key, persisted.payload)
            )
        else:
            stored = self.with_id(record, self._next_volatile_id)
            self._next_volatile_id -= 1
            self._volatile.setdefault(symbol, []).append(stored)

    def _update(self, record: R) -> None:
        record_id = self.record_id(record)
        if record_id is None:
            return
        was_durable = record_id >= 0
        now_durable = self._is_durable(record)
        if was_durable != now_durable:
            # A USER session toggle flipped the backing: remove from the old
            # backing and re-add to the new one. _add ignores the incoming id
            # and assigns a fresh one for the new backing.
            self._delete(record)
            self._add(record)
            self._reconcile_state()
            return
        if now_durable:
            self._context.update_indicator_record(record_id, self.sort_key(record), self.to_payload(record))
            self._durable = [record if self.record_id(c) == record_id else c for c in self._durable]
        elif self._active_symbol is not None:
            lines = self._volatile.get(self._active_symbol, [])
            self._volatile[self._active_symbol] = [
                record if self.record_id(c) == record_id else c for c in lines
            ]

    def _delete(self, record: R) -> None:
        record_id = self.record_id(record)
        if record_id is None:
            return
        if record_id >= 0:
            self._context.delete_indicator_record(record_id)
            self._durable = [c for c in self._durable if self.record_id(c) != record_id]
        elif self._active_symbol is not None:
            lines = self._volatile.get(self._active_symbol, [])
            self._volatile[self._active_symbol] = [c for c in lines if self.record_id(c) != record_id]
        self._context.remove_series_visibility(self.extension_name, self.series_key(record))
        self._reconcile_state()

    def _reconcile_state(self) -> None:
        if self._active_records():
            self._context.ensure_indicator_state(self.extension_name, {self.params_key: []})
        else:
            self._context.remove_indicator_state(self.extension_name)
