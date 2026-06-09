from typing import Any

from tools._line import LineTool
from tools._line.base_tool import _float_value
from tools.horizontal_line.models import HorizontalLineRecord, HorizontalLineShape
from tools.horizontal_line.session_store import HorizontalLineStore
from simplechart.api import (
    AxisPriceLabelRender,
    ChartEvent,
    ChartExtensionRender,
    FloatParam,
    HorizontalLineRender,
    OHLCVSeries,
    ToolIconLine,
    ToolIconSpec,
    register_extension,
    register_store_handler,
)

_DEFAULT_AGE_OFF_DAYS = 365.0


class HorizontalLineIndicator(HorizontalLineShape, LineTool[HorizontalLineRecord]):

    def label(self) -> str:
        return "Horizontal Line"

    def toolbar_icon(self) -> ToolIconSpec:
        return ToolIconSpec(lines=(
            ToolIconLine(4, 12, 20, 12),
            ToolIconLine(4, 7, 4, 17),
            ToolIconLine(20, 7, 20, 17),
        ))

    def default_age_off_days(self) -> float:
        return _DEFAULT_AGE_OFF_DAYS

    def _coord_from_event(self, event: ChartEvent) -> float | None:
        return float(event.y)

    def _append_render(
        self,
        render: ChartExtensionRender,
        record: HorizontalLineRecord,
        series: OHLCVSeries,
    ) -> None:
        key = self._series_key(record)
        render.horizontal_lines.append(
            HorizontalLineRender(
                key=key,
                y_value=record.price,
                label=self.label(),
                color=record.color,
                line_width=record.line_width,
                line_style=record.line_style,
            )
        )
        render.axis_price_labels.append(
            AxisPriceLabelRender(
                key=key,
                y_value=record.price,
                text=f"{record.price:.2f}",
                fill_color=record.color,
                text_color=_contrasting_text(record.color),
            )
        )

    def _hit(self, event: ChartEvent, record: HorizontalLineRecord, series: OHLCVSeries) -> bool:
        # Buffer in price units derived from a pixel target. Falls back to a
        # small fixed price band when pixel_size is unavailable (test path).
        buffer = 5.0 * event.pixel_size_y if event.pixel_size_y > 0 else 0.5
        return abs(event.y - record.price) <= buffer

    def _config_coord_params(self, record: HorizontalLineRecord) -> dict[str, Any]:
        return {"price": FloatParam(record.price, step=0.01)}

    def _coord_from_config(self, record: HorizontalLineRecord, edited_params: dict[str, Any]) -> float:
        return _float_value(edited_params["price"])


def _contrasting_text(hex_color: str) -> str:
    color = hex_color.lstrip("#")
    if len(color) != 6:
        return "#ffffff"
    r = int(color[0:2], 16)
    g = int(color[2:4], 16)
    b = int(color[4:6], 16)
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return "#000000" if luminance >= 128 else "#ffffff"


register_extension(HorizontalLineIndicator)
register_store_handler(HorizontalLineStore)
