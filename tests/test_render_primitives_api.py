from dataclasses import fields
from datetime import datetime, timedelta, timezone

import numpy as np

from data.models import Bar, OHLCVSeries, Timeframe
import indicators.rsi  # noqa: F401
from simplechart.extensions._base import (
    AxisPriceLabelRender,
    ChartEvent,
    ChartExtension,
    ChartExtensionAction,
    ChartExtensionAddMode,
    ChartExtensionConfig,
    ChartExtensionMutation,
    ChartExtensionRender,
    DragSession,
    DrawingSession,
    DrawingToolResult,
    FloatParam,
    HitTestResult,
    HorizontalLineRender,
    HorizontalSegmentRender,
    MarkerRender,
    SeriesRender,
    ToolIconLine,
    ToolIconSpec,
    VerticalLineRender,
)
from simplechart.extensions._registry import register_extension
from simplechart.extensions._store_registry import register_store_handler
from simplechart.api import (
    AxisPriceLabelRender as PublicAxisPriceLabelRender,
    ChartEvent as PublicChartEvent,
    ChartExtension as PublicChartExtension,
    ChartExtensionAction as PublicChartExtensionAction,
    ChartExtensionAddMode as PublicChartExtensionAddMode,
    ChartExtensionConfig as PublicChartExtensionConfig,
    ChartExtensionMutation as PublicChartExtensionMutation,
    ChartExtensionRender as PublicChartExtensionRender,
    DrawingSession as PublicDrawingSession,
    DrawingToolResult as PublicDrawingToolResult,
    DragSession as PublicDragSession,
    FloatParam as PublicFloatParam,
    HitTestResult as PublicHitTestResult,
    HorizontalLineRender as PublicHorizontalLineRender,
    HorizontalSegmentRender as PublicHorizontalSegmentRender,
    MarkerRender as PublicMarkerRender,
    SeriesRender as PublicSeriesRender,
    ToolIconLine as PublicToolIconLine,
    ToolIconSpec as PublicToolIconSpec,
    VerticalLineRender as PublicVerticalLineRender,
    all_extensions as public_all_extensions,
    get_extension as public_get_extension,
    register_extension as public_register_extension,
    register_store_handler as public_register_store_handler,
)


def test_render_primitives_are_public_api() -> None:
    assert PublicChartExtension is ChartExtension
    assert PublicChartExtensionRender is ChartExtensionRender
    assert PublicChartExtensionAddMode is ChartExtensionAddMode
    assert PublicChartExtensionAction is ChartExtensionAction
    assert PublicChartExtensionConfig is ChartExtensionConfig
    assert PublicChartExtensionMutation is ChartExtensionMutation
    assert PublicAxisPriceLabelRender is AxisPriceLabelRender
    assert PublicHorizontalLineRender is HorizontalLineRender
    assert PublicHorizontalSegmentRender is HorizontalSegmentRender
    assert PublicMarkerRender is MarkerRender
    assert PublicSeriesRender is SeriesRender
    assert PublicToolIconLine is ToolIconLine
    assert PublicToolIconSpec is ToolIconSpec
    assert PublicVerticalLineRender is VerticalLineRender
    assert PublicChartEvent is ChartEvent
    assert PublicDrawingSession is DrawingSession
    assert PublicDrawingToolResult is DrawingToolResult
    assert PublicDragSession is DragSession
    assert PublicFloatParam is FloatParam
    assert PublicHitTestResult is HitTestResult
    assert public_register_extension is register_extension
    assert public_get_extension("rsi").name() == "rsi"
    assert "rsi" in public_all_extensions()
    assert public_register_store_handler is register_store_handler


def test_marker_render_is_price_chart_only() -> None:
    field_names = {field.name for field in fields(MarkerRender)}

    assert "render_target" not in field_names


class _NoOpIndicator(ChartExtension):

    def name(self) -> str:
        return "noop"

    def label(self) -> str:
        return "Noop"

    def default_params(self) -> dict[str, object]:
        return {}

    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, object],
    ) -> dict[str, np.ndarray]:
        return {}


def test_drawing_lifecycle_defaults_are_noops() -> None:
    indicator = _NoOpIndicator()
    series = _series()
    event = ChartEvent(x=1.0, y=100.0, bar_index=1, timestamp_ms=1_700_086_400_000)
    session = DrawingSession(
        extension_name="noop",
        tool_key="noop",
        original_params={},
        working_params={},
    )

    assert indicator.start_drawing(series, {}, event) == DrawingToolResult()
    assert indicator.preview_drawing(series, session, event) is None
    result = indicator.advance_drawing(series, session, event)
    assert result == DrawingToolResult(session=session)
    assert indicator.cancel_drawing(session) is None


def _series() -> OHLCVSeries:
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    bars = [
        Bar(
            timestamp=start + timedelta(days=i),
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.0 + i,
            volume=1_000,
        )
        for i in range(20)
    ]
    return OHLCVSeries(symbol="SPY", timeframe=Timeframe.DAILY, bars=bars)
