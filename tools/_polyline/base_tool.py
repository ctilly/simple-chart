from abc import abstractmethod
from bisect import bisect_right
from dataclasses import replace
from typing import Any

import numpy as np

from tools._polyline.base_store import polyline_record_for_key, polyline_series_key
from tools._polyline.geometry import body_distance, nearest_vertex
from tools._polyline.record import PolylineRecord
from simplechart.api import (
    ChartEvent,
    ChartExtension,
    ChartExtensionAddMode,
    ChartExtensionConfig,
    ChartExtensionMutation,
    ChartExtensionRender,
    ChoiceParam,
    DragSession,
    DrawingSession,
    DrawingToolResult,
    FloatParam,
    HitTestResult,
    LINE_STYLE_OPTIONS,
    MarkerRender,
    OHLCVSeries,
    PolylineRender,
)

_DEFAULT_COLOR = "#8b5a2b"
_HANDLE_PIXELS = 9.0
_LINE_PIXELS = 6.0
_VERTEX_SUFFIX = "__vtx"
_BODY_SUFFIX = "__body"


class PolylineTool(ChartExtension):
    """
    Shared behavior for free-form line tools (trend line, poly-line).

    Subclasses fill only the identity seam — name, label, toolbar icon, the
    series-key prefix, the vertex cap, and the default age-off. Everything else
    (rendering, hit testing, per-vertex and whole-body dragging, configuration,
    removal) is identical and lives here. Drawing is generic too: each left
    click adds a vertex, the path auto-commits at the cap, and Enter commits any
    path with at least two vertices.
    """

    # ------------------------------------------------------------------
    # Identity seam — concrete tools implement these
    # ------------------------------------------------------------------

    @abstractmethod
    def key_prefix(self) -> str: ...

    @abstractmethod
    def max_vertices(self) -> int: ...

    @abstractmethod
    def default_age_off_days(self) -> float: ...

    # ------------------------------------------------------------------
    # Extension surface
    # ------------------------------------------------------------------

    def default_params(self) -> dict[str, Any]:
        return {
            "drawings": [],
            "color": _DEFAULT_COLOR,
            "line_width": 1.0,
            "line_style": ChoiceParam("solid", LINE_STYLE_OPTIONS),
            "persist_across_timeframes": False,
            "persist_across_sessions": False,
            "age_off_days": FloatParam(self.default_age_off_days(), minimum=0.0, maximum=3650.0, step=1.0, decimals=1),
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
        timestamps = _series_timestamps(series)
        if not timestamps:
            return render
        for drawing in self._drawings_for_series(params, series):
            layout = self._layout(drawing, timestamps)
            self._append_path(render, self._series_key(drawing), drawing, layout, layout)
        return render

    # ------------------------------------------------------------------
    # Drawing interaction
    # ------------------------------------------------------------------

    def start_drawing(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        event: ChartEvent,
    ) -> DrawingToolResult:
        timestamp = self._event_timestamp(event, _series_timestamps(series))
        if timestamp is None:
            return DrawingToolResult()
        return DrawingToolResult(
            session=DrawingSession(
                extension_name=self.name(),
                tool_key=self.key_prefix(),
                original_params=dict(params),
                working_params={
                    "vertices": [(timestamp, event.y)],
                    "defaults": self._drawing_defaults(params),
                },
            )
        )

    def preview_drawing(
        self,
        series: OHLCVSeries,
        session: DrawingSession,
        event: ChartEvent,
    ) -> ChartExtensionRender | None:
        render = ChartExtensionRender()
        timestamps = _series_timestamps(series)
        if not timestamps:
            return render
        placed = self._placed_layout(session, timestamps)
        path = placed + [(event.x, event.y)]
        defaults: dict[str, Any] = session.working_params["defaults"]
        self._append_path(
            render,
            f"{self.key_prefix()}_preview",
            _DrawingStyle(defaults),
            path,
            placed,
        )
        return render

    def advance_drawing(
        self,
        series: OHLCVSeries,
        session: DrawingSession,
        event: ChartEvent,
    ) -> DrawingToolResult:
        timestamps = _series_timestamps(series)
        timestamp = self._event_timestamp(event, timestamps)
        if timestamp is None:
            return DrawingToolResult(session=session)
        vertices: list[tuple[int, float]] = session.working_params["vertices"]
        vertices.append((timestamp, event.y))
        if len(vertices) >= self.max_vertices():
            return self._commit(series, session)
        render = ChartExtensionRender()
        placed = self._placed_layout(session, timestamps)
        defaults: dict[str, Any] = session.working_params["defaults"]
        self._append_path(render, f"{self.key_prefix()}_preview", _DrawingStyle(defaults), placed, placed)
        return DrawingToolResult(session=session, render=render)

    def commit_drawing(
        self,
        series: OHLCVSeries,
        session: DrawingSession,
    ) -> DrawingToolResult:
        return self._commit(series, session)

    def cancel_drawing(self, session: DrawingSession) -> ChartExtensionMutation | None:
        return None

    def _commit(self, series: OHLCVSeries, session: DrawingSession) -> DrawingToolResult:
        vertices: list[tuple[int, float]] = session.working_params["vertices"]
        if len(vertices) < 2:
            return DrawingToolResult(cancel=True, deactivate_tool=True)
        return DrawingToolResult(
            mutation=ChartExtensionMutation(
                extension_name=self.name(),
                operation="add",
                payload={"record": self._record_from_session(series, session)},
            ),
            done=True,
            deactivate_tool=True,
        )

    # ------------------------------------------------------------------
    # Hit testing and dragging
    # ------------------------------------------------------------------

    def hit_test(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        event: ChartEvent,
        visible_keys: set[str],
    ) -> HitTestResult | None:
        timestamps = _series_timestamps(series)
        if not timestamps:
            return None
        for drawing in self._drawings_for_series(params, series):
            key = self._series_key(drawing)
            if key not in visible_keys:
                continue
            layout = self._layout(drawing, timestamps)
            vertex = nearest_vertex(
                event.x, event.y, layout, event.pixel_size_x, event.pixel_size_y, _HANDLE_PIXELS
            )
            on_body = body_distance(
                event.x, event.y, layout, event.pixel_size_x, event.pixel_size_y
            ) <= _LINE_PIXELS
            if event.button == "right":
                # Right-click resolves to the drawing as a whole so Configure /
                # Remove can look it up by its series key.
                if vertex is not None or on_body:
                    return HitTestResult(self.name(), key, cursor=None, priority=10)
                continue
            if vertex is not None:
                return HitTestResult(self.name(), f"{key}{_VERTEX_SUFFIX}{vertex}", cursor="drag", priority=20)
            if on_body:
                return HitTestResult(self.name(), f"{key}{_BODY_SUFFIX}", cursor="drag", priority=10)
        return None

    def begin_drag(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        hit: HitTestResult,
    ) -> DragSession:
        base_key, mode, index = _parse_handle(hit.handle_key)
        record = polyline_record_for_key(
            self.key_prefix(), self._drawings_for_series(params, series), base_key
        )
        return DragSession(
            extension_name=self.name(),
            handle_key=base_key,
            original_params={"record": record},
            working_params={"record": record, "mode": mode, "index": index, "origin": None},
        )

    def drag_to(
        self,
        series: OHLCVSeries,
        session: DragSession,
        event: ChartEvent,
    ) -> ChartExtensionRender | None:
        record: PolylineRecord | None = session.working_params["record"]
        if record is None:
            return None
        timestamps = _series_timestamps(series)
        updated: PolylineRecord | None
        if session.working_params["mode"] == "vertex":
            updated = self._move_vertex(record, int(session.working_params["index"]), event, timestamps)
        else:
            updated = self._move_body(session, event, timestamps)
        if updated is None:
            return None
        session.working_params["record"] = updated
        render = ChartExtensionRender()
        layout = self._layout(updated, timestamps)
        self._append_path(render, session.handle_key, updated, layout, layout)
        return render

    def finish_drag(
        self,
        series: OHLCVSeries,
        session: DragSession,
        event: ChartEvent,
    ) -> ChartExtensionMutation | None:
        self.drag_to(series, session, event)
        record: PolylineRecord | None = session.working_params["record"]
        if record is None:
            return None
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="update",
            payload={"record": record},
        )

    def cancel_drag(self, session: DragSession) -> ChartExtensionMutation | None:
        # Dragging only previews; the store is untouched, so the controller
        # re-renders from store state on cancel.
        return None

    def _move_vertex(
        self,
        record: PolylineRecord,
        index: int,
        event: ChartEvent,
        timestamps: list[int],
    ) -> PolylineRecord | None:
        timestamp = self._event_timestamp(event, timestamps)
        if timestamp is None:
            return None
        vertices = list(record.vertices)
        vertices[index] = (timestamp, event.y)
        return replace(record, vertices=tuple(vertices))

    def _move_body(
        self,
        session: DragSession,
        event: ChartEvent,
        timestamps: list[int],
    ) -> PolylineRecord | None:
        record: PolylineRecord = session.original_params["record"]
        if not timestamps:
            return None
        if session.working_params["origin"] is None:
            session.working_params["origin"] = {
                "grab_x": event.x,
                "grab_price": event.y,
                "indices": [_timestamp_to_index(ts, timestamps) for ts, _ in record.vertices],
                "prices": [price for _, price in record.vertices],
            }
        origin: dict[str, Any] = session.working_params["origin"]
        delta_index = event.x - float(origin["grab_x"])
        delta_price = event.y - float(origin["grab_price"])
        vertices: list[tuple[int, float]] = []
        for base_index, base_price in zip(origin["indices"], origin["prices"]):
            shifted = float(base_index) + delta_index
            vertices.append((_index_to_timestamp(shifted, timestamps), base_price + delta_price))
        return replace(record, vertices=tuple(vertices))

    # ------------------------------------------------------------------
    # Configuration and removal
    # ------------------------------------------------------------------

    def config_for_series(
        self,
        series_key: str,
        params: dict[str, Any],
    ) -> ChartExtensionConfig | None:
        drawing = polyline_record_for_key(self.key_prefix(), params.get("drawings", []), series_key)
        if drawing is None:
            return None
        return ChartExtensionConfig(
            label=self.label(),
            params={
                "color": drawing.color,
                "line_width": drawing.line_width,
                "line_style": ChoiceParam(drawing.line_style, LINE_STYLE_OPTIONS),
                "persist_across_timeframes": drawing.persist_across_timeframes,
                "persist_across_sessions": drawing.persist_across_sessions,
                "age_off_days": FloatParam(drawing.age_off_days, minimum=0.0, maximum=3650.0, step=1.0, decimals=1),
            },
        )

    def apply_config_to_series(
        self,
        series_key: str,
        params: dict[str, Any],
        edited_params: dict[str, Any],
        y_range: tuple[float, float] | None = None,
    ) -> ChartExtensionMutation | None:
        drawing = polyline_record_for_key(self.key_prefix(), params.get("drawings", []), series_key)
        if drawing is None:
            return None
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="update",
            payload={
                "record": replace(
                    drawing,
                    color=str(edited_params["color"]),
                    line_width=float(edited_params["line_width"]),
                    line_style=_choice_value(edited_params["line_style"]),
                    persist_across_timeframes=bool(edited_params["persist_across_timeframes"]),
                    persist_across_sessions=bool(edited_params["persist_across_sessions"]),
                    age_off_days=_float_value(edited_params["age_off_days"]),
                )
            },
        )

    def remove_series(
        self,
        series_key: str,
        params: dict[str, Any],
    ) -> ChartExtensionMutation | None:
        drawing = polyline_record_for_key(self.key_prefix(), params.get("drawings", []), series_key)
        if drawing is None:
            return None
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="delete",
            payload={"record": drawing},
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _series_key(self, drawing: PolylineRecord) -> str:
        return polyline_series_key(self.key_prefix(), drawing)

    def _drawings_for_series(
        self,
        params: dict[str, Any],
        series: OHLCVSeries,
    ) -> list[PolylineRecord]:
        return [
            drawing for drawing in params.get("drawings", [])
            if drawing.symbol == series.symbol
        ]

    def _layout(
        self,
        drawing: PolylineRecord,
        timestamps: list[int],
    ) -> list[tuple[float, float]]:
        return [(_timestamp_to_index(ts, timestamps), price) for ts, price in drawing.vertices]

    def _placed_layout(
        self,
        session: DrawingSession,
        timestamps: list[int],
    ) -> list[tuple[float, float]]:
        return [
            (_timestamp_to_index(ts, timestamps), price)
            for ts, price in session.working_params["vertices"]
        ]

    def _event_timestamp(
        self,
        event: ChartEvent,
        timestamps: list[int],
    ) -> int | None:
        # In-range clicks carry a bar-snapped timestamp; clicks in the empty
        # space past the last bar carry none, so we extrapolate one from the
        # fractional bar index. Either way the position is stored as a timestamp,
        # which re-anchors to a real bar once that time becomes history.
        if event.timestamp_ms is not None:
            return event.timestamp_ms
        if not timestamps:
            return None
        return _index_to_timestamp(event.x, timestamps)

    def _append_path(
        self,
        render: ChartExtensionRender,
        key: str,
        style: "PolylineRecord | _DrawingStyle",
        path: list[tuple[float, float]],
        handles: list[tuple[float, float]],
    ) -> None:
        if len(path) >= 2:
            render.polylines.append(
                PolylineRender(
                    key=key,
                    points=tuple(path),
                    label=self.label(),
                    color=style.color,
                    line_width=style.line_width,
                    line_style=style.line_style,
                    # Free-form lines are managed entirely on-chart (drag handles,
                    # right-click configure/remove); never in the legend.
                    reference=True,
                )
            )
        for index, (x, y) in enumerate(handles):
            render.markers.append(
                MarkerRender(
                    key=f"{key}{_VERTEX_SUFFIX}{index}",
                    x_index=x,
                    y_value=y,
                    text="●",
                    color=style.color,
                    font_size=7,
                    # Center the node on the vertex so it sits on the line itself
                    # rather than floating above it.
                    anchor=(0.5, 0.5),
                )
            )

    def _drawing_defaults(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "color": str(params.get("color", _DEFAULT_COLOR)),
            "line_width": float(params.get("line_width", 1.0)),
            "line_style": _choice_value(params.get("line_style", "solid")),
            "persist_across_timeframes": bool(params.get("persist_across_timeframes", False)),
            "persist_across_sessions": bool(params.get("persist_across_sessions", False)),
            "age_off_days": _float_value(params.get("age_off_days", self.default_age_off_days())),
        }

    def _record_from_session(
        self,
        series: OHLCVSeries,
        session: DrawingSession,
    ) -> PolylineRecord:
        defaults: dict[str, Any] = session.working_params["defaults"]
        return PolylineRecord(
            symbol=series.symbol,
            timeframe=series.timeframe.value,
            vertices=tuple(session.working_params["vertices"]),
            color=str(defaults["color"]),
            line_width=float(defaults["line_width"]),
            line_style=str(defaults["line_style"]),
            persist_across_timeframes=bool(defaults["persist_across_timeframes"]),
            persist_across_sessions=bool(defaults["persist_across_sessions"]),
            age_off_days=float(defaults["age_off_days"]),
        )


