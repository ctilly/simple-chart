import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import finplot as fplt
import pytest

import app.controller as controller_module
from app.controller import MainWindow, _recent_history_start
from app.state import ChartExtensionState
from data.cache import Cache
from data.models import Bar, Level1Quote, MarketSnapshot, OHLCVSeries, Timeframe
from data.provider.base import DataProvider
from data.provider.config import ProviderConnection
from data.provider.credentials import (
    CredentialStore,
    CredentialStoreAccess,
    UnavailableCredentialStore,
)
from simplechart.api import (
    ChartEvent,
    ChartExtension,
    ChartExtensionAddMode,
    ChartExtensionMutation,
    ChartExtensionRender,
    DragSession,
    HitTestResult,
    HorizontalLineRender,
    register_extension,
)

_FIRST = datetime(2026, 6, 1, 13, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolated_finplot_windows() -> Iterator[None]:
    # fplt.refresh() walks finplot's process-global window list; drop windows
    # left by earlier tests, whose Qt objects are already deleted.
    fplt.windows.clear()
    yield
    fplt.windows.clear()


def _window(qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> MainWindow:
    # Never touch the host OS keyring from these tests.
    monkeypatch.setattr(
        controller_module,
        "initialize_keyring_credential_store",
        lambda: CredentialStoreAccess(
            UnavailableCredentialStore("test"), False, "test",
        ),
    )
    production_load = MainWindow._load
    monkeypatch.setattr(MainWindow, "_load", lambda window: None)
    monkeypatch.setattr(MainWindow, "_refresh_watchlist_snapshots", lambda window: None)
    monkeypatch.setattr(MainWindow, "_refresh_level1", lambda window: None)
    window = MainWindow(str(tmp_path / "test.db"))
    qtbot.addWidget(window)
    monkeypatch.setattr(MainWindow, "_load", production_load)
    return window


def _series(count: int, first: datetime = _FIRST) -> OHLCVSeries:
    return OHLCVSeries(
        "XBI",
        Timeframe.MIN5,
        [
            Bar(first + timedelta(minutes=5 * i), 100.0, 101.0, 99.0, 100.5, 1_000)
            for i in range(count)
        ],
    )


def _current_request(window: MainWindow) -> Any:
    request = window._make_chart_load_request(
        "XBI", Timeframe.MIN5, window._selected_route.connection,
    )
    window._current_chart_request = request
    window._state.symbol = "XBI"
    window._state.timeframe = Timeframe.MIN5
    return request


def _view(window: MainWindow) -> list[tuple[float, float]]:
    pm = window._chart.plot_manager
    return [
        (axis.vb.targetRect().left(), axis.vb.targetRect().right())  # type: ignore[attr-defined]
        for axis in pm._viewport_axes()
    ]


def _sma_keys(window: MainWindow) -> list[str]:
    return [key for key in window._chart.plot_manager.active_series_keys() if key.startswith("sma_")]


def test_recent_render_then_history_merge_keeps_view_and_adds_indicators(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    warnings: list[str] = []
    monkeypatch.setattr(controller_module, "show_warning", lambda _p, title, _m: warnings.append(title))
    request = _current_request(window)
    recent = _series(300, _FIRST + timedelta(minutes=5 * 200))
    full = _series(500)

    window._on_chart_recent_ready(request, recent)

    assert window._current_series is recent
    assert window._terminal_chart_generation != request.generation
    assert _sma_keys(window) == []
    assert window._symbol_bar._source_dot.toolTip().endswith(": Loading indicators...")
    status_bar = window.statusBar()
    assert status_bar is not None
    assert status_bar.currentMessage() == "Loading indicators..."
    for axis in window._chart.plot_manager._viewport_axes():
        axis.vb.set_range(250.0, 90.0, 290.0, 120.0)  # type: ignore[attr-defined]
    before = _view(window)

    window._on_chart_fetch_done(request, full)

    assert window._current_series is full
    assert window._terminal_chart_generation == request.generation
    assert _view(window) == [(left + 200.0, right + 200.0) for left, right in before]
    assert sorted(_sma_keys(window)) == ["sma_20", "sma_5", "sma_50"]
    assert "succeeded" in window._symbol_bar._source_dot.toolTip()
    assert warnings == []


def test_stale_recent_and_history_results_are_ignored(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    old_request = _current_request(window)
    current = _current_request(window)
    window._on_chart_recent_ready(current, _series(300))

    window._on_chart_recent_ready(old_request, _series(50))
    window._on_chart_fetch_done(old_request, _series(60))

    assert window._current_series is not None
    assert len(window._current_series.bars) == 300


def test_backfill_error_after_recent_render_shows_status_not_dialog(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    warnings: list[str] = []
    monkeypatch.setattr(controller_module, "show_warning", lambda _p, title, _m: warnings.append(title))
    request = _current_request(window)
    window._on_chart_recent_ready(request, _series(300))

    window._on_chart_fetch_error(request, "network down")

    assert warnings == []
    assert window._terminal_chart_generation == request.generation
    assert "failed" in window._symbol_bar._source_dot.toolTip()
    status_bar = window.statusBar()
    assert status_bar is not None
    assert "older history" in status_bar.currentMessage().lower()
    assert _sma_keys(window) == []


_FINISHED_DRAG_TIMESTAMPS: list[int | None] = []


class _InterimDragTool(ChartExtension):
    """Interim-safe toolbar drawing that records where a drag finishes."""

    def name(self) -> str:
        return "test_interim_drag_tool"

    def label(self) -> str:
        return "Test Interim Drag Tool"

    def default_params(self) -> dict[str, Any]:
        return {}

    def add_mode(self) -> ChartExtensionAddMode:
        return ChartExtensionAddMode.TOOLBAR

    def history_lookback_days(self, params: dict[str, Any]) -> int | None:
        return 0

    def compute(self, series: OHLCVSeries, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def render(self, series: OHLCVSeries, params: dict[str, Any]) -> ChartExtensionRender:
        return ChartExtensionRender(
            horizontal_lines=[
                HorizontalLineRender(
                    key="test_interim_drag_line", y_value=100.0, label="Test",
                    color="#ffffff", line_width=1.0,
                )
            ]
        )

    def hit_test(
        self, series: OHLCVSeries, params: dict[str, Any], event: ChartEvent,
        visible_keys: set[str],
    ) -> HitTestResult | None:
        if "test_interim_drag_line" not in visible_keys:
            return None
        return HitTestResult(self.name(), "test_interim_drag_line")

    def finish_drag(
        self, series: OHLCVSeries, session: DragSession, event: ChartEvent,
    ) -> ChartExtensionMutation | None:
        _FINISHED_DRAG_TIMESTAMPS.append(event.timestamp_ms)
        return None


register_extension(_InterimDragTool)


def test_history_merge_is_held_during_real_drag_and_applied_once_after(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    request = _current_request(window)
    recent = _series(300, _FIRST + timedelta(minutes=5 * 200))
    full = _series(500)
    window._on_chart_recent_ready(request, recent)
    window._state.extensions.append(
        ChartExtensionState(name="test_interim_drag_tool", params={})
    )
    window._reload_extensions(draw_bars=False)
    _FINISHED_DRAG_TIMESTAMPS.clear()

    assert window._on_drag_start(10.0, 100.0, 1.0, 1.0)
    window._on_chart_fetch_done(request, full)
    assert window._current_series is recent
    window._on_drag_move(12.0, 100.0, 1.0, 1.0)
    window._on_drag_finish(12.0, 100.0, 1.0, 1.0)

    # The drag resolved its timestamp against the series it started on.
    expected = int(recent.bars[12].timestamp.timestamp() * 1000)
    assert _FINISHED_DRAG_TIMESTAMPS == [expected]
    assert window._current_series is full
    window._flush_held_history_merge()
    assert window._current_series is full


def test_held_history_merge_is_discarded_after_navigation(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    request = _current_request(window)
    window._on_chart_recent_ready(request, _series(300))
    window._extension_runtime._drag_session = object()  # type: ignore[assignment]
    window._on_chart_fetch_done(request, _series(500))

    window._state.symbol = "SPY"
    window._load()
    window._extension_runtime._drag_session = None
    window._flush_held_history_merge()

    assert window._held_history_merge is None
    assert window._current_series is None or window._current_series.symbol == "XBI"
    assert window._current_series is None or len(window._current_series.bars) == 300


def test_interim_reload_does_not_prepend_partially_backfilled_bars(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    request = _current_request(window)
    full = _series(500, (datetime.now(tz=UTC) - timedelta(days=3)).replace(second=0, microsecond=0))
    recent = OHLCVSeries("XBI", Timeframe.MIN5, full.bars[200:])
    namespace = window._selected_route.cache_namespace
    window._cache.put_bars(namespace, "XBI", Timeframe.MIN5, full.bars)
    window._on_chart_recent_ready(request, recent)

    window._reload_extensions()

    assert window._current_series is not None
    assert window._current_series.bars[0].timestamp == recent.bars[0].timestamp
    assert _sma_keys(window) == []


def _production_recent_start(window: MainWindow, now: datetime) -> datetime:
    return _recent_history_start(
        Timeframe.MIN5,
        now - timedelta(days=600),
        now,
        window._extension_lookback_days("XBI"),
    )


class _GatedPagedProvider(DataProvider):
    """Paged 5m provider whose backfill (pre-recent) requests wait on a gate."""

    def __init__(self, recent_start: datetime) -> None:
        self.recent_start = recent_start
        self.gate = threading.Event()

    def fetch_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime,
    ) -> list[Bar]:
        if timeframe == Timeframe.DAILY:
            return []
        if end <= self.recent_start:
            assert self.gate.wait(10)
        bars: list[Bar] = []
        slot = start.replace(second=0, microsecond=0)
        slot -= timedelta(minutes=slot.minute % 5)
        while slot < end:
            if slot >= start and controller_module.is_regular_session_timestamp(slot):
                bars.append(Bar(slot, 100.0, 101.0, 99.0, 100.5, 1_000))
            slot += timedelta(minutes=5)
        return bars

    def history_page_span(self, timeframe: Timeframe) -> timedelta | None:
        return timedelta(days=30) if timeframe.is_intraday else None

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        return {}

    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        return None

    def native_timeframes(self) -> list[Timeframe]:
        return [Timeframe.MIN5, Timeframe.DAILY]


def test_production_load_renders_recent_window_before_backfill_completes(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    now = datetime.now(tz=UTC)
    recent_start = _production_recent_start(window, now)
    provider = _GatedPagedProvider(recent_start)

    def create_test_provider(
        _connection: ProviderConnection, _store: CredentialStore,
    ) -> DataProvider:
        return provider

    monkeypatch.setattr(controller_module, "create_provider", create_test_provider)
    monkeypatch.setattr(controller_module, "show_warning", lambda *_args: None)
    window._state.symbol = "XBI"
    window._state.timeframe = Timeframe.MIN5

    window._load()

    qtbot.waitUntil(lambda: window._current_series is not None, timeout=10_000)
    recent = window._current_series
    assert recent is not None
    assert recent.bars[0].timestamp >= recent_start
    assert window._partial_history_request is not None
    # The first fetch covers the default SMAs' lookback, so they render with
    # the candles instead of waiting for the backfill.
    assert sorted(_sma_keys(window)) == ["sma_20", "sma_5", "sma_50"]

    provider.gate.set()
    qtbot.waitUntil(lambda: window._partial_history_request is None, timeout=10_000)
    final = window._current_series
    assert final is not None and final.bars[0].timestamp < recent_start
    assert sorted(_sma_keys(window)) == ["sma_20", "sma_5", "sma_50"]
    qtbot.waitUntil(lambda: not window._chart_load_tasks, timeout=10_000)
    with Cache(window._db_path) as cache:
        coverage = cache.get_bar_fetch_coverage(
            window._selected_route.cache_namespace, "XBI", Timeframe.MIN5,
        )
    assert coverage is not None
    assert coverage[0] <= int((now - timedelta(days=599)).timestamp() * 1000)


def test_replacement_load_does_not_reveal_partial_cache_as_complete(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    monkeypatch.setattr(window, "_chart_load_timer", _InertTimer())
    request = _current_request(window)
    full = _series(500, (datetime.now(tz=UTC) - timedelta(days=3)).replace(second=0, microsecond=0))
    recent = OHLCVSeries("XBI", Timeframe.MIN5, full.bars[200:])
    namespace = window._selected_route.cache_namespace
    window._cache.put_bars(namespace, "XBI", Timeframe.MIN5, full.bars)
    window._on_chart_recent_ready(request, recent)

    # Reload the same chart; the replacement load stays pending.
    window._load()
    window._reload_extensions()

    assert window._current_series is not None
    assert window._current_series.bars[0].timestamp == recent.bars[0].timestamp
    assert _sma_keys(window) == []


class _InertTimer:
    def start(self) -> None:
        pass

    def isActive(self) -> bool:
        return False

    def stop(self) -> None:
        pass


def test_load_timing_log_records_production_load_steps(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIMPLECHART_LOAD_TIMING", "1")
    window = _window(qtbot, tmp_path, monkeypatch)
    now = datetime.now(tz=UTC)
    provider = _GatedPagedProvider(_production_recent_start(window, now))
    monkeypatch.setattr(
        controller_module, "create_provider", lambda _connection, _store: provider,
    )
    real_clear = window._chart.clear_all

    def slow_clear() -> None:
        time.sleep(0.2)  # work after the user action must count
        real_clear()

    monkeypatch.setattr(window._chart, "clear_all", slow_clear)
    monkeypatch.setattr(controller_module, "show_warning", lambda *_args: None)
    window._state.symbol = "XBI"
    window._state.timeframe = Timeframe.MIN5

    window._load()
    generation = window._chart_load_generation
    # Hold the backfill until the recent render's event-loop marker is logged.
    qtbot.waitUntil(lambda: window._partial_history_request is not None, timeout=10_000)
    qtbot.wait(50)
    provider.gate.set()
    qtbot.waitUntil(lambda: window._partial_history_request is None
                    and window._current_series is not None
                    and not window._chart_load_tasks, timeout=10_000)
    qtbot.wait(50)
    window._load_timing.close()

    lines = (tmp_path / "diagnostics" / "load-timing.log").read_text().splitlines()
    steps = [line.split(" ")[4] for line in lines if f" gen={generation} " in line]
    expected = [
        "load_requested", "dispatched", "worker_started", "provider_ready",
        "daily_refs_done", "recent_fetched", "recent_delivered", "render_done", "event_loop_resumed",
        "history_fetched", "history_delivered", "merge_done",
    ]
    assert [step for step in steps if step in expected] == expected
    for line in lines:
        assert len(line.split(" ")) == 6
    load_requested_ms = int(
        next(line for line in lines if f" gen={generation} " in line and "load_requested" in line)
        .split(" ")[5][1:-2]
    )
    assert load_requested_ms >= 200


def test_new_symbol_load_requests_the_quote_once(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    quote_requests: list[str | None] = []
    monkeypatch.setattr(
        window, "_refresh_level1", lambda: quote_requests.append(window._state.symbol),
    )
    monkeypatch.setattr(window, "_chart_load_timer", _InertTimer())
    window._level1_symbol = "SPY"
    window._state.symbol = "XBI"
    window._state.timeframe = Timeframe.MIN5

    window._load()  # symbol changed: the click refreshes the quote
    request = window._current_chart_request
    window._on_chart_recent_ready(request, _series(300, _FIRST + timedelta(minutes=5 * 200)))
    window._on_chart_fetch_done(request, _series(500))

    assert quote_requests == ["XBI"]


def test_timeframe_switch_still_refreshes_the_quote_after_load(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    quote_requests: list[str | None] = []
    monkeypatch.setattr(
        window, "_refresh_level1", lambda: quote_requests.append(window._state.symbol),
    )
    monkeypatch.setattr(window, "_chart_load_timer", _InertTimer())
    window._level1_symbol = "XBI"
    window._state.symbol = "XBI"
    window._state.timeframe = Timeframe.MIN5

    window._load()  # same symbol: no quote request on click
    request = window._current_chart_request
    window._on_chart_fetch_done(request, _series(300))

    assert quote_requests == ["XBI"]


class _HistoryAnchorProbe(ChartExtension):
    """Anchored like AVWAP: omits anchors older than the loaded bars."""

    def name(self) -> str:
        return "test_history_anchor_probe"

    def label(self) -> str:
        return "Test History Anchor Probe"

    def default_params(self) -> dict[str, Any]:
        return {"anchors_ms": []}

    def history_lookback_days(self, params: dict[str, Any]) -> int | None:
        return 0

    def compute(self, series: OHLCVSeries, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def render(self, series: OHLCVSeries, params: dict[str, Any]) -> ChartExtensionRender:
        first_ms = int(series.bars[0].timestamp.timestamp() * 1000)
        anchors: list[int] = params["anchors_ms"]
        unloaded = [
            ms for ms in anchors
            if not params.get("_history_complete", True) and ms < first_ms
        ]
        return ChartExtensionRender(
            horizontal_lines=[
                HorizontalLineRender(
                    key=f"probe_{ms}", y_value=100.0, label="Probe",
                    color="#ffffff", line_width=1.0,
                )
                for ms in anchors
                if ms not in unloaded
            ],
            pending_history_start_ms=max(unloaded, default=None),
        )


register_extension(_HistoryAnchorProbe)


def _ms(bar: Bar) -> int:
    return int(bar.timestamp.timestamp() * 1000)


def _probe_keys(window: MainWindow) -> list[str]:
    probe = window._state.get_extension("test_history_anchor_probe")
    return [] if probe is None else sorted(probe.series_keys)


def _interim_with_probe(
    window: MainWindow,
    full: OHLCVSeries,
    anchors: list[Bar],
) -> Any:
    request = _current_request(window)
    namespace = window._selected_route.cache_namespace
    window._cache.put_bars(namespace, "XBI", Timeframe.MIN5, full.bars)
    window._on_chart_recent_ready(
        request, OHLCVSeries("XBI", Timeframe.MIN5, full.bars[200:]),
    )
    window._state.extensions.append(
        ChartExtensionState(
            name="test_history_anchor_probe",
            params={"anchors_ms": [_ms(bar) for bar in anchors]},
        )
    )
    window._reload_extensions(draw_bars=False)
    return request


def test_history_progress_renders_content_once_its_history_arrives(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    full = _series(500)
    request = _interim_with_probe(window, full, [full.bars[100]])
    assert _probe_keys(window) == []
    assert window._extension_runtime.pending_history_start_ms == _ms(full.bars[100])
    before = _view(window)

    # Committed history that stops short of the anchor changes nothing.
    window._on_chart_history_progress(request, _ms(full.bars[150]))
    assert not window._history_refresh_timer.isActive()

    window._on_chart_history_progress(request, _ms(full.bars[50]))
    qtbot.waitUntil(lambda: _probe_keys(window) != [], timeout=5_000)

    assert _probe_keys(window) == [f"probe_{_ms(full.bars[100])}"]
    assert window._current_series is not None
    assert window._current_series.bars[0].timestamp == full.bars[50].timestamp
    # Still interim: the full window has not arrived.
    assert window._partial_history_request == request
    assert window._displayed_history_complete is False
    assert _view(window) == [(left + 150.0, right + 150.0) for left, right in before]

    window._on_chart_fetch_done(request, full)
    assert window._current_series is full
    assert window._partial_history_request is None


def test_history_progress_re_renders_at_most_once_per_interval(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    full = _series(500)
    request = _interim_with_probe(window, full, [full.bars[100], full.bars[20]])

    window._on_chart_history_progress(request, _ms(full.bars[50]))
    qtbot.waitUntil(lambda: len(_probe_keys(window)) == 1, timeout=5_000)

    window._on_chart_history_progress(request, _ms(full.bars[10]))
    window._on_chart_history_progress(request, _ms(full.bars[0]))
    assert window._history_refresh_timer.isActive()
    assert window._history_refresh_timer.remainingTime() > 500
    assert len(_probe_keys(window)) == 1

    qtbot.waitUntil(lambda: len(_probe_keys(window)) == 2, timeout=5_000)
    assert window._current_series is not None
    assert window._current_series.bars[0].timestamp == full.bars[0].timestamp


def test_history_progress_waits_for_a_drag_to_finish(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    full = _series(500)
    request = _interim_with_probe(window, full, [full.bars[100]])
    displayed = window._current_series
    window._extension_runtime._drag_session = object()  # type: ignore[assignment]

    window._on_chart_history_progress(request, _ms(full.bars[50]))
    qtbot.waitUntil(lambda: window._history_refresh_deferred, timeout=5_000)
    assert window._current_series is displayed

    window._extension_runtime._drag_session = None
    window._flush_held_history_merge()
    qtbot.waitUntil(lambda: _probe_keys(window) != [], timeout=5_000)


def test_stale_history_progress_is_ignored(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    full = _series(500)
    request = _interim_with_probe(window, full, [full.bars[100]])
    stale = window._make_chart_load_request(
        "XBI", Timeframe.MIN5, window._selected_route.connection,
    )
    assert stale != request

    window._on_chart_history_progress(stale, _ms(full.bars[0]))

    assert not window._history_refresh_timer.isActive()
    assert window._history_progress_ms is None


class _CutGatedPagedProvider(_GatedPagedProvider):
    """Backfill chunks reaching before `cut` wait on the gate."""

    def __init__(self, cut: datetime) -> None:
        # The parent gates requests ending before recent_start; an epoch
        # recent_start disables that so only `cut` decides.
        super().__init__(datetime(1970, 1, 1, tzinfo=UTC))
        self.cut = cut

    def fetch_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime,
    ) -> list[Bar]:
        if timeframe != Timeframe.DAILY and start < self.cut:
            assert self.gate.wait(10)
        return super().fetch_bars(symbol, timeframe, start, end)


def test_production_backfill_progress_renders_anchored_content_before_completion(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    now = datetime.now(tz=UTC)
    recent_start = _production_recent_start(window, now)
    # The newest backfill chunk spans the 30 days before recent_start; the
    # anchor sits inside it and every older chunk waits on the gate.
    anchor = recent_start - timedelta(days=10)
    provider = _CutGatedPagedProvider(recent_start - timedelta(days=40))
    monkeypatch.setattr(
        controller_module, "create_provider", lambda _connection, _store: provider,
    )
    monkeypatch.setattr(controller_module, "show_warning", lambda *_args: None)
    monkeypatch.setattr(
        controller_module,
        "INITIAL_EXTENSIONS",
        [
            *controller_module.INITIAL_EXTENSIONS,
            (
                "test_history_anchor_probe",
                {"anchors_ms": [int(anchor.timestamp() * 1000)]},
            ),
        ],
    )
    window._state.symbol = "XBI"
    window._state.timeframe = Timeframe.MIN5
    received_ms: list[int] = []
    production_progress = window._on_chart_history_progress

    def record_progress(request: Any, earliest_ms: int) -> None:
        received_ms.append(earliest_ms)
        production_progress(request, earliest_ms)

    # The worker connects to this attribute when the load is dispatched.
    monkeypatch.setattr(window, "_on_chart_history_progress", record_progress)

    window._load()

    qtbot.waitUntil(lambda: _probe_keys(window) != [], timeout=10_000)
    assert window._partial_history_request is not None
    displayed = window._current_series
    assert displayed is not None and displayed.bars[0].timestamp <= anchor

    provider.gate.set()
    qtbot.waitUntil(lambda: window._partial_history_request is None, timeout=10_000)
    final = window._current_series
    assert final is not None and final.bars[0].timestamp < displayed.bars[0].timestamp
    qtbot.waitUntil(lambda: not window._chart_load_tasks, timeout=10_000)
    # Progress values cross the real worker signal intact: each is the UTC-ms
    # start of a committed chunk, older than the last, inside the history the
    # load fetched (a chunk may start up to a weekend before its first bar).
    oldest_ms = _ms(final.bars[0]) - int(timedelta(days=7).total_seconds() * 1000)
    newest_ms = int(recent_start.timestamp() * 1000)
    assert received_ms
    assert received_ms == sorted(received_ms, reverse=True)
    assert len(set(received_ms)) == len(received_ms)
    assert all(oldest_ms <= ms <= newest_ms for ms in received_ms)


def test_history_progress_signal_carries_utc_ms_timestamps_intact() -> None:
    worker = controller_module._FetchWorker(
        db_path=":memory:",
        credential_store=UnavailableCredentialStore("test"),
        request=object(),  # type: ignore[arg-type]
    )
    received: list[tuple[object, int]] = []
    worker.history_progress.connect(
        lambda request, earliest_ms: received.append((request, earliest_ms))
    )

    worker._emit_history_progress(1_794_240_000_000)

    assert [ms for _request, ms in received] == [1_794_240_000_000]
