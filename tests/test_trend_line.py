from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.extension_runtime import ChartExtensionRuntime
from app.extension_store import AppChartExtensionStoreContext, ChartExtensionStore
from app.state import State
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
import tools.trend_line  # noqa: F401
from tools.trend_line import TrendLineTool
from tools.trend_line.session_store import TrendLineStore
from simplechart.api import ChartExtensionAddMode, ChoiceParam, FloatParam


def test_trend_line_declares_toolbar_and_icon() -> None:
    tool = TrendLineTool()
    assert tool.add_mode() == ChartExtensionAddMode.TOOLBAR
    assert tool.toolbar_icon() is not None


def test_preview_draws_diagonal_to_cursor() -> None:
    tool = TrendLineTool()
    series = _series()
    start = tool.start_drawing(series, tool.default_params(), _event(1, 100.0))
    assert start.session is not None

    render = tool.preview_drawing(series, start.session, _event(3, 111.0))

    assert render is not None
    assert len(render.polylines) == 1
    assert render.polylines[0].points == ((1.0, 100.0), (3.0, 111.0))
    assert render.polylines[0].reference is True
    # One handle marker for the single placed vertex; the live cursor has none.
    assert len(render.markers) == 1


def test_second_click_commits_and_deactivates(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _series()
    with Cache(str(tmp_path / "t.db")) as cache:
        store = TrendLineStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        start = runtime.start_drawing("trend_line", series, runtime.chart_event(series, 1.0, 100.0))
        assert start.session is not None
        result = runtime.advance_drawing(series, start.session, runtime.chart_event(series, 3.0, 111.0))
        render = runtime.render_all(series)[0].render

    assert result.done
    assert result.deactivate_tool
    assert len(render.polylines) == 1
    assert render.polylines[0].points == ((1.0, 100.0), (3.0, 111.0))


def test_vertex_drag_moves_only_that_endpoint(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _series()
    with Cache(str(tmp_path / "t.db")) as cache:
        store = TrendLineStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        start = runtime.start_drawing("trend_line", series, runtime.chart_event(series, 1.0, 100.0))
        assert start.session is not None
        runtime.advance_drawing(series, start.session, runtime.chart_event(series, 3.0, 111.0))
        runtime.render_all(series)

        # Grab the end vertex (bar 3, price 111) and drag it to bar 4, price 90.
        hit = runtime.drawing_hit_test(series, runtime.chart_event(series, 3.0, 111.0))
        assert hit is not None
        assert hit.handle_key == "trend_line_1__vtx1"
        assert runtime.begin_drag(series, runtime.chart_event(series, 3.0, 111.0))
        runtime.drag_to(series, runtime.chart_event(series, 4.0, 90.0))
        runtime.finish_drag(series, runtime.chart_event(series, 4.0, 90.0))
        render = runtime.render_all(series)[0].render

    assert render.polylines[0].points == ((1.0, 100.0), (4.0, 90.0))


def test_body_drag_translates_whole_line(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _series()
    with Cache(str(tmp_path / "t.db")) as cache:
        store = TrendLineStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        start = runtime.start_drawing("trend_line", series, runtime.chart_event(series, 0.0, 100.0))
        assert start.session is not None
        runtime.advance_drawing(series, start.session, runtime.chart_event(series, 2.0, 110.0))
        runtime.render_all(series)

        # Grab the body midpoint and drag +1 bar / +5 price. Realistic pixel
        # sizes keep the midpoint clear of the endpoint handles (as on screen).
        grab = runtime.chart_event(series, 1.0, 105.0, pixel_size_x=0.05, pixel_size_y=0.2)
        hit = runtime.drawing_hit_test(series, grab)
        assert hit is not None
        assert hit.handle_key == "trend_line_1__body"
        assert runtime.begin_drag(series, grab)
        # The app feeds the grab point as the first drag_to, then the destination.
        runtime.drag_to(series, runtime.chart_event(series, 1.0, 105.0))
        runtime.drag_to(series, runtime.chart_event(series, 2.0, 110.0))
        runtime.finish_drag(series, runtime.chart_event(series, 2.0, 110.0))
        render = runtime.render_all(series)[0].render

    assert render.polylines[0].points == ((1.0, 105.0), (3.0, 115.0))


def test_line_projects_past_last_bar(tmp_path: Path) -> None:
    # The chart has 5 bars (indices 0-4). Ending the line at x=6.0 places the
    # second vertex two bars into the empty future space; it must stay there and
    # not snap back onto the last bar.
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _series()
    with Cache(str(tmp_path / "t.db")) as cache:
        store = TrendLineStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        start = runtime.start_drawing("trend_line", series, runtime.chart_event(series, 1.0, 100.0))
        assert start.session is not None
        result = runtime.advance_drawing(series, start.session, runtime.chart_event(series, 6.0, 120.0))
        render = runtime.render_all(series)[0].render

    assert result.done
    assert render.polylines[0].points == ((1.0, 100.0), (6.0, 120.0))
    # The end-vertex handle marker tracks the projected point too.
    assert any(m.x_index == 6.0 for m in render.markers)


def test_body_drag_into_future(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _series()
    with Cache(str(tmp_path / "t.db")) as cache:
        store = TrendLineStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        start = runtime.start_drawing("trend_line", series, runtime.chart_event(series, 0.0, 100.0))
        assert start.session is not None
        runtime.advance_drawing(series, start.session, runtime.chart_event(series, 2.0, 110.0))
        runtime.render_all(series)

        grab = runtime.chart_event(series, 1.0, 105.0, pixel_size_x=0.05, pixel_size_y=0.2)
        hit = runtime.drawing_hit_test(series, grab)
        assert hit is not None and hit.handle_key == "trend_line_1__body"
        assert runtime.begin_drag(series, grab)
        # Grab point first, then translate +4 bars so both vertices land past
        # the last bar (index 4).
        runtime.drag_to(series, runtime.chart_event(series, 1.0, 105.0))
        runtime.drag_to(series, runtime.chart_event(series, 5.0, 105.0))
        runtime.finish_drag(series, runtime.chart_event(series, 5.0, 105.0))
        render = runtime.render_all(series)[0].render

    assert render.polylines[0].points == ((4.0, 100.0), (6.0, 110.0))


def test_configure_and_remove(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _series()
    with Cache(str(tmp_path / "t.db")) as cache:
        store = TrendLineStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        start = runtime.start_drawing("trend_line", series, runtime.chart_event(series, 1.0, 100.0))
        assert start.session is not None
        runtime.advance_drawing(series, start.session, runtime.chart_event(series, 3.0, 111.0))
        runtime.render_all(series)

        request = runtime.config_request("trend_line_1")
        assert request is not None
        assert request.params["persist_across_timeframes"] is True
        assert isinstance(request.params["age_off_days"], FloatParam)
        edited = dict(request.params)
        edited["color"] = "#ff0000"
        edited["line_width"] = 2.0
        edited["line_style"] = ChoiceParam("dash", ["solid", "dash", "dot", "dash_dot"])
        edited["persist_across_timeframes"] = False
        runtime.apply_config("trend_line_1", edited)
        configured = runtime.render_all(series)[0].render
        assert configured.polylines[0].color == "#ff0000"
        assert configured.polylines[0].line_width == 2.0
        assert configured.polylines[0].line_style == "dash"

        # persist_across_timeframes now False -> hidden on other timeframes.
        state.timeframe = Timeframe.MIN5
        intraday = runtime.render_all(_intraday_series(series.bars[1].timestamp))
        assert intraday == [] or intraday[0].render.polylines == []

        # Back on the creation timeframe it returns, and can be removed.
        state.timeframe = Timeframe.DAILY
        runtime.render_all(series)
        removal = runtime.remove("trend_line_1")

    assert removal is not None
    assert state.get_extension("trend_line") is None


def _series() -> OHLCVSeries:
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    bars = [
        Bar(start + timedelta(days=i), 100.0 + i, 105.0 + i, 95.0 + i, 100.0 + i, 1_000)
        for i in range(5)
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


def _event(index: int, y: float) -> Any:
    ts = int((datetime(2026, 1, 2, tzinfo=timezone.utc) + timedelta(days=index)).timestamp() * 1000)
    return type("Event", (), {
        "x": float(index),
        "y": y,
        "bar_index": index,
        "timestamp_ms": ts,
        "button": "left",
        "pixel_size_x": 0.0,
        "pixel_size_y": 0.0,
    })()


def _runtime(state: State, cache: Cache, store: TrendLineStore) -> ChartExtensionRuntime:
    return ChartExtensionRuntime(
        state,
        cache,
        ChartExtensionStore(state, cache, [store]),
        lookback_days=600,
    )
