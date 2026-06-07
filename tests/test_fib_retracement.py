from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.extension_runtime import ChartExtensionRuntime
from app.extension_store import AppChartExtensionStoreContext, ChartExtensionStore
from app.state import State
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
import tools.fib_retracement  # noqa: F401
from tools.fib_retracement import FibonacciRetracementIndicator
from tools.fib_retracement.session_store import FibRetracementStore
from simplechart.api import ChoiceParam, ChartExtensionAddMode


def test_fib_declares_toolbar_add_mode() -> None:
    assert FibonacciRetracementIndicator().add_mode() == ChartExtensionAddMode.TOOLBAR


def test_fib_provides_toolbar_icon() -> None:
    spec = FibonacciRetracementIndicator().toolbar_icon()
    assert spec is not None
    assert len(spec.lines) == 4


def test_fib_preview_upward_swing_wick_levels() -> None:
    indicator = FibonacciRetracementIndicator()
    series = _series()
    start = indicator.start_drawing(
        series,
        indicator.default_params(),
        _event(series, 1, 100.0),
    )
    assert start.session is not None

    render = indicator.preview_drawing(series, start.session, _event(series, 3, 111.0))

    assert render is not None
    assert [segment.y_value for segment in render.segments] == [
        111.0,
        107.46,
        105.27,
        103.5,
        101.73,
        96.0,
    ]
    assert render.markers[0].text == "0%"
    assert render.markers[-2].text == "S"
    assert render.markers[-1].text == "E"


def test_fib_preview_keys_stay_stable_across_pointer_bars() -> None:
    indicator = FibonacciRetracementIndicator()
    series = _series()
    start = indicator.start_drawing(
        series,
        indicator.default_params(),
        _event(series, 1, 100.0),
    )
    assert start.session is not None

    first = indicator.preview_drawing(series, start.session, _event(series, 3, 111.0))
    second = indicator.preview_drawing(series, start.session, _event(series, 4, 90.0))

    assert first is not None
    assert second is not None
    assert [segment.key for segment in first.segments] == [
        segment.key for segment in second.segments
    ]
    assert [marker.key for marker in first.markers] == [
        marker.key for marker in second.markers
    ]


def test_fib_preview_updates_continuously_with_pointer_y_on_same_bar() -> None:
    indicator = FibonacciRetracementIndicator()
    series = _series()
    start = indicator.start_drawing(
        series,
        indicator.default_params(),
        _event(series, 1, 100.0),
    )
    assert start.session is not None

    first = indicator.preview_drawing(series, start.session, _event(series, 3, 111.0))
    second = indicator.preview_drawing(series, start.session, _event(series, 3, 109.0))

    assert first is not None
    assert second is not None
    assert first.segments[0].y_value == 111.0
    assert second.segments[0].y_value == 109.0


def test_fib_preview_redraws_when_pointer_crosses_start_price() -> None:
    indicator = FibonacciRetracementIndicator()
    series = _series()
    start = indicator.start_drawing(
        series,
        indicator.default_params(),
        _event(series, 1, 100.0),
    )
    assert start.session is not None

    upward = indicator.preview_drawing(series, start.session, _event(series, 3, 111.0))
    downward = indicator.preview_drawing(series, start.session, _event(series, 3, 90.0))

    assert upward is not None
    assert downward is not None
    assert upward.segments[0].y_value == 111.0
    assert downward.segments[0].y_value == 90.0


def test_fib_preview_downward_swing_wick_flips_direction() -> None:
    indicator = FibonacciRetracementIndicator()
    series = _series()
    start = indicator.start_drawing(
        series,
        indicator.default_params(),
        _event(series, 3, 111.0),
    )
    assert start.session is not None

    render = indicator.preview_drawing(series, start.session, _event(series, 4, 90.0))

    assert render is not None
    assert render.segments[0].y_value == 90.0
    assert render.segments[-1].y_value == 111.0


