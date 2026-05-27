from bisect import bisect_left
from typing import Any

import numpy as np

from tools.fib_retracement.models import FIB_LEVELS, FibRetracementRecord
from tools.fib_retracement.session_store import (
    FibRetracementStore,
    fib_drawing_key,
)
from simplechart.api import (
    ChartEvent,
    ChoiceParam,
    DragSession,
    DrawingSession,
    DrawingToolResult,
    HitTestResult,
    HorizontalSegmentRender,
    ChartExtension,
    ChartExtensionAddMode,
    ChartExtensionConfig,
    ChartExtensionMutation,
    ChartExtensionRender,
    LINE_STYLE_OPTIONS,
    MarkerRender,
    OHLCVSeries,
    register_extension,
    register_store_handler,
)

_ANCHOR_MODE_OPTIONS: list[str] = ["swing_wick", "close"]
_LABEL_POSITION_OPTIONS: list[str] = ["right", "left"]
_DEFAULT_COLOR = "#4f7cff"
_HIT_X_BUFFER = 0.65
_HANDLE_HIT_Y_FRACTION = 0.004


class FibonacciRetracementIndicator(ChartExtension):

    def name(self) -> str:
        return "fib_retracement"

    def label(self) -> str:
        return "Fibonacci Retracement"

    def default_params(self) -> dict[str, Any]:
        return {
            "drawings": [],
            "anchor_price_mode": ChoiceParam("swing_wick", _ANCHOR_MODE_OPTIONS),
            "color": _DEFAULT_COLOR,
            "line_width": 1.0,
            "line_style": ChoiceParam("solid", LINE_STYLE_OPTIONS),
            "show_price_labels": False,
            "label_position": ChoiceParam("right", _LABEL_POSITION_OPTIONS),
            "show_anchor_handles": True,
            "show_0_0": True,
            "show_23_6": True,
            "show_38_2": True,
            "show_50_0": True,
            "show_61_8": True,
            "show_100_0": True,
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
        for drawing in _drawings_for_series(params, series):
            layout = _drawing_layout(series, drawing)
            if layout is None:
                continue
            _append_drawing_render(render, drawing, layout, len(series.bars))
        return render

    def start_drawing(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        event: ChartEvent,
    ) -> DrawingToolResult:
        if event.bar_index is None or event.timestamp_ms is None:
            return DrawingToolResult()
        return DrawingToolResult(
            session=DrawingSession(
                extension_name=self.name(),
                tool_key="fib_retracement",
                original_params=dict(params),
                working_params={
                    "start_index": event.bar_index,
                    "start_timestamp_ms": event.timestamp_ms,
                    "params": _drawing_defaults(params),
                },
            )
        )

    def preview_drawing(
        self,
        series: OHLCVSeries,
        session: DrawingSession,
        event: ChartEvent,
    ) -> ChartExtensionRender | None:
        drawing = _session_drawing(series, session, event)
        if drawing is None:
            return ChartExtensionRender()
        render = ChartExtensionRender()
        layout = _live_drawing_layout(series, drawing, event)
        if layout is None:
            return render
        _append_drawing_render(render, drawing, layout, len(series.bars))
        return render

    def advance_drawing(
        self,
        series: OHLCVSeries,
        session: DrawingSession,
        event: ChartEvent,
    ) -> DrawingToolResult:
        drawing = _session_drawing(series, session, event)
        if drawing is None:
            return DrawingToolResult(cancel=True, deactivate_tool=True)
        return DrawingToolResult(
            mutation=ChartExtensionMutation(
                extension_name=self.name(),
                operation="add",
                payload={"record": drawing},
            ),
            done=True,
            deactivate_tool=True,
        )

    def cancel_drawing(
        self,
        session: DrawingSession,
    ) -> ChartExtensionMutation | None:
        return None

    def hit_test(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        event: ChartEvent,
        visible_keys: set[str],
    ) -> HitTestResult | None:
        for drawing in _drawings_for_series(params, series):
            key = fib_drawing_key(drawing)
            hit_key = _visible_drawing_key(key, visible_keys)
            if hit_key is None:
                continue
            layout = _drawing_layout(series, drawing)
            if layout is None:
                continue
            if _end_handle_hit(series, layout, event):
                return HitTestResult(self.name(), hit_key, cursor="drag", priority=20)
            if _level_hit(layout, event):
                return HitTestResult(self.name(), hit_key, cursor=None, priority=10)
        return None

    def begin_drag(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        hit: HitTestResult,
    ) -> DragSession:
        drawings = _drawings_for_series(params, series)
        return DragSession(
            extension_name=self.name(),
            handle_key=hit.handle_key,
            original_params={"drawings": list(drawings)},
            working_params={"drawings": list(drawings)},
        )

    def drag_to(
        self,
        series: OHLCVSeries,
        session: DragSession,
        event: ChartEvent,
    ) -> ChartExtensionRender | None:
        updated = _updated_drag_drawing(series, session, event)
        if updated is None:
            return None
        session.working_params["drawings"] = [
            updated if fib_drawing_key(drawing) == session.handle_key else drawing
            for drawing in session.working_params["drawings"]
        ]
        render = ChartExtensionRender()
        layout = _live_drawing_layout(series, updated, event)
        if layout is not None:
            _append_drawing_render(render, updated, layout, len(series.bars))
        return render

    def finish_drag(
        self,
        series: OHLCVSeries,
        session: DragSession,
        event: ChartEvent,
    ) -> ChartExtensionMutation | None:
        self.drag_to(series, session, event)
        drawing = fib_drawing_for_key(
            session.working_params["drawings"],
            session.handle_key,
        )
        if drawing is None:
            return None
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="update",
            payload={"record": drawing},
        )

    def cancel_drag(self, session: DragSession) -> ChartExtensionMutation | None:
        # Dragging only produces preview renders; the store is untouched, so a
        # cancel needs no mutation — the controller re-renders from store state.
        return None

    def config_for_series(
        self,
        series_key: str,
        params: dict[str, Any],
    ) -> ChartExtensionConfig | None:
        drawing = fib_drawing_for_key(params.get("drawings", []), series_key)
        if drawing is None:
            return None
        return ChartExtensionConfig(
            label="Fibonacci Retracement",
            params={
                "anchor_price_mode": ChoiceParam(drawing.anchor_price_mode, _ANCHOR_MODE_OPTIONS),
                "color": drawing.color,
                "line_width": drawing.line_width,
                "line_style": ChoiceParam(drawing.line_style, LINE_STYLE_OPTIONS),
                "show_price_labels": drawing.show_price_labels,
                "label_position": ChoiceParam(drawing.label_position, _LABEL_POSITION_OPTIONS),
                "show_anchor_handles": drawing.show_anchor_handles,
                "show_0_0": 0.0 in drawing.visible_levels,
                "show_23_6": 0.236 in drawing.visible_levels,
                "show_38_2": 0.382 in drawing.visible_levels,
                "show_50_0": 0.5 in drawing.visible_levels,
                "show_61_8": 0.618 in drawing.visible_levels,
                "show_100_0": 1.0 in drawing.visible_levels,
            },
        )

    def apply_config_to_series(
        self,
        series_key: str,
        params: dict[str, Any],
        edited_params: dict[str, Any],
    ) -> ChartExtensionMutation | None:
        drawing = fib_drawing_for_key(params.get("drawings", []), series_key)
        if drawing is None:
            return None
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="update",
            payload={"record": _configured_drawing(drawing, edited_params)},
        )

    def remove_series(
        self,
        series_key: str,
        params: dict[str, Any],
    ) -> ChartExtensionMutation | None:
        drawing = fib_drawing_for_key(params.get("drawings", []), series_key)
        if drawing is None:
            return None
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="delete",
            payload={"record": drawing},
        )


