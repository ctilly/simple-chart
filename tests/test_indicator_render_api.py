from datetime import datetime, timedelta, timezone

import numpy as np

from data.models import Bar, OHLCVSeries, Timeframe
from indicators.avwap import (
    AVWAPIndicator,
    avwap_anchor_for_key,
    avwap_anchor_key,
)
from indicators.avwap.models import AnchorRecord
from indicators.pivot_points import PivotPointsIndicator
from simplechart.extensions._base import (
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
    HitTestResult,
    HorizontalSegmentRender,
    MarkerRender,
    SeriesRender,
    VerticalLineRender,
)
from simplechart.extensions._store_registry import register_store_handler
from indicators.rsi import RSIIndicator
from simplechart.extensions._registry import register_extension
from simplechart.api import (
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
    HorizontalSegmentRender as PublicHorizontalSegmentRender,
    HitTestResult as PublicHitTestResult,
    MarkerRender as PublicMarkerRender,
    SeriesRender as PublicSeriesRender,
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
    assert PublicHorizontalSegmentRender is HorizontalSegmentRender
    assert PublicMarkerRender is MarkerRender
    assert PublicSeriesRender is SeriesRender
    assert PublicVerticalLineRender is VerticalLineRender
    assert PublicChartEvent is ChartEvent
    assert PublicDrawingSession is DrawingSession
    assert PublicDrawingToolResult is DrawingToolResult
    assert PublicDragSession is DragSession
    assert PublicHitTestResult is HitTestResult
    assert public_register_extension is register_extension
    assert public_get_extension("rsi").name() == "rsi"
    assert "rsi" in public_all_extensions()
    assert public_register_store_handler is register_store_handler


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


def test_legacy_compute_output_adapts_to_indicator_render() -> None:
    indicator = RSIIndicator()
    render = indicator.render(_series(), indicator.default_params())

    assert isinstance(render, ChartExtensionRender)
    assert render.markers == []
    assert [series.key for series in render.series] == [
        "rsi_14",
        "rsi_14_ref_overbought",
        "rsi_14_ref_oversold",
    ]
    assert render.series[0].label == "rsi_14"
    assert render.series[0].color == "#DA70D6"
    assert render.series[0].line_width == 1.0
    assert render.series[0].line_style == "solid"
    assert render.series[0].render_target == "rsi"
    assert render.series[1].color == "#AAAAAA"
    assert render.series[1].line_width == 0.8
    assert render.series[1].line_style == "dash"
    assert np.array_equal(render.series[1].values, np.full(20, 70.0))


def test_rsi_compute_uses_wilder_smoothing_kernel() -> None:
    result = RSIIndicator().compute(_series(), RSIIndicator().default_params())
    values = result["rsi_14"]

    assert np.all(np.isnan(values[:14]))
    assert values[14] == 100.0
    assert values[-1] == 100.0


def test_avwap_render_owns_anchor_style_label_and_marker() -> None:
    series = _series()
    anchor_ts = int(series.bars[2].timestamp.timestamp() * 1000)
    anchor = AnchorRecord(
        symbol="SPY",
        anchor_ts=anchor_ts,
        label="2026-01-04",
        color="#00FF88",
        line_width=2.5,
        line_style="dash",
        show_anchor=True,
        anchor_id=123,
    )

    render = AVWAPIndicator().render(series, {"anchors": [anchor]})

    assert len(render.series) == 1
    assert render.series[0].key == "avwap_anchor_123"
    assert render.series[0].label == "AVWAP 2026-01-04"
    assert render.series[0].color == "#00FF88"
    assert render.series[0].line_width == 2.5
    assert render.series[0].line_style == "dash"
    assert len(render.markers) == 1
    assert render.markers[0].key == "avwap_anchor_123"
    assert render.markers[0].x_index == 2
    assert render.markers[0].text == "⚓️"
    assert render.markers[0].color == "#00FF88"


def test_pivot_points_render_standard_horizontal_segments() -> None:
    indicator = PivotPointsIndicator()
    params = indicator.default_params()
    render = indicator.render(_series(), params)

    assert len(render.series) == 0
    assert len(render.segments) == 21
    assert len(render.markers) == 21
    assert render.segments[0].key == "pivot_points"
    assert render.segments[0].label == "Pivot Points"
    assert render.segments[0].x_start == 16.55
    assert render.segments[0].x_end == 17.45
    assert render.segments[0].y_value == 119.0
    assert render.segments[0].color == "#f3bc4f"
    assert render.markers[0].key == "pivot_points"
    assert render.markers[0].x_index == 17
    assert render.markers[0].text == "R3"


def test_pivot_points_can_hide_labels() -> None:
    indicator = PivotPointsIndicator()
    params = indicator.default_params()
    params["show_labels"] = False

    render = indicator.render(_series(), params)

    assert len(render.segments) == 21
    assert render.markers == []


def test_pivot_points_intraday_segments_use_daily_pivots() -> None:
    indicator = PivotPointsIndicator()
    params = indicator.default_params()
    daily_bars = [
        Bar(
            timestamp=datetime(2026, 1, day, tzinfo=timezone.utc),
            open=100.0,
            high=110.0,
            low=90.0,
            close=100.0,
            volume=1_000,
        )
        for day in range(2, 6)
    ]
    params["_daily_bars"] = daily_bars
    intraday_bars = [
        Bar(
            timestamp=datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
            + timedelta(minutes=30 * i),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1_000,
        )
        for i in range(4)
    ]
    series = OHLCVSeries(
        symbol="SPY",
        timeframe=Timeframe.MIN30,
        bars=intraday_bars,
    )

    render = indicator.render(series, params)

    assert len(render.segments) == 7
    assert render.segments[0].x_start == 0.0
    assert render.segments[0].x_end == 4.0
    assert render.segments[0].y_value == 130.0


def test_pivot_points_classic_method_alias_uses_standard_levels() -> None:
    indicator = PivotPointsIndicator()
    params = indicator.default_params()
    params["method"] = "classic"

    render = indicator.render(_series(), params)

    assert len(render.segments) == 21
    assert render.segments[0].key == "pivot_points"
    assert render.segments[0].y_value == 119.0
    assert render.markers[0].text == "R3"


def test_avwap_anchor_keys_use_stable_persisted_identity() -> None:
    persisted = AnchorRecord(
        symbol="SPY",
        anchor_ts=1_700_000_000_000,
        label="2023-11-14",
        color="#00FF88",
        anchor_id=321,
    )
    unpersisted = AnchorRecord(
        symbol="SPY",
        anchor_ts=1_700_086_400_000,
        label="2023-11-15",
        color="#00FF88",
    )

    assert avwap_anchor_key(persisted) == "avwap_anchor_321"
    assert avwap_anchor_key(unpersisted) == "avwap_anchor_ts_1700086400000"
    assert avwap_anchor_for_key([persisted, unpersisted], "avwap_anchor_321") is persisted
    assert (
        avwap_anchor_for_key(
            [persisted, unpersisted],
            "avwap_anchor_ts_1700086400000",
        )
        is unpersisted
    )


def test_avwap_hit_test_claims_anchor_bar_near_candle() -> None:
    series = _series()
    anchor_ts = int(series.bars[2].timestamp.timestamp() * 1000)
    anchor = AnchorRecord(
        symbol="SPY",
        anchor_ts=anchor_ts,
        label="2026-01-04",
        color="#00FF88",
        anchor_id=123,
    )
    indicator = AVWAPIndicator()

    hit = indicator.hit_test(
        series,
        {"anchors": [anchor]},
        ChartEvent(x=2.0, y=101.5, bar_index=2, timestamp_ms=anchor_ts),
        {"avwap_anchor_123"},
    )

    assert hit is not None
    assert hit.handle_key == "avwap_anchor_123"


def test_avwap_hit_test_uses_resolved_bar_index_not_raw_x() -> None:
    series = _series()
    anchor_ts = int(series.bars[2].timestamp.timestamp() * 1000)
    anchor = AnchorRecord(
        symbol="SPY",
        anchor_ts=anchor_ts,
        label="2026-01-04",
        color="#00FF88",
        anchor_id=123,
    )

    hit = AVWAPIndicator().hit_test(
        series,
        {"anchors": [anchor]},
        ChartEvent(
            x=series.bars[2].timestamp.timestamp(),
            y=101.5,
            bar_index=2,
            timestamp_ms=anchor_ts,
        ),
        {"avwap_anchor_123"},
    )

    assert hit is not None
    assert hit.handle_key == "avwap_anchor_123"


def test_avwap_hit_test_rejects_empty_space_on_anchor_bar() -> None:
    series = _series()
    anchor_ts = int(series.bars[2].timestamp.timestamp() * 1000)
    anchor = AnchorRecord(
        symbol="SPY",
        anchor_ts=anchor_ts,
        label="2026-01-04",
        color="#00FF88",
        anchor_id=123,
    )

    hit = AVWAPIndicator().hit_test(
        series,
        {"anchors": [anchor]},
        ChartEvent(x=2.0, y=500.0, bar_index=2, timestamp_ms=anchor_ts),
        {"avwap_anchor_123"},
    )

    assert hit is None


def test_avwap_hit_test_keeps_small_candle_target_clickable() -> None:
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    bars = [
        Bar(
            timestamp=start + timedelta(days=i),
            open=100.0,
            high=100.01,
            low=100.0,
            close=100.005,
            volume=1_000,
        )
        for i in range(20)
    ]
    series = OHLCVSeries(symbol="SPY", timeframe=Timeframe.DAILY, bars=bars)
    anchor_ts = int(series.bars[2].timestamp.timestamp() * 1000)
    anchor = AnchorRecord(
        symbol="SPY",
        anchor_ts=anchor_ts,
        label="2026-01-04",
        color="#00FF88",
        anchor_id=123,
    )

    hit = AVWAPIndicator().hit_test(
        series,
        {"anchors": [anchor]},
        ChartEvent(x=2.0, y=100.035, bar_index=2, timestamp_ms=anchor_ts),
        {"avwap_anchor_123"},
    )

    assert hit is not None
    assert hit.handle_key == "avwap_anchor_123"


def test_avwap_declares_context_add_mode() -> None:
    assert AVWAPIndicator().add_mode() == ChartExtensionAddMode.CONTEXT


def test_avwap_opts_out_of_per_symbol_ui_state() -> None:
    assert AVWAPIndicator().preserve_ui_state_per_symbol() is False


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
