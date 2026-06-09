from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.extension_runtime import ChartExtensionRuntime
from app.extension_store import AppChartExtensionStoreContext, ChartExtensionStore
from app.state import State
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
import tools.poly_line  # noqa: F401
from tools.poly_line import PolyLineTool
from tools.poly_line.session_store import PolyLineStore
from simplechart.api import ChartExtensionAddMode


def test_poly_line_declares_toolbar_and_icon() -> None:
    tool = PolyLineTool()
    assert tool.add_mode() == ChartExtensionAddMode.TOOLBAR
    assert tool.max_vertices() == 10


def test_clicks_add_vertices_until_enter_commits(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _series()
    with Cache(str(tmp_path / "t.db")) as cache:
        store = PolyLineStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        start = runtime.start_drawing("poly_line", series, runtime.chart_event(series, 0.0, 100.0))
        assert start.session is not None
        # Three more clicks: still in progress, never auto-committing below the cap.
        for index, price in ((2, 110.0), (4, 102.0), (6, 118.0)):
            result = runtime.advance_drawing(series, start.session, runtime.chart_event(series, float(index), price))
            assert not result.done
        commit = runtime.commit_drawing(series, start.session)
        render = runtime.render_all(series)[0].render

    assert commit.done
    assert commit.deactivate_tool
    assert len(render.polylines) == 1
    assert render.polylines[0].points == ((0.0, 100.0), (2.0, 110.0), (4.0, 102.0), (6.0, 118.0))
    assert len(render.markers) == 4


def test_enter_with_single_vertex_cancels(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _series()
    with Cache(str(tmp_path / "t.db")) as cache:
        store = PolyLineStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        start = runtime.start_drawing("poly_line", series, runtime.chart_event(series, 0.0, 100.0))
        assert start.session is not None
        commit = runtime.commit_drawing(series, start.session)
        render = runtime.render_all(series)

    assert commit.cancel
    assert render == [] or render[0].render.polylines == []


def test_tenth_click_auto_commits(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _series(count=12)
    with Cache(str(tmp_path / "t.db")) as cache:
        store = PolyLineStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        start = runtime.start_drawing("poly_line", series, runtime.chart_event(series, 0.0, 100.0))
        assert start.session is not None
        result = None
        for index in range(1, 10):
            result = runtime.advance_drawing(series, start.session, runtime.chart_event(series, float(index), 100.0 + index))
        assert result is not None
        render = runtime.render_all(series)[0].render

    # 1 start + 9 advances = 10 vertices -> auto-commit on the tenth.
    assert result.done
    assert len(render.polylines[0].points) == 10


def _series(count: int = 8) -> OHLCVSeries:
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    bars = [
        Bar(start + timedelta(days=i), 100.0 + i, 105.0 + i, 95.0 + i, 100.0 + i, 1_000)
        for i in range(count)
    ]
    return OHLCVSeries(symbol="SPY", timeframe=Timeframe.DAILY, bars=bars)


def _runtime(state: State, cache: Cache, store: PolyLineStore) -> ChartExtensionRuntime:
    return ChartExtensionRuntime(
        state,
        cache,
        ChartExtensionStore(state, cache, [store]),
        lookback_days=600,
    )