class _DrawingLayout:

    def __init__(
        self,
        start_index: int,
        end_index: float,
        start_price: float,
        end_price: float,
    ) -> None:
        self.start_index = start_index
        self.end_index = end_index
        self.start_price = start_price
        self.end_price = end_price


def fib_drawing_for_key(
    drawings: list[FibRetracementRecord],
    series_key: str,
) -> FibRetracementRecord | None:
    if series_key.startswith("fib_retracement_ref_ts_"):
        suffix = series_key.removeprefix("fib_retracement_ref_ts_")
        suffix = suffix.split("_ref_", 1)[0]
        start_ts, end_ts = [int(value) for value in suffix.split("_", 1)]
        return next(
            (
                drawing for drawing in drawings
                if drawing.start_timestamp_ms == start_ts
                and drawing.end_timestamp_ms == end_ts
            ),
            None,
        )
    if series_key.startswith("fib_retracement_ref_"):
        suffix = series_key.removeprefix("fib_retracement_ref_")
        drawing_id = int(suffix.split("_ref_", 1)[0])
        return next((drawing for drawing in drawings if drawing.drawing_id == drawing_id), None)
    return None


def _visible_drawing_key(
    drawing_key: str,
    visible_keys: set[str],
) -> str | None:
    if drawing_key in visible_keys:
        return drawing_key
    prefix = f"{drawing_key}_ref_"
    return next((key for key in visible_keys if key.startswith(prefix)), None)


