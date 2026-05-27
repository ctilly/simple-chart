from bisect import bisect_left
from typing import Any

import numpy as np

from tools.vertical_line.models import VerticalLineRecord
from tools.vertical_line.session_store import (
    VerticalLineStore,
    vertical_line_key,
)
from simplechart.api import (
    ChartEvent,
    ChoiceParam,
    DragSession,
    DrawingToolResult,
    HitTestResult,
    ChartExtension,
    ChartExtensionAddMode,
    ChartExtensionConfig,
    ChartExtensionMutation,
    ChartExtensionRender,
    LINE_STYLE_OPTIONS,
    OHLCVSeries,
    VerticalLineRender,
    register_extension,
    register_store_handler,
)

_DEFAULT_COLOR = "#7a7f8c"
_HIT_BUFFER = 0.55


class VerticalLineIndicator(ChartExtension):

    def name(self) -> str:
        return "vertical_line"

    def label(self) -> str:
        return "Vertical Line"

    def default_params(self) -> dict[str, Any]:
        return {
            "lines": [],
            "color": _DEFAULT_COLOR,
            "line_width": 1.0,
            "line_style": ChoiceParam("solid", LINE_STYLE_OPTIONS),
        }

    def add_mode(self) -> ChartExtensionAddMode:
        return ChartExtensionAddMode.TOOLBAR

    def preserve_ui_state_per_symbol(self) -> bool:
        return False

    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        return {}

    def render(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> ChartExtensionRender:
        render = ChartExtensionRender()
        for line in _lines_for_symbol(params, series.symbol):
            x_index = _timestamp_to_nearest_index(line.timestamp_ms, series)
            if x_index is None:
                continue
            render.vertical_lines.append(
                VerticalLineRender(
                    key=vertical_line_key(line),
                    x_index=float(x_index),
                    label="Vertical Line",
                    color=line.color,
                    line_width=line.line_width,
                    line_style=line.line_style,
                )
            )
        return render

    def start_drawing(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        event: ChartEvent,
    ) -> DrawingToolResult:
        if event.timestamp_ms is None:
            return DrawingToolResult()
        record = VerticalLineRecord(
            symbol=series.symbol,
            timestamp_ms=event.timestamp_ms,
            timeframe=series.timeframe.value,
            color=str(params.get("color", _DEFAULT_COLOR)),
            line_width=float(params.get("line_width", 1.0)),
            line_style=_choice_value(params.get("line_style", "solid")),
        )
        return DrawingToolResult(
            mutation=ChartExtensionMutation(
                extension_name=self.name(),
                operation="add",
                payload={"record": record},
            ),
            done=True,
            deactivate_tool=True,
        )

    def hit_test(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        event: ChartEvent,
        visible_keys: set[str],
    ) -> HitTestResult | None:
        for line in _lines_for_symbol(params, series.symbol):
            key = vertical_line_key(line)
            if key not in visible_keys:
                continue
            x_index = _timestamp_to_nearest_index(line.timestamp_ms, series)
            if x_index is None:
                continue
            if abs(event.x - float(x_index)) <= _HIT_BUFFER:
                return HitTestResult(
                    extension_name=self.name(),
                    handle_key=key,
                    cursor="drag",
                    priority=10,
                )
        return None

    def begin_drag(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        hit: HitTestResult,
    ) -> DragSession:
        lines = _lines_for_symbol(params, series.symbol)
        return DragSession(
            extension_name=self.name(),
            handle_key=hit.handle_key,
            original_params={"lines": list(lines)},
            working_params={"lines": list(lines)},
        )

    def drag_to(
        self,
        series: OHLCVSeries,
        session: DragSession,
        event: ChartEvent,
    ) -> ChartExtensionRender | None:
        updated = _updated_drag_line(session, event)
        if updated is None:
            return None
        session.working_params["lines"] = [
            updated if vertical_line_key(line) == session.handle_key else line
            for line in session.working_params["lines"]
        ]
        return self.render(series, {"lines": [updated]})

    def finish_drag(
        self,
        series: OHLCVSeries,
        session: DragSession,
        event: ChartEvent,
    ) -> ChartExtensionMutation | None:
        self.drag_to(series, session, event)
        line = vertical_line_for_key(
            session.working_params["lines"],
            session.handle_key,
        )
        if line is None:
            return None
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="update",
            payload={"record": line},
        )

    def cancel_drag(
        self,
        session: DragSession,
    ) -> ChartExtensionMutation | None:
        # Dragging only produces preview renders; the store is untouched, so a
        # cancel needs no mutation — the controller re-renders from store state.
        return None

    def config_for_series(
        self,
        series_key: str,
        params: dict[str, Any],
    ) -> ChartExtensionConfig | None:
        line = vertical_line_for_key(params.get("lines", []), series_key)
        if line is None:
            return None
        return ChartExtensionConfig(
            label="Vertical Line",
            params={
                "color": line.color,
                "line_width": line.line_width,
                "line_style": ChoiceParam(line.line_style, LINE_STYLE_OPTIONS),
                "persist_across_timeframes": line.persist_across_timeframes,
                "persist_across_sessions": line.persist_across_sessions,
            },
        )

    def apply_config_to_series(
        self,
        series_key: str,
        params: dict[str, Any],
        edited_params: dict[str, Any],
    ) -> ChartExtensionMutation | None:
        line = vertical_line_for_key(params.get("lines", []), series_key)
        if line is None:
            return None
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="update",
            payload={
                "record": VerticalLineRecord(
                    symbol=line.symbol,
                    timestamp_ms=line.timestamp_ms,
                    timeframe=line.timeframe,
                    color=str(edited_params["color"]),
                    line_width=float(edited_params["line_width"]),
                    line_style=_choice_value(edited_params["line_style"]),
                    persist_across_timeframes=bool(edited_params["persist_across_timeframes"]),
                    persist_across_sessions=bool(edited_params["persist_across_sessions"]),
                    line_id=line.line_id,
                )
            },
        )

    def remove_series(
        self,
        series_key: str,
        params: dict[str, Any],
    ) -> ChartExtensionMutation | None:
        line = vertical_line_for_key(params.get("lines", []), series_key)
        if line is None:
            return None
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="delete",
            payload={"record": line},
        )


