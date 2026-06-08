from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.extension_runtime import ChartExtensionRuntime
from app.extension_store import AppChartExtensionStoreContext, ChartExtensionStore
from app.state import State
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
import tools.vertical_line  # noqa: F401
import tools.fib_retracement  # noqa: F401
from tools.vertical_line.session_store import VerticalLineStore
from tools.fib_retracement.session_store import FibRetracementStore

_DAY_MS = 86_400_000
_VERTICAL_STORE_KEY = "vertical_line.lines"


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


def test_fib_drawing_can_persist_after_restart_when_enabled(tmp_path: Path) -> None:
    db = str(tmp_path / "t.db")

    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    with Cache(db) as cache:
        runtime, store = _fib_runtime(state, cache)
        store.load_for_symbol("SPY")
        start = runtime.start_drawing("fib_retracement", _series(), runtime.chart_event(_series(), 1.0, 100.0))
        assert start.session is not None
        runtime.advance_drawing(_series(), start.session, runtime.chart_event(_series(), 3.0, 111.0))
        runtime.render_all(_series())

        request = runtime.config_request("fib_retracement_ref_-1")
        assert request is not None
        edited = dict(request.params)
        edited["persist_across_sessions"] = True
        runtime.apply_config("fib_retracement_ref_-1", edited)
        assert runtime.render_all(_series())[0].render.segments

    state2 = State(symbol="SPY", timeframe=Timeframe.DAILY)
    with Cache(db) as cache:
        runtime2, store2 = _fib_runtime(state2, cache)
        store2.load_for_symbol("SPY")
        passes = runtime2.render_all(_series())

    assert len(passes) == 1
    assert len(passes[0].render.segments) == 6


def test_expired_durable_tool_record_is_deleted(tmp_path: Path) -> None:
    now_ms = 10 * _DAY_MS
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    with Cache(str(tmp_path / "t.db")) as cache:
        cache.put_extension_record(
            _VERTICAL_STORE_KEY,
            "SPY",
            _series_timestamp(2),
            _vertical_payload(updated_at_ms=now_ms - (2 * _DAY_MS), age_off_days=1.0),
        )
        runtime, store = _vertical_line_runtime(state, cache, now_ms=lambda: now_ms)
        store.load_for_symbol("SPY")
        passes = runtime.render_all(_series())
        records = cache.get_extension_records(_VERTICAL_STORE_KEY, "SPY")

    assert passes == []
    assert records == []
    assert state.get_extension("vertical_line") is None


def test_age_off_zero_never_expires(tmp_path: Path) -> None:
    now_ms = 10 * _DAY_MS
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    with Cache(str(tmp_path / "t.db")) as cache:
        cache.put_extension_record(
            _VERTICAL_STORE_KEY,
            "SPY",
            _series_timestamp(2),
            _vertical_payload(updated_at_ms=1, age_off_days=0.0),
        )
        runtime, store = _vertical_line_runtime(state, cache, now_ms=lambda: now_ms)
        store.load_for_symbol("SPY")
        passes = runtime.render_all(_series())

    assert len(passes) == 1
    assert len(passes[0].render.vertical_lines) == 1


def test_tool_update_resets_age_off_clock(tmp_path: Path) -> None:
    clock = {"now": 1_000}
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    with Cache(str(tmp_path / "t.db")) as cache:
        runtime, store = _vertical_line_runtime(state, cache, now_ms=lambda: clock["now"])
        store.load_for_symbol("SPY")
        runtime.start_drawing("vertical_line", _series(), runtime.chart_event(_series(), 2.0, 103.0))
        runtime.render_all(_series())

        clock["now"] += 59 * _DAY_MS
        request = runtime.config_request("vertical_line_1")
        assert request is not None
        edited = dict(request.params)
        edited["color"] = "#ff0000"
        runtime.apply_config("vertical_line_1", edited)

        clock["now"] += 59 * _DAY_MS
        passes = runtime.render_all(_series())

    assert len(passes) == 1
    assert len(passes[0].render.vertical_lines) == 1


def test_legacy_tool_record_gets_age_off_metadata_without_expiring(tmp_path: Path) -> None:
    now_ms = 10 * _DAY_MS
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    with Cache(str(tmp_path / "t.db")) as cache:
        cache.put_extension_record(
            _VERTICAL_STORE_KEY,
            "SPY",
            _series_timestamp(2),
            _vertical_payload(),
        )
        runtime, store = _vertical_line_runtime(state, cache, now_ms=lambda: now_ms)
        store.load_for_symbol("SPY")
        passes = runtime.render_all(_series())
        records = cache.get_extension_records(_VERTICAL_STORE_KEY, "SPY")

    assert len(passes) == 1
    assert records[0].payload["updated_at_ms"] == now_ms
    assert records[0].payload["age_off_days"] == 60.0


def _vertical_line_runtime(
    state: State,
    cache: Cache,
    now_ms: Callable[[], int] | None = None,
) -> tuple[ChartExtensionRuntime, VerticalLineStore]:
    store = VerticalLineStore(AppChartExtensionStoreContext(state, cache), now_ms=now_ms)
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


def _series_timestamp(index: int) -> int:
    return int(_series().bars[index].timestamp.timestamp() * 1000)


def _vertical_payload(
    updated_at_ms: int | None = None,
    age_off_days: float | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "timeframe": Timeframe.DAILY.value,
        "color": "#7a7f8c",
        "line_width": 1.0,
        "line_style": "solid",
        "persist_across_timeframes": True,
        "persist_across_sessions": True,
    }
    if updated_at_ms is not None:
        payload["updated_at_ms"] = updated_at_ms
    if age_off_days is not None:
        payload["age_off_days"] = age_off_days
    return payload