def _drawing_defaults(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "anchor_price_mode": _choice_value(params.get("anchor_price_mode", "swing_wick")),
        "color": str(params.get("color", _DEFAULT_COLOR)),
        "line_width": float(params.get("line_width", 1.0)),
        "line_style": _choice_value(params.get("line_style", "solid")),
        "show_price_labels": bool(params.get("show_price_labels", False)),
        "label_position": _choice_value(params.get("label_position", "right")),
        "show_anchor_handles": bool(params.get("show_anchor_handles", True)),
        "visible_levels": _visible_levels_from_params(params),
    }


def _session_drawing(
    series: OHLCVSeries,
    session: DrawingSession,
    event: ChartEvent,
) -> FibRetracementRecord | None:
    start_index = int(session.working_params["start_index"])
    if event.bar_index is None or event.timestamp_ms is None:
        return None
    if event.bar_index <= start_index:
        return None
    defaults: dict[str, Any] = session.working_params["params"]
    return FibRetracementRecord(
        symbol=series.symbol,
        timeframe=series.timeframe.value,
        start_timestamp_ms=int(session.working_params["start_timestamp_ms"]),
        end_timestamp_ms=event.timestamp_ms,
        direction=_direction_for_event(series, start_index, event),
        anchor_price_mode=str(defaults["anchor_price_mode"]),
        color=str(defaults["color"]),
        line_width=float(defaults["line_width"]),
        line_style=str(defaults["line_style"]),
        show_price_labels=bool(defaults["show_price_labels"]),
        label_position=str(defaults["label_position"]),
        show_anchor_handles=bool(defaults["show_anchor_handles"]),
        visible_levels=defaults["visible_levels"],
    )


def _append_drawing_render(
    render: ChartExtensionRender,
    drawing: FibRetracementRecord,
    layout: _DrawingLayout,
    bar_count: int,
) -> None:
    key = fib_drawing_key(drawing)
    x_end = float(max(layout.end_index, bar_count - 1))
    label_index = _label_index(drawing, layout, bar_count)
    for level in FIB_LEVELS:
        if level not in drawing.visible_levels:
            continue
        level_key = key if level == 0.0 else f"{key}_ref_{_level_key(level)}"
        price = _level_price(layout, level)
        render.segments.append(
            HorizontalSegmentRender(
                key=level_key,
                x_start=float(layout.start_index),
                x_end=x_end,
                y_value=price,
                label="Fibonacci Retracement",
                color=drawing.color,
                line_width=drawing.line_width,
                line_style=drawing.line_style,
                # A fib retracement is managed entirely on-chart (drag handles,
                # right-click configure/remove); no level belongs in the legend.
                reference=True,
            )
        )
        render.markers.append(
            MarkerRender(
                key=f"{level_key}_label",
                x_index=label_index,
                y_value=price,
                text=_level_label(level, price, drawing.show_price_labels),
                color=drawing.color,
                font_size=9,
            )
        )
    if drawing.show_anchor_handles:
        render.markers.append(
            MarkerRender(
                key=f"{key}_start_handle",
                x_index=layout.start_index,
                y_value=layout.start_price,
                text="S",
                color=drawing.color,
                font_size=8,
            )
        )
        render.markers.append(
            MarkerRender(
                key=f"{key}_end_handle",
                x_index=int(round(layout.end_index)),
                y_value=layout.end_price,
                text="E",
                color=drawing.color,
                font_size=8,
            )
        )