class _DrawingStyle:
    """Lightweight style adapter so previews share the committed render path."""

    def __init__(self, defaults: dict[str, Any]) -> None:
        self.color = str(defaults["color"])
        self.line_width = float(defaults["line_width"])
        self.line_style = str(defaults["line_style"])


def _parse_handle(handle_key: str) -> tuple[str, str, int]:
    if _VERTEX_SUFFIX in handle_key:
        base, raw_index = handle_key.rsplit(_VERTEX_SUFFIX, 1)
        return base, "vertex", int(raw_index)
    if handle_key.endswith(_BODY_SUFFIX):
        return handle_key.removesuffix(_BODY_SUFFIX), "body", 0
    return handle_key, "body", 0


def _series_timestamps(series: OHLCVSeries) -> list[int]:
    return [int(bar.timestamp.timestamp() * 1000) for bar in series.bars]


def _timestamp_to_index(timestamp_ms: int, timestamps: list[int]) -> float:
    # Continuous inverse of _index_to_timestamp: a fractional bar index that
    # interpolates between bars in range and extrapolates linearly beyond the
    # first/last bar, so future positions keep going instead of snapping back.
    n = len(timestamps)
    if n <= 1:
        return 0.0
    if timestamp_ms <= timestamps[0]:
        step = timestamps[1] - timestamps[0]
        return (timestamp_ms - timestamps[0]) / step if step else 0.0
    if timestamp_ms >= timestamps[-1]:
        step = timestamps[-1] - timestamps[-2]
        return (n - 1) + (timestamp_ms - timestamps[-1]) / step if step else float(n - 1)
    idx = bisect_right(timestamps, timestamp_ms) - 1
    span = timestamps[idx + 1] - timestamps[idx]
    return idx + (timestamp_ms - timestamps[idx]) / span if span else float(idx)


def _index_to_timestamp(x: float, timestamps: list[int]) -> int:
    # Map a fractional bar index back to a timestamp, extrapolating with the
    # edge bar spacing past either end so positions in the empty future space
    # get a synthetic timestamp instead of being clamped to the last bar.
    n = len(timestamps)
    if n == 0:
        return 0
    if n == 1:
        return timestamps[0]
    if x <= 0.0:
        step = timestamps[1] - timestamps[0]
        return int(round(timestamps[0] + step * x))
    if x >= n - 1:
        step = timestamps[-1] - timestamps[-2]
        return int(round(timestamps[-1] + step * (x - (n - 1))))
    lower = int(x)
    base = timestamps[lower]
    step = timestamps[lower + 1] - base
    return int(round(base + step * (x - lower)))


def _choice_value(value: Any) -> str:
    if isinstance(value, ChoiceParam):
        return value.value
    return str(value)


def _float_value(value: Any) -> float:
    if isinstance(value, FloatParam):
        return value.value
    return float(value)
