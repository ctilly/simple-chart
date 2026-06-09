from bisect import bisect_left

from tools._line import LineTool
from tools.vertical_line.models import VerticalLineRecord, VerticalLineShape
from tools.vertical_line.session_store import VerticalLineStore
from simplechart.api import (
    ChartEvent,
    ChartExtensionRender,
    OHLCVSeries,
    ToolIconLine,
    ToolIconSpec,
    VerticalLineRender,
    register_extension,
    register_store_handler,
)

_DEFAULT_AGE_OFF_DAYS = 60.0
_HIT_BUFFER = 0.55


class VerticalLineIndicator(VerticalLineShape, LineTool[VerticalLineRecord]):

    def label(self) -> str:
        return "Vertical Line"

    def toolbar_icon(self) -> ToolIconSpec:
        return ToolIconSpec(lines=(
            ToolIconLine(12, 4, 12, 20),
            ToolIconLine(7, 4, 17, 4),
            ToolIconLine(7, 20, 17, 20),
        ))

    def default_age_off_days(self) -> float:
        return _DEFAULT_AGE_OFF_DAYS

    def _coord_from_event(self, event: ChartEvent) -> float | None:
        if event.timestamp_ms is None:
            return None
        return float(event.timestamp_ms)

    def _append_render(
        self,
        render: ChartExtensionRender,
        record: VerticalLineRecord,
        series: OHLCVSeries,
    ) -> None:
        x_index = _timestamp_to_nearest_index(record.timestamp_ms, _series_timestamps(series))
        if x_index is None:
            return
        render.vertical_lines.append(
            VerticalLineRender(
                key=self._series_key(record),
                x_index=float(x_index),
                label=self.label(),
                color=record.color,
                line_width=record.line_width,
                line_style=record.line_style,
            )
        )

    def _hit(self, event: ChartEvent, record: VerticalLineRecord, series: OHLCVSeries) -> bool:
        x_index = _timestamp_to_nearest_index(record.timestamp_ms, _series_timestamps(series))
        if x_index is None:
            return False
        return abs(event.x - float(x_index)) <= _HIT_BUFFER


def _series_timestamps(series: OHLCVSeries) -> list[int]:
    return [int(bar.timestamp.timestamp() * 1000) for bar in series.bars]


def _timestamp_to_nearest_index(
    timestamp_ms: int,
    timestamps: list[int],
) -> int | None:
    if not timestamps:
        return None
    idx = bisect_left(timestamps, timestamp_ms)
    candidates: list[int] = []
    if idx < len(timestamps):
        candidates.append(idx)
    if idx > 0:
        candidates.append(idx - 1)
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: abs(timestamps[candidate] - timestamp_ms))


register_extension(VerticalLineIndicator)
register_store_handler(VerticalLineStore)