def test_fib_commit_deactivates_and_scopes_to_exact_timeframe(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _series()

    with Cache(str(tmp_path / "test.db")) as cache:
        store = FibRetracementStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        start = runtime.start_drawing(
            "fib_retracement",
            series,
            runtime.chart_event(series, 1.0, 100.0),
        )
        assert start.session is not None
        result = runtime.advance_drawing(
            series,
            start.session,
            runtime.chart_event(series, 3.0, 111.0),
        )
        daily_render = runtime.render_all(series)[0].render
        intraday_render = runtime.render_all(_intraday_series(series.bars[1].timestamp)).pop().render

    assert result.done
    assert result.deactivate_tool
    assert len(daily_render.segments) == 6
    assert intraday_render.segments == []


def test_fib_drag_preview_uses_base_key_from_level_hit(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _series()

    with Cache(str(tmp_path / "test.db")) as cache:
        store = FibRetracementStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        start = runtime.start_drawing(
            "fib_retracement",
            series,
            runtime.chart_event(series, 1.0, 100.0),
        )
        assert start.session is not None
        runtime.advance_drawing(series, start.session, runtime.chart_event(series, 3.0, 111.0))
        runtime.render_all(series)

        request = runtime.config_request("fib_retracement_ref_-1")
        assert request is not None
        edited = dict(request.params)
        edited["show_0_0"] = False
        runtime.apply_config("fib_retracement_ref_-1", edited)
        runtime.render_all(series)

        hit = runtime.drawing_hit_test(series, runtime.chart_event(series, 3.0, 107.46))
        assert hit is not None
        assert hit.handle_key == "fib_retracement_ref_-1_ref_236"
        assert runtime.begin_drag(series, runtime.chart_event(series, 3.0, 107.46))
        render_pass = runtime.drag_to(series, runtime.chart_event(series, 4.0, 90.0))
        runtime.finish_drag(series, runtime.chart_event(series, 4.0, 90.0))
        dragged = runtime.render_all(series)[0].render

    assert render_pass is not None
    assert {segment.key for segment in render_pass.render.segments} == {
        "fib_retracement_ref_-1_ref_236",
        "fib_retracement_ref_-1_ref_382",
        "fib_retracement_ref_-1_ref_500",
        "fib_retracement_ref_-1_ref_618",
        "fib_retracement_ref_-1_ref_1000",
    }
    assert [round(segment.y_value, 2) for segment in dragged.segments] == [
        93.78,
        96.11,
        98.0,
        99.89,
        106.0,
    ]


def test_fib_close_mode_config_drag_and_remove(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _series()

    with Cache(str(tmp_path / "test.db")) as cache:
        store = FibRetracementStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        start = runtime.start_drawing(
            "fib_retracement",
            series,
            runtime.chart_event(series, 1.0, 100.0),
        )
        assert start.session is not None
        runtime.advance_drawing(series, start.session, runtime.chart_event(series, 3.0, 111.0))
        runtime.render_all(series)

        request = runtime.config_request("fib_retracement_ref_-1")
        assert request is not None
        edited = dict(request.params)
        edited["anchor_price_mode"] = ChoiceParam("close", ["swing_wick", "close"])
        edited["color"] = "#ff0000"
        edited["line_width"] = 2.0
        edited["line_style"] = ChoiceParam("dash", ["solid", "dash", "dot", "dash_dot"])
        edited["show_price_labels"] = True
        edited["show_23_6"] = False
        runtime.apply_config("fib_retracement_ref_-1", edited)
        configured = runtime.render_all(series)[0].render
        assert [segment.y_value for segment in configured.segments] == [106.0, 104.09, 103.5, 102.91, 101.0]
        assert configured.segments[0].color == "#ff0000"
        assert configured.segments[0].line_width == 2.0
        assert configured.segments[0].line_style == "dash"
        assert configured.markers[0].text == "0% 106.00"

        hit = runtime.drawing_hit_test(series, runtime.chart_event(series, 3.0, 104.0))
        assert hit is not None
        assert hit.handle_key == "fib_retracement_ref_-1"

        assert runtime.begin_drag(series, runtime.chart_event(series, 3.0, 106.0))
        runtime.finish_drag(series, runtime.chart_event(series, 4.0, 90.0))
        dragged = runtime.render_all(series)[0].render
        assert dragged.segments[0].y_value == 89.0
        assert dragged.segments[-1].y_value == 101.0

        removal = runtime.remove("fib_retracement_ref_-1")

    assert removal is not None
    assert state.get_extension("fib_retracement") is None


def test_fib_zero_height_drawing_stacks_levels() -> None:
    indicator = FibonacciRetracementIndicator()
    series = _flat_series()
    start = indicator.start_drawing(
        series,
        indicator.default_params(),
        _event(series, 0, 100.0),
    )
    assert start.session is not None

    render = indicator.preview_drawing(series, start.session, _event(series, 1, 100.0))

    assert render is not None
    assert {segment.y_value for segment in render.segments} == {100.0}


def test_fib_levels_stay_out_of_the_legend() -> None:
    # Every fib segment must be reference=True so it is excluded from the legend.
    # A reference=False level would reintroduce per-drag-frame legend restyling.
    indicator = FibonacciRetracementIndicator()
    series = _series()
    start = indicator.start_drawing(
        series,
        indicator.default_params(),
        _event(series, 1, 100.0),
    )
    assert start.session is not None
    render = indicator.preview_drawing(series, start.session, _event(series, 3, 111.0))
    assert render is not None
    assert render.segments
    assert all(segment.reference for segment in render.segments)


def _series() -> OHLCVSeries:
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    bars = [
        Bar(start, 100.0, 105.0, 95.0, 100.0, 1_000),
        Bar(start + timedelta(days=1), 101.0, 106.0, 96.0, 101.0, 1_000),
        Bar(start + timedelta(days=2), 102.0, 107.0, 97.0, 102.0, 1_000),
        Bar(start + timedelta(days=3), 106.0, 111.0, 101.0, 106.0, 1_000),
        Bar(start + timedelta(days=4), 89.0, 94.0, 90.0, 89.0, 1_000),
    ]
    return OHLCVSeries(symbol="SPY", timeframe=Timeframe.DAILY, bars=bars)


def _flat_series() -> OHLCVSeries:
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    bars = [
        Bar(start + timedelta(days=i), 100.0, 100.0, 100.0, 100.0, 1_000)
        for i in range(2)
    ]
    return OHLCVSeries(symbol="SPY", timeframe=Timeframe.DAILY, bars=bars)


def _intraday_series(day_start: datetime) -> OHLCVSeries:
    return OHLCVSeries(
        symbol="SPY",
        timeframe=Timeframe.MIN5,
        bars=[
            Bar(day_start + timedelta(minutes=5 * i), 100.0, 101.0, 99.0, 100.0, 1_000)
            for i in range(3)
        ],
    )


def _event(series: OHLCVSeries, index: int, y: float) -> Any:
    bar = series.bars[index]
    return type("Event", (), {
        "x": float(index),
        "y": y,
        "bar_index": index,
        "timestamp_ms": int(bar.timestamp.timestamp() * 1000),
    })()


def _runtime(
    state: State,
    cache: Cache,
    store: FibRetracementStore,
) -> ChartExtensionRuntime:
    return ChartExtensionRuntime(
        state,
        cache,
        ChartExtensionStore(state, cache, [store]),
        lookback_days=600,
    )
