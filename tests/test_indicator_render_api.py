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
from indicators.rsi import RSIIndicator
from simplechart.extensions._base import (
    ChartEvent,
    ChartExtensionAddMode,
    ChartExtensionRender,
)


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
