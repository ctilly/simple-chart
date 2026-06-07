from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.extension_runtime import ChartExtensionRuntime
from app.extension_store import AppChartExtensionStoreContext, ChartExtensionStore
from app.state import State
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
import tools.horizontal_line  # noqa: F401
from tools.horizontal_line import HorizontalLineIndicator
from tools.horizontal_line.session_store import HorizontalLineStore
from simplechart.api import ChoiceParam, ChartExtensionAddMode


def test_horizontal_line_declares_toolbar_add_mode() -> None:
    assert HorizontalLineIndicator().add_mode() == ChartExtensionAddMode.TOOLBAR


def test_horizontal_line_provides_toolbar_icon() -> None:
    spec = HorizontalLineIndicator().toolbar_icon()
    assert spec is not None
    assert len(spec.lines) == 3


def test_horizontal_line_one_click_commit_renders_line_and_label(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _daily_series()

    with Cache(str(tmp_path / "test.db")) as cache:
        store = HorizontalLineStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")

        result = runtime.start_drawing(
            "horizontal_line",
            series,
            runtime.chart_event(series, 2.0, 103.0),
        )
        render_passes = runtime.render_all(series)

    assert result.done
    assert result.deactivate_tool
    assert state.get_extension("horizontal_line") is not None
    assert len(render_passes) == 1

    rendered_line = render_passes[0].render.horizontal_lines[0]
    rendered_label = render_passes[0].render.axis_price_labels[0]
    assert rendered_line.key == "horizontal_line_1"
    assert rendered_line.y_value == 103.0
    assert rendered_label.key == "horizontal_line_1"
    assert rendered_label.y_value == 103.0
    assert rendered_label.text == "103.00"
    assert rendered_label.fill_color == rendered_line.color


def test_horizontal_line_renders_same_price_across_timeframes(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    daily = _daily_series()
    intraday = _intraday_series(daily.bars[2].timestamp)

    with Cache(str(tmp_path / "test.db")) as cache:
        store = HorizontalLineStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        runtime.start_drawing(
            "horizontal_line",
            daily,
            runtime.chart_event(daily, 2.0, 103.0),
        )
        render_pass = runtime.render_all(intraday)[0]

    assert render_pass.render.horizontal_lines[0].y_value == 103.0
    assert render_pass.render.axis_price_labels[0].y_value == 103.0


def test_horizontal_line_hit_test_drag_config_and_remove(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _daily_series()

    with Cache(str(tmp_path / "test.db")) as cache:
        store = HorizontalLineStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        runtime.start_drawing(
            "horizontal_line",
            series,
            runtime.chart_event(series, 1.0, 100.0),
        )
        runtime.render_all(series)

        # Hit-test: cursor 0.3 price units above the line. Tests use
        # pixel_size_y=0, so the tool falls back to a 0.5 price-unit buffer.
        hit = runtime.drawing_hit_test(series, runtime.chart_event(series, 1.0, 100.3))
        assert hit is not None
        assert hit.handle_key == "horizontal_line_1"

        # Hit-test miss: well outside the buffer.
        miss = runtime.drawing_hit_test(series, runtime.chart_event(series, 1.0, 200.0))
        assert miss is None

        assert runtime.begin_drag(series, runtime.chart_event(series, 1.0, 100.0))
        runtime.finish_drag(series, runtime.chart_event(series, 1.0, 115.0))
        dragged_line = runtime.render_all(series)[0].render.horizontal_lines[0]
        dragged_label = runtime.render_all(series)[0].render.axis_price_labels[0]
        assert dragged_line.y_value == 115.0
        assert dragged_label.y_value == 115.0
        assert dragged_label.text == "115.00"

        request = runtime.config_request("horizontal_line_1")
        assert request is not None
        edited = dict(request.params)
        edited["color"] = "#ff0000"
        edited["line_width"] = 2.5
        edited["line_style"] = ChoiceParam("dash", ["solid", "dash", "dot", "dash_dot"])
        runtime.apply_config("horizontal_line_1", edited)
        configured_line = runtime.render_all(series)[0].render.horizontal_lines[0]
        configured_label = runtime.render_all(series)[0].render.axis_price_labels[0]
        assert configured_line.color == "#ff0000"
        assert configured_line.line_width == 2.5
        assert configured_line.line_style == "dash"
        # Label fill tracks the line color via the tool's render logic.
        assert configured_label.fill_color == "#ff0000"

        removal = runtime.remove("horizontal_line_1")

    assert removal is not None
    assert "horizontal_line_1" in removal.series_keys
    assert state.get_extension("horizontal_line") is None


def test_horizontal_line_config_accepts_in_range_price(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _daily_series()

    with Cache(str(tmp_path / "test.db")) as cache:
        store = HorizontalLineStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        runtime.start_drawing(
            "horizontal_line",
            series,
            runtime.chart_event(series, 1.0, 100.0),
        )
        runtime.render_all(series)

        request = runtime.config_request("horizontal_line_1")
        assert request is not None
        assert request.params["price"].value == 100.0
        assert request.params["price"].step == 0.01

        edited = dict(request.params)
        edited["price"] = 120.0
        runtime.apply_config("horizontal_line_1", edited, y_range=(50.0, 200.0))
        moved = runtime.render_all(series)[0].render.horizontal_lines[0]

    assert moved.y_value == 120.0


def test_horizontal_line_config_accepts_price_outside_view_range(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _daily_series()

    with Cache(str(tmp_path / "test.db")) as cache:
        store = HorizontalLineStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        runtime.start_drawing(
            "horizontal_line",
            series,
            runtime.chart_event(series, 1.0, 100.0),
        )
        runtime.render_all(series)

        request = runtime.config_request("horizontal_line_1")
        assert request is not None

        edited = dict(request.params)
        edited["price"] = 500.0
        runtime.apply_config("horizontal_line_1", edited, y_range=(50.0, 200.0))
        moved = runtime.render_all(series)[0].render.horizontal_lines[0]

    assert moved.y_value == 500.0


def test_horizontal_line_config_no_y_range_accepts_any_price(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _daily_series()

    with Cache(str(tmp_path / "test.db")) as cache:
        store = HorizontalLineStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        runtime.start_drawing(
            "horizontal_line",
            series,
            runtime.chart_event(series, 1.0, 100.0),
        )
        runtime.render_all(series)

        request = runtime.config_request("horizontal_line_1")
        assert request is not None

        edited = dict(request.params)
        edited["price"] = 99999.0
        runtime.apply_config("horizontal_line_1", edited)
        moved = runtime.render_all(series)[0].render.horizontal_lines[0]

    assert moved.y_value == 99999.0


def _daily_series() -> OHLCVSeries:
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    return OHLCVSeries(
        symbol="SPY",
        timeframe=Timeframe.DAILY,
        bars=[
            Bar(
                timestamp=start + timedelta(days=i),
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.0 + i,
                volume=1_000,
            )
            for i in range(5)
        ],
    )


def _intraday_series(day_start: datetime) -> OHLCVSeries:
    return OHLCVSeries(
        symbol="SPY",
        timeframe=Timeframe.MIN5,
        bars=[
            Bar(
                timestamp=day_start + timedelta(minutes=5 * i),
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.0 + i,
                volume=1_000,
            )
            for i in range(3)
        ],
    )


def _runtime(
    state: State,
    cache: Cache,
    store: HorizontalLineStore,
) -> ChartExtensionRuntime:
    return ChartExtensionRuntime(
        state,
        cache,
        ChartExtensionStore(state, cache, [store]),
        lookback_days=600,
    )