def _drawings_for_series(
    params: dict[str, Any],
    series: OHLCVSeries,
) -> list[FibRetracementRecord]:
    return [
        drawing for drawing in params.get("drawings", [])
        if drawing.symbol == series.symbol
        and drawing.timeframe == series.timeframe.value
    ]


def _drawing_layout(
    series: OHLCVSeries,
    drawing: FibRetracementRecord,
) -> _DrawingLayout | None:
    start_index = _timestamp_to_exact_index(drawing.start_timestamp_ms, series)
    end_index = _timestamp_to_exact_index(drawing.end_timestamp_ms, series)
    if start_index is None or end_index is None:
        return None
    start_bar = series.bars[start_index]
    end_bar = series.bars[end_index]
    if drawing.anchor_price_mode == "close":
        return _DrawingLayout(start_index, end_index, float(start_bar.close), float(end_bar.close))
    if drawing.direction == "up":
        return _DrawingLayout(start_index, float(end_index), float(start_bar.low), float(end_bar.high))
    return _DrawingLayout(start_index, float(end_index), float(start_bar.high), float(end_bar.low))


def _live_drawing_layout(
    series: OHLCVSeries,
    drawing: FibRetracementRecord,
    event: ChartEvent,
) -> _DrawingLayout | None:
    start_index = _timestamp_to_exact_index(drawing.start_timestamp_ms, series)
    if start_index is None:
        return None
    start_bar = series.bars[start_index]
    if drawing.anchor_price_mode == "close":
        return _DrawingLayout(start_index, event.x, float(start_bar.close), event.y)
    if drawing.direction == "up":
        return _DrawingLayout(start_index, event.x, float(start_bar.low), event.y)
    return _DrawingLayout(start_index, event.x, float(start_bar.high), event.y)


def _updated_drag_drawing(
    series: OHLCVSeries,
    session: DragSession,
    event: ChartEvent,
) -> FibRetracementRecord | None:
    if event.bar_index is None or event.timestamp_ms is None:
        return None
    drawing = fib_drawing_for_key(session.working_params["drawings"], session.handle_key)
    if drawing is None:
        return None
    start_index = _timestamp_to_exact_index(drawing.start_timestamp_ms, series)
    if start_index is None or event.bar_index <= start_index:
        return None
    return FibRetracementRecord(
        symbol=drawing.symbol,
        timeframe=drawing.timeframe,
        start_timestamp_ms=drawing.start_timestamp_ms,
        end_timestamp_ms=event.timestamp_ms,
        direction=_direction_for_event(series, start_index, event),
        anchor_price_mode=drawing.anchor_price_mode,
        color=drawing.color,
        line_width=drawing.line_width,
        line_style=drawing.line_style,
        show_price_labels=drawing.show_price_labels,
        label_position=drawing.label_position,
        show_anchor_handles=drawing.show_anchor_handles,
        visible_levels=drawing.visible_levels,
        drawing_id=drawing.drawing_id,
    )


