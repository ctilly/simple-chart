from typing import Any

import numpy as np

from indicators.five_day_marker.store import (
    DEFAULT_COLOR,
    DEFAULT_LINE_STYLE,
    DEFAULT_LINE_WIDTH,
    DEFAULT_VISIBLE,
    MARKER_KEY,
)
from simplechart.api import (
    ChartExtension,
    ChartExtensionAddMode,
    ChartExtensionConfig,
    ChartExtensionMutation,
    ChartExtensionRender,
    ChoiceParam,
    LINE_STYLE_OPTIONS,
    OHLCVSeries,
    VerticalLineRender,
    bars_for_n_days,
    register_extension,
    register_store_handler,
)

_DAYS = 5
_LABEL = "5-Day Marker"


class FiveDayMarkerIndicator(ChartExtension):

    def name(self) -> str:
        return MARKER_KEY

    def label(self) -> str:
        return _LABEL

    def default_params(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "color": DEFAULT_COLOR,
            "line_width": DEFAULT_LINE_WIDTH,
            "line_style": ChoiceParam(DEFAULT_LINE_STYLE, LINE_STYLE_OPTIONS),
            "visible": DEFAULT_VISIBLE,
        }

    def add_mode(self) -> ChartExtensionAddMode:
        return ChartExtensionAddMode.HIDDEN

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
        if not bool(params.get("enabled", True)):
            return ChartExtensionRender()
        marker_index = _marker_index(series)
        if marker_index is None:
            return ChartExtensionRender()
        return ChartExtensionRender(
            vertical_lines=[
                VerticalLineRender(
                    key=MARKER_KEY,
                    x_index=float(marker_index),
                    label=_LABEL,
                    color=str(params["color"]),
                    line_width=float(params["line_width"]),
                    line_style=_line_style_value(params["line_style"]),
                    visible=bool(params.get("visible", True)),
                )
            ]
        )

    def config_for_series(
        self,
        series_key: str,
        params: dict[str, Any],
    ) -> ChartExtensionConfig | None:
        if series_key != MARKER_KEY:
            return None
        return ChartExtensionConfig(
            label=_LABEL,
            params={
                "color": str(params["color"]),
                "line_width": float(params["line_width"]),
                "line_style": ChoiceParam(
                    _line_style_value(params["line_style"]),
                    LINE_STYLE_OPTIONS,
                ),
                "visible": bool(params.get("visible", True)),
            },
        )

    def apply_config_to_series(
        self,
        series_key: str,
        params: dict[str, Any],
        edited_params: dict[str, Any],
        y_range: tuple[float, float] | None = None,
    ) -> ChartExtensionMutation | None:
        if series_key != MARKER_KEY:
            return None
        return ChartExtensionMutation(
            extension_name=MARKER_KEY,
            operation="update_settings",
            payload={
                "color": edited_params["color"],
                "line_width": edited_params["line_width"],
                "line_style": _line_style_value(edited_params["line_style"]),
                "visible": edited_params["visible"],
            },
        )

    def remove_series(
        self,
        series_key: str,
        params: dict[str, Any],
    ) -> ChartExtensionMutation | None:
        if series_key != MARKER_KEY:
            return None
        return ChartExtensionMutation(
            extension_name=MARKER_KEY,
            operation="disable",
            payload={},
        )


register_extension(FiveDayMarkerIndicator)


def _marker_index(series: OHLCVSeries) -> int | None:
    if series.timeframe.value == "weekly":
        return None
    period = bars_for_n_days(_DAYS, series.timeframe)
    marker_index = len(series.bars) - period
    if marker_index < 0:
        return None
    return marker_index


def _line_style_value(value: Any) -> str:
    if isinstance(value, ChoiceParam):
        return value.value
    return str(value)


from indicators.five_day_marker.store import FiveDayMarkerStore  # noqa: E402

register_store_handler(FiveDayMarkerStore)
