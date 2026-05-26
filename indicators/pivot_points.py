from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from simplechart.api import (
    Bar,
    ChoiceParam,
    HorizontalSegmentRender,
    ChartExtension,
    ChartExtensionRender,
    LINE_STYLE_OPTIONS,
    MarkerRender,
    OHLCVSeries,
    register_extension,
)

_ET = ZoneInfo("America/New_York")
_METHOD_OPTIONS: list[str] = ["standard", "fibonacci", "camarilla"]
_LEVEL_ORDER_BY_METHOD: dict[str, tuple[str, ...]] = {
    "standard": ("r3", "r2", "r1", "p", "s1", "s2", "s3"),
    "fibonacci": ("r3", "r2", "r1", "p", "s1", "s2", "s3"),
    "camarilla": ("r4", "r3", "r2", "r1", "p", "s1", "s2", "s3", "s4"),
}


class PivotPointsIndicator(ChartExtension):

    def name(self) -> str:
        return "pivot_points"

    def label(self) -> str:
        return "Pivot Points"

    def default_params(self) -> dict[str, Any]:
        return {
            "periods": 3,
            "method": ChoiceParam("standard", _METHOD_OPTIONS),
            "show_labels": True,
            "resistance_color": "#f3bc4f",
            "pivot_color": "#46acf7",
            "support_color": "#f3bc4f",
            "line_width": 1.0,
            "line_style": ChoiceParam("solid", LINE_STYLE_OPTIONS),
        }

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
        method = _method_value(params["method"])
        periods = int(params["periods"])
        daily_bars = _daily_bars_for(series, params)
        session_dates = _display_session_dates(daily_bars, periods)
        session_ranges = _session_ranges(
            series.bars,
            session_dates,
            series.timeframe.is_intraday,
        )
        line_width = float(params["line_width"])
        line_style = _choice_value(params["line_style"])
        show_labels = bool(params["show_labels"])
        render = ChartExtensionRender()
        primary_key_used = False

        for i in range(1, len(daily_bars)):
            session_date = _daily_bar_date(daily_bars[i])
            if session_date not in session_dates:
                continue
            segment_range = session_ranges.get(session_date)
            if segment_range is None:
                continue
            levels = _pivot_levels(daily_bars[i - 1], method)
            for level in _LEVEL_ORDER_BY_METHOD[method]:
                key = _series_key(method, session_date, level)
                if not primary_key_used:
                    key = "pivot_points"
                    primary_key_used = True
                color = _level_color(level, params)
                render.segments.append(
                    HorizontalSegmentRender(
                        key=key,
                        x_start=segment_range.x_start,
                        x_end=segment_range.x_end,
                        y_value=levels[level],
                        label="Pivot Points",
                        color=color,
                        line_width=line_width,
                        line_style=line_style,
                    )
                )
                if show_labels:
                    render.markers.append(
                        MarkerRender(
                            key=key,
                            x_index=segment_range.label_index,
                            y_value=levels[level],
                            text=level.upper(),
                            color=color,
                            font_size=9,
                        )
                    )

        return render


register_extension(PivotPointsIndicator)


def _daily_bars_for(series: OHLCVSeries, params: dict[str, Any]) -> list[Bar]:
    if series.timeframe.is_intraday:
        return list(params.get("_daily_bars") or [])
    return series.bars


def _display_session_dates(daily_bars: list[Bar], periods: int) -> set[date]:
    if periods <= 0 or len(daily_bars) < 2:
        return set()
    session_bars = daily_bars[1:]
    return {
        _daily_bar_date(bar)
        for bar in session_bars[-periods:]
    }


class _SegmentRange:

    def __init__(self, x_start: float, x_end: float, label_index: int) -> None:
        self.x_start = x_start
        self.x_end = x_end
        self.label_index = label_index


def _session_ranges(
    bars: list[Bar],
    session_dates: set[date],
    is_intraday: bool,
) -> dict[date, _SegmentRange]:
    indexes: dict[date, list[int]] = {session_date: [] for session_date in session_dates}
    for i, bar in enumerate(bars):
        session_date = _intraday_bar_date(bar) if is_intraday else _daily_bar_date(bar)
        if session_date in indexes:
            indexes[session_date].append(i)
    ranges: dict[date, _SegmentRange] = {}
    for session_date, session_indexes in indexes.items():
        if not session_indexes:
            continue
        first_index = session_indexes[0]
        last_index = session_indexes[-1]
        if is_intraday:
            ranges[session_date] = _SegmentRange(
                float(first_index),
                float(last_index + 1),
                first_index,
            )
            continue
        ranges[session_date] = _SegmentRange(
            float(first_index) - 0.45,
            float(first_index) + 0.45,
            first_index,
        )
    return ranges


def _pivot_levels(previous: Bar, method: str) -> dict[str, float]:
    high = float(previous.high)
    low = float(previous.low)
    close = float(previous.close)
    pivot = (high + low + close) / 3.0
    price_range = high - low

    if method == "standard":
        return {
            "p": pivot,
            "r1": (2.0 * pivot) - low,
            "s1": (2.0 * pivot) - high,
            "r2": pivot + price_range,
            "s2": pivot - price_range,
            "r3": high + (2.0 * (pivot - low)),
            "s3": low - (2.0 * (high - pivot)),
        }

    if method == "fibonacci":
        return {
            "p": pivot,
            "r1": pivot + (0.382 * price_range),
            "s1": pivot - (0.382 * price_range),
            "r2": pivot + (0.618 * price_range),
            "s2": pivot - (0.618 * price_range),
            "r3": pivot + price_range,
            "s3": pivot - price_range,
        }

    return {
        "p": pivot,
        "r1": close + (price_range * 1.1 / 12.0),
        "s1": close - (price_range * 1.1 / 12.0),
        "r2": close + (price_range * 1.1 / 6.0),
        "s2": close - (price_range * 1.1 / 6.0),
        "r3": close + (price_range * 1.1 / 4.0),
        "s3": close - (price_range * 1.1 / 4.0),
        "r4": close + (price_range * 1.1 / 2.0),
        "s4": close - (price_range * 1.1 / 2.0),
    }


def _choice_value(value: Any) -> str:
    if isinstance(value, ChoiceParam):
        return value.value
    return str(value)


def _method_value(value: Any) -> str:
    method = _choice_value(value)
    if method == "classic":
        return "standard"
    return method


def _daily_bar_date(bar: Bar) -> date:
    return bar.timestamp.date()


def _intraday_bar_date(bar: Bar) -> date:
    return bar.timestamp.astimezone(_ET).date()


def _series_key(method: str, session_date: date, level: str) -> str:
    return f"pivot_points_ref_{method}_{session_date.isoformat()}_{level}"


def _level_color(level: str, params: dict[str, Any]) -> str:
    if level.startswith("r"):
        return str(params["resistance_color"])
    if level.startswith("s"):
        return str(params["support_color"])
    return str(params["pivot_color"])
