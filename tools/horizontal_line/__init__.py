from typing import Any

import numpy as np

from tools.horizontal_line.models import HorizontalLineRecord
from tools.horizontal_line.session_store import (
    HorizontalLineStore,
    horizontal_line_key,
)
from simplechart.api import (
    AxisPriceLabelRender,
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
    HorizontalLineRender,
    LINE_STYLE_OPTIONS,
    OHLCVSeries,
    ToolIconLine,
    ToolIconSpec,
    register_extension,
    register_store_handler,
)

_DEFAULT_COLOR = "#7a7f8c"
_DEFAULT_AGE_OFF_DAYS = 365.0


class HorizontalLineIndicator(ChartExtension):

    def name(self) -> str:
        return "horizontal_line"

    def label(self) -> str:
        return "Horizontal Line"

    def default_params(self) -> dict[str, Any]:
        return {
            "lines": [],
            "color": _DEFAULT_COLOR,
            "line_width": 1.0,
            "line_style": ChoiceParam("solid", LINE_STYLE_OPTIONS),
            "age_off_days": FloatParam(_DEFAULT_AGE_OFF_DAYS, minimum=0.0, maximum=3650.0, step=1.0, decimals=1),
        }

    def add_mode(self) -> ChartExtensionAddMode:
        return ChartExtensionAddMode.TOOLBAR

    def toolbar_icon(self) -> ToolIconSpec:
        return ToolIconSpec(lines=(
            ToolIconLine(4, 12, 20, 12),
            ToolIconLine(4, 7, 4, 17),
            ToolIconLine(20, 7, 20, 17),
        ))

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
            key = horizontal_line_key(line)
            render.horizontal_lines.append(
                HorizontalLineRender(
                    key=key,
                    y_value=line.price,
                    label="Horizontal Line",
                    color=line.color,
                    line_width=line.line_width,
                    line_style=line.line_style,
                )
            )
            render.axis_price_labels.append(
                AxisPriceLabelRender(
                    key=key,
                    y_value=line.price,
                    text=f"{line.price:.2f}",
                    fill_color=line.color,
                    text_color=_contrasting_text(line.color),
                )
            )
        return render

    def start_drawing(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        event: ChartEvent,
    ) -> DrawingToolResult:
        record = HorizontalLineRecord(
            symbol=series.symbol,
            price=float(event.y),
            timeframe=series.timeframe.value,
            color=str(params.get("color", _DEFAULT_COLOR)),
            line_width=float(params.get("line_width", 1.0)),
            line_style=_choice_value(params.get("line_style", "solid")),
            age_off_days=_float_value(params.get("age_off_days", _DEFAULT_AGE_OFF_DAYS)),
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
        # Buffer in price units derived from a pixel target. Falls back to a
        # small fixed price band when pixel_size is unavailable (test path).
        buffer = 5.0 * event.pixel_size_y if event.pixel_size_y > 0 else 0.5
        for line in _lines_for_symbol(params, series.symbol):
            key = horizontal_line_key(line)
            if key not in visible_keys:
                continue
            if abs(event.y - line.price) <= buffer:
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
            updated if horizontal_line_key(line) == session.handle_key else line
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
        line = horizontal_line_for_key(
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
        return None

    def config_for_series(
        self,
        series_key: str,
        params: dict[str, Any],
    ) -> ChartExtensionConfig | None:
        line = horizontal_line_for_key(params.get("lines", []), series_key)
        if line is None:
            return None
        return ChartExtensionConfig(
            label="Horizontal Line",
            params={
                "price": FloatParam(line.price, step=0.01),
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
        line = horizontal_line_for_key(params.get("lines", []), series_key)
        if line is None:
            return None
        new_price = _float_value(edited_params["price"])
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="update",
            payload={
                "record": HorizontalLineRecord(
                    symbol=line.symbol,
                    price=new_price,
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
        line = horizontal_line_for_key(params.get("lines", []), series_key)
        if line is None:
            return None
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="delete",
            payload={"record": line},
        )


def horizontal_line_for_key(
    lines: list[HorizontalLineRecord],
    series_key: str,
) -> HorizontalLineRecord | None:
    if series_key.startswith("horizontal_line_price_"):
        scaled = int(series_key.removeprefix("horizontal_line_price_"))
        return next(
            (line for line in lines if int(line.price * 1_000_000) == scaled),
            None,
        )
    if series_key.startswith("horizontal_line_"):
        line_id = int(series_key.removeprefix("horizontal_line_"))
        return next((line for line in lines if line.line_id == line_id), None)
    return None


def _lines_for_symbol(
    params: dict[str, Any],
    symbol: str,
) -> list[HorizontalLineRecord]:
    return [
        line for line in params.get("lines", [])
        if line.symbol == symbol
    ]


def _updated_drag_line(
    session: DragSession,
    event: ChartEvent,
) -> HorizontalLineRecord | None:
    line = horizontal_line_for_key(
        session.working_params["lines"],
        session.handle_key,
    )
    if line is None:
        return None
    return HorizontalLineRecord(
        symbol=line.symbol,
        price=float(event.y),
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


def _contrasting_text(hex_color: str) -> str:
    color = hex_color.lstrip("#")
    if len(color) != 6:
        return "#ffffff"
    r = int(color[0:2], 16)
    g = int(color[2:4], 16)
    b = int(color[4:6], 16)
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return "#000000" if luminance >= 128 else "#ffffff"


def _choice_value(value: Any) -> str:
    if isinstance(value, ChoiceParam):
        return value.value
    return str(value)


def _float_value(value: Any) -> float:
    if isinstance(value, FloatParam):
        return value.value
    return float(value)


register_extension(HorizontalLineIndicator)
register_store_handler(HorizontalLineStore)