def vertical_line_for_key(
    lines: list[VerticalLineRecord],
    series_key: str,
) -> VerticalLineRecord | None:
    if series_key.startswith("vertical_line_ts_"):
        timestamp_ms = int(series_key.removeprefix("vertical_line_ts_"))
        return next((line for line in lines if line.timestamp_ms == timestamp_ms), None)
    if series_key.startswith("vertical_line_"):
        line_id = int(series_key.removeprefix("vertical_line_"))
        return next((line for line in lines if line.line_id == line_id), None)
    return None


def _lines_for_symbol(
    params: dict[str, Any],
    symbol: str,
) -> list[VerticalLineRecord]:
    return [
        line for line in params.get("lines", [])
        if line.symbol == symbol
    ]


def _timestamp_to_nearest_index(
    timestamp_ms: int,
    series: OHLCVSeries,
) -> int | None:
    if not series.bars:
        return None
    timestamps = [
        int(bar.timestamp.timestamp() * 1000)
        for bar in series.bars
    ]
    idx = bisect_left(timestamps, timestamp_ms)
    candidates: list[int] = []
    if idx < len(timestamps):
        candidates.append(idx)
    if idx > 0:
        candidates.append(idx - 1)
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: abs(timestamps[candidate] - timestamp_ms))


def _updated_drag_line(
    session: DragSession,
    event: ChartEvent,
) -> VerticalLineRecord | None:
    if event.timestamp_ms is None:
        return None
    line = vertical_line_for_key(
        session.working_params["lines"],
        session.handle_key,
    )
    if line is None:
        return None
    return VerticalLineRecord(
        symbol=line.symbol,
        timestamp_ms=event.timestamp_ms,
        timeframe=line.timeframe,
        color=line.color,
        line_width=line.line_width,
        line_style=line.line_style,
        persist_across_timeframes=line.persist_across_timeframes,
        persist_across_sessions=line.persist_across_sessions,
        line_id=line.line_id,
    )


def _choice_value(value: Any) -> str:
    if isinstance(value, ChoiceParam):
        return value.value
    return str(value)


register_extension(VerticalLineIndicator)
register_store_handler(VerticalLineStore)
