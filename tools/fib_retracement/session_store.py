from typing import Any

from tools.fib_retracement.models import FibRetracementRecord
from simplechart.api import IndicatorMutation, IndicatorStoreContext

_INDICATOR_NAME = "fib_retracement"


class FibRetracementSessionStore:

    def __init__(self, context: IndicatorStoreContext) -> None:
        self._context = context
        self._records_by_symbol: dict[str, list[FibRetracementRecord]] = {}
        self._active_symbol: str | None = None
        self._next_id = 1

    def load_for_symbol(self, symbol: str) -> None:
        self._active_symbol = symbol

    def params_for(
        self,
        indicator_name: str,
        base_params: dict[str, Any],
    ) -> dict[str, Any]:
        if indicator_name != _INDICATOR_NAME:
            return base_params
        params = dict(base_params)
        params["drawings"] = self._active_records()
        return params

    def apply(self, mutation: IndicatorMutation) -> None:
        if mutation.indicator_name != _INDICATOR_NAME:
            return
        if mutation.operation == "add_drawing":
            self.add_drawing(mutation.payload["drawing"])
            self.prepare_active_indicators()
        elif mutation.operation == "update_drawing":
            self.update_drawing(mutation.payload["drawing"])
        elif mutation.operation == "restore_drawings":
            self.restore_drawings(mutation.payload["drawings"])
        elif mutation.operation == "delete_drawing":
            self.delete_drawing(mutation.payload["drawing"])

    def prepare_active_indicators(self) -> None:
        if self._active_records():
            self._context.ensure_indicator_state(_INDICATOR_NAME, {"drawings": []})

    def add_drawing(self, drawing: FibRetracementRecord) -> None:
        record = FibRetracementRecord(
            symbol=drawing.symbol,
            timeframe=drawing.timeframe,
            start_timestamp_ms=drawing.start_timestamp_ms,
            end_timestamp_ms=drawing.end_timestamp_ms,
            direction=drawing.direction,
            anchor_price_mode=drawing.anchor_price_mode,
            color=drawing.color,
            line_width=drawing.line_width,
            line_style=drawing.line_style,
            show_price_labels=drawing.show_price_labels,
            label_position=drawing.label_position,
            show_anchor_handles=drawing.show_anchor_handles,
            visible_levels=drawing.visible_levels,
            drawing_id=self._next_id,
        )
        self._next_id += 1
        self._records_by_symbol.setdefault(record.symbol, []).append(record)

    def update_drawing(self, drawing: FibRetracementRecord) -> None:
        if drawing.drawing_id is None:
            return
        drawings = self._records_by_symbol.get(drawing.symbol, [])
        self._records_by_symbol[drawing.symbol] = [
            drawing if current.drawing_id == drawing.drawing_id else current
            for current in drawings
        ]

    def restore_drawings(self, drawings: list[FibRetracementRecord]) -> None:
        if self._active_symbol is None:
            return
        self._records_by_symbol[self._active_symbol] = drawings

    def delete_drawing(self, drawing: FibRetracementRecord) -> None:
        if drawing.drawing_id is None:
            return
        drawings = self._records_by_symbol.get(drawing.symbol, [])
        self._records_by_symbol[drawing.symbol] = [
            current for current in drawings
            if current.drawing_id != drawing.drawing_id
        ]
        self._context.remove_series_visibility(_INDICATOR_NAME, fib_drawing_key(drawing))
        if not self._active_records():
            self._context.remove_indicator_state(_INDICATOR_NAME)

    def _active_records(self) -> list[FibRetracementRecord]:
        if self._active_symbol is None:
            return []
        return list(self._records_by_symbol.get(self._active_symbol, []))


def fib_drawing_key(drawing: FibRetracementRecord) -> str:
    if drawing.drawing_id is not None:
        return f"fib_retracement_ref_{drawing.drawing_id}"
    return f"fib_retracement_ref_ts_{drawing.start_timestamp_ms}_{drawing.end_timestamp_ms}"