def _configured_drawing(
    drawing: FibRetracementRecord,
    params: dict[str, Any],
) -> FibRetracementRecord:
    return FibRetracementRecord(
        symbol=drawing.symbol,
        timeframe=drawing.timeframe,
        start_timestamp_ms=drawing.start_timestamp_ms,
        end_timestamp_ms=drawing.end_timestamp_ms,
        direction=drawing.direction,
        anchor_price_mode=_choice_value(params["anchor_price_mode"]),
        color=str(params["color"]),
        line_width=float(params["line_width"]),
        line_style=_choice_value(params["line_style"]),
        show_price_labels=bool(params["show_price_labels"]),
        label_position=_choice_value(params["label_position"]),
        show_anchor_handles=bool(params["show_anchor_handles"]),
        visible_levels=_visible_levels_from_params(params),
        drawing_id=drawing.drawing_id,
    )


def _timestamp_to_exact_index(timestamp_ms: int, series: OHLCVSeries) -> int | None:
    timestamps = [int(bar.timestamp.timestamp() * 1000) for bar in series.bars]
    idx = bisect_left(timestamps, timestamp_ms)
    if idx < len(timestamps) and timestamps[idx] == timestamp_ms:
        return idx
    return None


def _direction_for_event(
    series: OHLCVSeries,
    start_index: int,
    event: ChartEvent,
) -> str:
    start_price = float(series.bars[start_index].close)
    return "up" if event.y >= start_price else "down"


def _level_price(layout: _DrawingLayout, level: float) -> float:
    return layout.end_price + ((layout.start_price - layout.end_price) * level)


def _level_label(level: float, price: float, show_price: bool) -> str:
    percent = f"{level * 100.0:g}%"
    if show_price:
        return f"{percent} {price:.2f}"
    return percent


def _level_key(level: float) -> str:
    return f"{level * 1000.0:.0f}"


def _label_index(
    drawing: FibRetracementRecord,
    layout: _DrawingLayout,
    bar_count: int,
) -> int:
    if drawing.label_position == "left":
        return layout.start_index
    return max(0, bar_count - 1)


def _level_hit(layout: _DrawingLayout, event: ChartEvent) -> bool:
    if event.x < min(layout.start_index, layout.end_index) - _HIT_X_BUFFER:
        return False
    low = min(layout.start_price, layout.end_price)
    high = max(layout.start_price, layout.end_price)
    buffer = max(abs(high - low) * 0.015, max(abs(high), 1.0) * 0.0005)
    return any(
        abs(event.y - _level_price(layout, level)) <= buffer
        for level in FIB_LEVELS
    )


def _end_handle_hit(
    series: OHLCVSeries,
    layout: _DrawingLayout,
    event: ChartEvent,
) -> bool:
    if abs(event.x - float(layout.end_index)) > _HIT_X_BUFFER:
        return False
    buffer = max(
        abs(layout.end_price) * _HANDLE_HIT_Y_FRACTION,
        _local_price_range(series, int(round(layout.end_index))) * 0.4,
    )
    return abs(event.y - layout.end_price) <= buffer


def _local_price_range(series: OHLCVSeries, index: int) -> float:
    start = max(0, index - 5)
    end = min(len(series.bars), index + 6)
    ranges = [
        abs(bar.high - bar.low)
        for bar in series.bars[start:end]
        if abs(bar.high - bar.low) > 0.0
    ]
    return max(ranges) if ranges else 0.0


def _visible_levels_from_params(params: dict[str, Any]) -> tuple[float, ...]:
    values: list[float] = []
    for level, key in (
        (0.0, "show_0_0"),
        (0.236, "show_23_6"),
        (0.382, "show_38_2"),
        (0.5, "show_50_0"),
        (0.618, "show_61_8"),
        (1.0, "show_100_0"),
    ):
        if bool(params.get(key, True)):
            values.append(level)
    return tuple(values)


def _choice_value(value: Any) -> str:
    if isinstance(value, ChoiceParam):
        return value.value
    return str(value)


register_extension(FibonacciRetracementIndicator)
register_store_handler(FibRetracementStore)
