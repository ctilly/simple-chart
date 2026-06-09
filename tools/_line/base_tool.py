from abc import abstractmethod
from typing import Any, ClassVar, Generic

import numpy as np

from tools._line.base_store import R, line_series_key
from simplechart.api import (
    ChartEvent,
    ChartExtension,
    ChartExtensionAddMode,
    ChartExtensionConfig,
    ChartExtensionMutation,
    ChartExtensionRender,
    ChoiceParam,
    DragSession,
    DrawingToolResult,
    FloatParam,
    HitTestResult,
    LINE_STYLE_OPTIONS,
    OHLCVSeries,
    ToolIconSpec,
)

_DEFAULT_COLOR = "#8b5a2b"


class LineTool(ChartExtension, Generic[R]):
    """
    Shared behavior for single-anchor line tools (horizontal, vertical).

    A line is placed with one click, dragged by its single anchor, and
    configured/removed like any other drawing. The entire lifecycle —
    drawing, rendering, hit testing, dragging, configuration, removal — is
    identical between the two tools and lives here. Subclasses fill only the
    axis seam: the identity (name via key_prefix, label, icon, default
    age-off), how the anchor maps to/from a chart event, how it renders, and
    how it hit-tests. The record-shape adapter (coord access, assembly, key
    encoding) is mixed in and shared with the store.
    """

    # ------------------------------------------------------------------
    # Record-shape adapter — provided by the per-axis mixin
    # ------------------------------------------------------------------

    key_prefix: ClassVar[str]
    coord_infix: ClassVar[str]

    @abstractmethod
    def _coord_of(self, record: R) -> float: ...

    @abstractmethod
    def _encode_coord(self, coord: float) -> int: ...

    @abstractmethod
    def _assemble_record(
        self,
        *,
        coord: float,
        symbol: str,
        timeframe: str,
        color: str,
        line_width: float,
        line_style: str,
        persist_across_timeframes: bool,
        persist_across_sessions: bool,
        updated_at_ms: int,
        age_off_days: float,
        line_id: int | None,
    ) -> R: ...

    # ------------------------------------------------------------------
    # Axis seam — concrete tools implement these
    # ------------------------------------------------------------------

    @abstractmethod
    def label(self) -> str: ...

    @abstractmethod
    def toolbar_icon(self) -> ToolIconSpec: ...

    @abstractmethod
    def default_age_off_days(self) -> float: ...

    @abstractmethod
    def _coord_from_event(self, event: ChartEvent) -> float | None:
        """Anchor value implied by a chart event, or None if it has none."""

    @abstractmethod
    def _append_render(
        self,
        render: ChartExtensionRender,
        record: R,
        series: OHLCVSeries,
    ) -> None:
        """Append this line's render primitive(s) for the given record."""

    @abstractmethod
    def _hit(self, event: ChartEvent, record: R, series: OHLCVSeries) -> bool:
        """Whether the event lands on this line."""

    def _config_coord_params(self, record: R) -> dict[str, Any]:
        """Axis-specific Configure fields for the anchor (none by default)."""
        return {}

    def _coord_from_config(self, record: R, edited_params: dict[str, Any]) -> float:
        """Anchor value after a Configure edit (unchanged by default)."""
        return self._coord_of(record)

    # ------------------------------------------------------------------
    # Extension surface
    # ------------------------------------------------------------------

    def name(self) -> str:
        return self.key_prefix

    def default_params(self) -> dict[str, Any]:
        return {
            "lines": [],
            "color": _DEFAULT_COLOR,
            "line_width": 1.0,
            "line_style": ChoiceParam("solid", LINE_STYLE_OPTIONS),
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
        for line in self._lines_for_symbol(params, series.symbol):
            self._append_render(render, line, series)
        return render

    def start_drawing(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        event: ChartEvent,
    ) -> DrawingToolResult:
        coord = self._coord_from_event(event)
        if coord is None:
            return DrawingToolResult()
        record = self._assemble_record(
            coord=coord,
            symbol=series.symbol,
            timeframe=series.timeframe.value,
            color=str(params.get("color", _DEFAULT_COLOR)),
            line_width=float(params.get("line_width", 1.0)),
            line_style=_choice_value(params.get("line_style", "solid")),
            persist_across_timeframes=False,
            persist_across_sessions=False,
            updated_at_ms=0,
            age_off_days=_float_value(params.get("age_off_days", self.default_age_off_days())),
            line_id=None,
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
        for line in self._lines_for_symbol(params, series.symbol):
            key = self._series_key(line)
            if key not in visible_keys:
                continue
            if self._hit(event, line, series):
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
        lines = self._lines_for_symbol(params, series.symbol)
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
        updated = self._updated_drag_line(session, event)
        if updated is None:
            return None
        session.working_params["lines"] = [
            updated if self._series_key(line) == session.handle_key else line
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
        line = self._record_for_key(session.working_params["lines"], session.handle_key)
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
        line = self._record_for_key(params.get("lines", []), series_key)
        if line is None:
            return None
        return ChartExtensionConfig(
            label=self.label(),
            params={
                **self._config_coord_params(line),
                "color": line.color,
                "line_width": line.line_width,
                "line_style": ChoiceParam(line.line_style, LINE_STYLE_OPTIONS),
                "age_off_days": FloatParam(line.age_off_days, minimum=0.0, maximum=3650.0, step=1.0, decimals=1),
                "persist_across_timeframes": line.persist_across_timeframes,
                "persist_across_sessions": line.persist_across_sessions,
            },
        )

    def apply_config_to_series(
        self,
        series_key: str,
        params: dict[str, Any],
        edited_params: dict[str, Any],
        y_range: tuple[float, float] | None = None,
    ) -> ChartExtensionMutation | None:
        line = self._record_for_key(params.get("lines", []), series_key)
        if line is None:
            return None
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="update",
            payload={
                "record": self._assemble_record(
                    coord=self._coord_from_config(line, edited_params),
                    symbol=line.symbol,
                    timeframe=line.timeframe,
                    color=str(edited_params["color"]),
                    line_width=float(edited_params["line_width"]),
                    line_style=_choice_value(edited_params["line_style"]),
                    persist_across_timeframes=bool(edited_params["persist_across_timeframes"]),
                    persist_across_sessions=bool(edited_params["persist_across_sessions"]),
                    updated_at_ms=line.updated_at_ms,
                    age_off_days=_float_value(edited_params["age_off_days"]),
                    line_id=line.line_id,
                )
            },
        )

    def remove_series(
        self,
        series_key: str,
        params: dict[str, Any],
    ) -> ChartExtensionMutation | None:
        line = self._record_for_key(params.get("lines", []), series_key)
        if line is None:
            return None
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="delete",
            payload={"record": line},
        )

    # ------------------------------------------------------------------
    # Shared internals
    # ------------------------------------------------------------------

    def _lines_for_symbol(self, params: dict[str, Any], symbol: str) -> list[R]:
        return [line for line in params.get("lines", []) if line.symbol == symbol]

    def _series_key(self, record: R) -> str:
        return line_series_key(
            self.key_prefix,
            self.coord_infix,
            self._encode_coord(self._coord_of(record)),
            record.line_id,
        )

    def _record_for_key(self, lines: list[R], series_key: str) -> R | None:
        coord_prefix = f"{self.key_prefix}_{self.coord_infix}_"
        if series_key.startswith(coord_prefix):
            encoded = int(series_key.removeprefix(coord_prefix))
            return next(
                (line for line in lines if self._encode_coord(self._coord_of(line)) == encoded),
                None,
            )
        id_prefix = f"{self.key_prefix}_"
        if series_key.startswith(id_prefix):
            line_id = int(series_key.removeprefix(id_prefix))
            return next((line for line in lines if line.line_id == line_id), None)
        return None

    def _updated_drag_line(self, session: DragSession, event: ChartEvent) -> R | None:
        coord = self._coord_from_event(event)
        if coord is None:
            return None
        line = self._record_for_key(session.working_params["lines"], session.handle_key)
        if line is None:
            return None
        return self._assemble_record(
            coord=coord,
            symbol=line.symbol,
            timeframe=line.timeframe,
            color=line.color,
            line_width=line.line_width,
            line_style=line.line_style,
            persist_across_timeframes=line.persist_across_timeframes,
            persist_across_sessions=line.persist_across_sessions,
            updated_at_ms=line.updated_at_ms,
            age_off_days=line.age_off_days,
            line_id=line.line_id,
        )


def _choice_value(value: Any) -> str:
    if isinstance(value, ChoiceParam):
        return value.value
    return str(value)


def _float_value(value: Any) -> float:
    if isinstance(value, FloatParam):
        return value.value
    return float(value)
