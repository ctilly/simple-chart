from typing import Any

from tools.vertical_line.models import VerticalLineRecord
from simplechart.api import IndicatorMutation, IndicatorStoreContext

_INDICATOR_NAME = "vertical_line"


class VerticalLineSessionStore:

    def __init__(self, context: IndicatorStoreContext) -> None:
        self._context = context
        self._lines_by_symbol: dict[str, list[VerticalLineRecord]] = {}
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
        params["lines"] = self._active_lines()
        return params

    def apply(self, mutation: IndicatorMutation) -> None:
        if mutation.indicator_name != _INDICATOR_NAME:
            return
        if mutation.operation == "add_line":
            self.add_line(mutation.payload)
            self.prepare_active_indicators()
        elif mutation.operation == "update_line":
            self.update_line(mutation.payload["line"])
        elif mutation.operation == "restore_lines":
            self.restore_lines(mutation.payload["lines"])
        elif mutation.operation == "delete_line":
            self.delete_line(mutation.payload["line"])

    def prepare_active_indicators(self) -> None:
        if self._active_lines():
            self._context.ensure_indicator_state(_INDICATOR_NAME, {"lines": []})

    def add_line(self, payload: dict[str, Any]) -> None:
        symbol = self._context.current_symbol()
        if symbol is None:
            return
        line = VerticalLineRecord(
            symbol=symbol,
            timestamp_ms=int(payload["timestamp_ms"]),
            color=str(payload["color"]),
            line_width=float(payload["line_width"]),
            line_style=str(payload["line_style"]),
            line_id=self._next_id,
        )
        self._next_id += 1
        self._lines_by_symbol.setdefault(symbol, []).append(line)

    def update_line(self, line: VerticalLineRecord) -> None:
        if line.line_id is None:
            return
        lines = self._lines_by_symbol.get(line.symbol, [])
        self._lines_by_symbol[line.symbol] = [
            line if current.line_id == line.line_id else current
            for current in lines
        ]

    def restore_lines(self, lines: list[VerticalLineRecord]) -> None:
        if self._active_symbol is None:
            return
        self._lines_by_symbol[self._active_symbol] = lines

    def delete_line(self, line: VerticalLineRecord) -> None:
        if line.line_id is None:
            return
        lines = self._lines_by_symbol.get(line.symbol, [])
        self._lines_by_symbol[line.symbol] = [
            current for current in lines
            if current.line_id != line.line_id
        ]
        self._context.remove_series_visibility(_INDICATOR_NAME, vertical_line_key(line))
        if not self._active_lines():
            self._context.remove_indicator_state(_INDICATOR_NAME)

    def _active_lines(self) -> list[VerticalLineRecord]:
        if self._active_symbol is None:
            return []
        return list(self._lines_by_symbol.get(self._active_symbol, []))


def vertical_line_key(line: VerticalLineRecord) -> str:
    if line.line_id is not None:
        return f"vertical_line_{line.line_id}"
    return f"vertical_line_ts_{line.timestamp_ms}"
