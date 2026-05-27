from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.indicator_runtime import ChartExtensionRuntime
from app.indicator_store import AppChartExtensionStoreContext, ChartExtensionStore
from app.state import State
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
import tools.vertical_line  # noqa: F401
import tools.fib_retracement  # noqa: F401
from tools.vertical_line.session_store import VerticalLineStore
from tools.fib_retracement.session_store import FibRetracementStore


def test_durable_vertical_line_survives_a_restart(tmp_path: Path) -> None:
    db = str(tmp_path / "t.db")

    # Session 1: draw a vertical line (defaults persist across sessions).
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    with Cache(db) as cache:
        runtime, store = _vertical_line_runtime(state, cache)
        store.load_for_symbol("SPY")
        runtime.start_drawing("vertical_line", _series(), runtime.chart_event(_series(), 2.0, 103.0))

    # Session 2: a fresh state/store/runtime on the same database.
    state2 = State(symbol="SPY", timeframe=Timeframe.DAILY)
    with Cache(db) as cache:
        runtime2, store2 = _vertical_line_runtime(state2, cache)
        store2.load_for_symbol("SPY")
        passes = runtime2.render_all(_series())

    assert len(passes) == 1
    assert len(passes[0].render.vertical_lines) == 1


def test_session_toggle_off_makes_a_vertical_line_volatile(tmp_path: Path) -> None:
    db = str(tmp_path / "t.db")

    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    with Cache(db) as cache:
        runtime, store = _vertical_line_runtime(state, cache)
        store.load_for_symbol("SPY")
        runtime.start_drawing("vertical_line", _series(), runtime.chart_event(_series(), 2.0, 103.0))
        runtime.render_all(_series())

        # Turn off session persistence for the line (durable -> volatile).
        request = runtime.config_request("vertical_line_1")
        assert request is not None
        edited = dict(request.params)
        edited["persist_across_sessions"] = False
        runtime.apply_config("vertical_line_1", edited)
        # Still present this session (volatile records survive within the run).
        assert runtime.render_all(_series())[0].render.vertical_lines

    # Restart: the volatile line is gone.
    state2 = State(symbol="SPY", timeframe=Timeframe.DAILY)
    with Cache(db) as cache:
        runtime2, store2 = _vertical_line_runtime(state2, cache)
        store2.load_for_symbol("SPY")
        passes = runtime2.render_all(_series())

    assert passes == []


def test_timeframe_toggle_off_scopes_line_to_its_timeframe(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    with Cache(str(tmp_path / "t.db")) as cache:
        runtime, store = _vertical_line_runtime(state, cache)
        store.load_for_symbol("SPY")
        runtime.start_drawing("vertical_line", _series(), runtime.chart_event(_series(), 2.0, 103.0))
        runtime.render_all(_series())

        # Pin the line to the timeframe it was drawn on (daily).
        request = runtime.config_request("vertical_line_1")
        assert request is not None
        edited = dict(request.params)
        edited["persist_across_timeframes"] = False
        runtime.apply_config("vertical_line_1", edited)
        assert runtime.render_all(_series())[0].render.vertical_lines  # visible on daily

        # Switch to another timeframe: hidden.
        state.timeframe = Timeframe.MIN5
        assert runtime.render_all(_series()) == []

        # Return to daily: reappears.
        state.timeframe = Timeframe.DAILY
        assert runtime.render_all(_series())[0].render.vertical_lines


def test_fib_drawing_is_volatile_and_gone_after_restart(tmp_path: Path) -> None:
    db = str(tmp_path / "t.db")

    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    with Cache(db) as cache:
        runtime, store = _fib_runtime(state, cache)
        store.load_for_symbol("SPY")
        start = runtime.start_drawing("fib_retracement", _series(), runtime.chart_event(_series(), 1.0, 100.0))
        assert start.session is not None
        runtime.advance_drawing(_series(), start.session, runtime.chart_event(_series(), 3.0, 111.0))
        assert runtime.render_all(_series())[0].render.segments  # present this session

    state2 = State(symbol="SPY", timeframe=Timeframe.DAILY)
    with Cache(db) as cache:
        runtime2, store2 = _fib_runtime(state2, cache)
        store2.load_for_symbol("SPY")
        passes = runtime2.render_all(_series())

    assert passes == []


def _vertical_line_runtime(
    state: State,
    cache: Cache,
) -> tuple[ChartExtensionRuntime, VerticalLineStore]:
    store = VerticalLineStore(AppChartExtensionStoreContext(state, cache))
    runtime = ChartExtensionRuntime(state, cache, ChartExtensionStore(state, cache, [store]), lookback_days=600)
    return runtime, store


def _fib_runtime(
    state: State,
    cache: Cache,
) -> tuple[ChartExtensionRuntime, FibRetracementStore]:
    store = FibRetracementStore(AppChartExtensionStoreContext(state, cache))
    runtime = ChartExtensionRuntime(state, cache, ChartExtensionStore(state, cache, [store]), lookback_days=600)
    return runtime, store


def _series() -> OHLCVSeries:
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    return OHLCVSeries(
        symbol="SPY",
        timeframe=Timeframe.DAILY,
        bars=[
            Bar(start + timedelta(days=i), 100.0 + i, 105.0 + i, 95.0 + i, 100.0 + i, 1_000)
            for i in range(5)
        ],
    )
