from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.extension_runtime import ChartExtensionRuntime
from app.extension_store import AppChartExtensionStoreContext, ChartExtensionStore
from app.state import State
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
import indicators.avwap  # noqa: F401
import tools.erase  # noqa: F401
import tools.fib_retracement  # noqa: F401
import tools.vertical_line  # noqa: F401
from indicators.avwap.anchor_store import AvwapAnchorStore
from tools.erase import EraseTool
from tools.fib_retracement.session_store import FibRetracementStore
from tools.vertical_line.session_store import VerticalLineStore
from simplechart.api import ChartEvent, ChartExtensionAddMode, ChartExtensionMutation


def test_erase_declares_toolbar_and_icon() -> None:
    tool = EraseTool()
    assert tool.add_mode() == ChartExtensionAddMode.TOOLBAR
    assert tool.toolbar_icon() is not None
    # The tool persists nothing and renders nothing of its own.
    assert tool.default_params() == {}
    series = _series()
    assert tool.render(series, {}).vertical_lines == []


def test_erase_click_requests_clear_and_deactivates() -> None:
    tool = EraseTool()
    series = _series()
    result = tool.start_drawing(series, {}, _event())
    assert result.clear_transient is True
    assert result.done is True
    assert result.deactivate_tool is True
    assert result.mutation is None
    assert result.session is None


def test_erase_clears_transient_but_keeps_persistent(tmp_path: Path) -> None:
    # Build the full persistence matrix, then erase. The survivors are exactly
    # the drawings that persist across sessions.
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _series()
    with Cache(str(tmp_path / "t.db")) as cache:
        runtime, store = _runtime(state, cache)
        store.load_for_symbol("SPY")

        # Four vertical lines (USER/USER), one fib (FIXED_OFF/FIXED_OFF).
        for x in (1.0, 2.0, 3.0, 4.0):
            runtime.start_drawing("vertical_line", series, runtime.chart_event(series, x, 100.0))
        fib_start = runtime.start_drawing("fib_retracement", series, runtime.chart_event(series, 1.0, 100.0))
        assert fib_start.session is not None
        runtime.advance_drawing(series, fib_start.session, runtime.chart_event(series, 3.0, 111.0))
        # Render once so the lines' series keys are registered for config lookups.
        runtime.render_all(series)

        # Session persistence is the only protection from erasure; the timeframe
        # axis is irrelevant.
        # vertical_line_-1: session on  (timeframe on)           -> KEPT
        _configure("vertical_line_-1", runtime, persist_tf=True, persist_session=True)
        # vertical_line_-2: session on  (timeframe off, default) -> KEPT
        _configure("vertical_line_-2", runtime, persist_tf=False, persist_session=True)
        # vertical_line_-3: session off (timeframe off, default) -> ERASED
        # vertical_line_-4: session off (timeframe on)           -> ERASED
        _configure("vertical_line_-4", runtime, persist_tf=True, persist_session=False)

        assert _vertical_count(runtime, series) == 4
        assert _has_fib(runtime, series)

        result = runtime.start_drawing("erase", series, runtime.chart_event(series, 2.5, 100.0))
        assert result.clear_transient is True

        # Only the two session-protected lines remain.
        assert _vertical_count(runtime, series) == 2
        assert not _has_fib(runtime, series)          # fib defaults to session off


def test_erase_is_a_noop_when_nothing_is_transient(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _series()
    with Cache(str(tmp_path / "t.db")) as cache:
        runtime, store = _runtime(state, cache)
        store.load_for_symbol("SPY")
        runtime.start_drawing("vertical_line", series, runtime.chart_event(series, 2.0, 100.0))
        runtime.render_all(series)
        _configure("vertical_line_-1", runtime, persist_tf=False, persist_session=True)

        runtime.start_drawing("erase", series, runtime.chart_event(series, 2.0, 100.0))

        assert _vertical_count(runtime, series) == 1


def test_erase_runs_cleanly_and_spares_avwap_anchors(tmp_path: Path) -> None:
    # AVWAP anchors are indicator state (not drawings) and must survive erase;
    # the fan-out must not throw on the non-DrawingStore avwap handler.
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _series()
    with Cache(str(tmp_path / "t.db")) as cache:
        runtime, store = _runtime(state, cache)
        store.load_for_symbol("SPY")
        # Place the drawing first so it owns series key vertical_line_1 (the
        # anchor and the line share one autoincrementing record table).
        runtime.start_drawing("vertical_line", series, runtime.chart_event(series, 2.0, 100.0))
        store.apply(ChartExtensionMutation(
            extension_name="avwap",
            operation="add_anchor",
            payload={"anchor_ts": int(series.bars[1].timestamp.timestamp() * 1000),
                     "label": "AVWAP", "color": "#8b5a2b"},
        ))
        runtime.render_all(series)

        runtime.start_drawing("erase", series, runtime.chart_event(series, 2.5, 100.0))

        assert _vertical_count(runtime, series) == 0  # transient line erased
        assert [h for h in store._handlers if isinstance(h, AvwapAnchorStore)][0].anchors()


def _configure(
    series_key: str,
    runtime: ChartExtensionRuntime,
    *,
    persist_tf: bool,
    persist_session: bool,
) -> None:
    request = runtime.config_request(series_key)
    assert request is not None
    edited = dict(request.params)
    edited["persist_across_timeframes"] = persist_tf
    edited["persist_across_sessions"] = persist_session
    runtime.apply_config(series_key, edited)


def _vertical_count(runtime: ChartExtensionRuntime, series: OHLCVSeries) -> int:
    return sum(
        len(rendered.render.vertical_lines)
        for rendered in runtime.render_all(series)
    )


def _has_fib(runtime: ChartExtensionRuntime, series: OHLCVSeries) -> bool:
    return any(rendered.render.segments for rendered in runtime.render_all(series))


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


def _event() -> ChartEvent:
    return ChartEvent(
        x=2.0,
        y=100.0,
        bar_index=2,
        timestamp_ms=0,
    )


def _runtime(state: State, cache: Cache) -> tuple[ChartExtensionRuntime, ChartExtensionStore]:
    context = AppChartExtensionStoreContext(state, cache)
    # AvwapAnchorStore is a non-DrawingStore handler; including it guards against
    # the erase fan-out throwing on a handler that lacks clear_transient.
    store = ChartExtensionStore(
        state,
        cache,
        [VerticalLineStore(context), FibRetracementStore(context), AvwapAnchorStore(context)],
    )
    runtime = ChartExtensionRuntime(state, cache, store, lookback_days=600)
    return runtime, store
