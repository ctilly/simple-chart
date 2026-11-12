import gc
import inspect
import sqlite3
import threading
import weakref
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

import app.controller as controller_module
from app.controller import MainWindow, _FetchWorker
from data.aggregator import Aggregator
from data.cache import Cache
from data.models import Bar, Level1Quote, MarketSnapshot, OHLCVSeries, Timeframe
from data.provider.base import DataProvider
from data.provider.alpaca_provider import AlpacaProvider
from data.provider.config import ProviderConnection, alpaca_paper_connection
from data.provider.credentials import CredentialStore, ProviderCredentials


def _window(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MainWindow, Any]:
    production_load = MainWindow._load
    monkeypatch.setattr(MainWindow, "_load", lambda window: None)
    monkeypatch.setattr(
        MainWindow,
        "_refresh_watchlist_snapshots",
        lambda window: None,
    )
    monkeypatch.setattr(MainWindow, "_refresh_level1", lambda window: None)
    window = MainWindow(str(tmp_path / "test.db"))
    qtbot.addWidget(window)
    monkeypatch.setattr(MainWindow, "_load", production_load)
    return window, production_load


class _FinishedThreadStub:
    def isRunning(self) -> bool:
        return False

    def deleteLater(self) -> None:
        pass


def _finished_task_stub(request: Any) -> SimpleNamespace:
    return SimpleNamespace(
        thread=_FinishedThreadStub(),
        request=request,
        cancellation_event=threading.Event(),
        older=False,
    )


def _bar(timestamp: datetime) -> Bar:
    return Bar(timestamp, 100.0, 101.0, 99.0, 100.5, 1_000)


def test_load_never_waits_for_running_fetch(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, production_load = _window(qtbot, tmp_path, monkeypatch)

    class RunningThread:
        def isRunning(self) -> bool:
            return True

        def quit(self) -> None:
            pass

        def wait(self) -> None:
            raise AssertionError("GUI load path waited for a provider call")

    symbol = window._state.symbol
    assert symbol is not None
    connection = window._selected_route.connection
    active_request = window._make_chart_load_request(
        "XLE",
        Timeframe.DAILY,
        connection,
    )
    tasks = cast(dict[int, Any], window._chart_load_tasks)
    tasks[active_request.generation] = SimpleNamespace(
        thread=RunningThread(),
        request=active_request,
        cancellation_event=threading.Event(),
        older=False,
    )
    generation_before_load = window._chart_load_generation

    try:
        production_load(window)

        assert window._chart_load_generation == generation_before_load + 1
        assert window._current_chart_request is not None
        assert window._current_chart_request.symbol == symbol
        assert window._pending_chart_load == window._current_chart_request
        assert window._chart_load_timer.isActive()
        assert tasks[active_request.generation].cancellation_event.is_set()
    finally:
        window._chart_load_timer.stop()
        window._pending_chart_load = None
        tasks.clear()


def test_stopping_data_workers_marks_every_chart_task_cancelled(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _ = _window(qtbot, tmp_path, monkeypatch)
    tasks = cast(dict[int, Any], window._chart_load_tasks)
    requests = [
        window._make_chart_load_request(
            symbol,
            Timeframe.DAILY,
            window._selected_route.connection,
        )
        for symbol in ("XLE", "TSLA")
    ]
    events = [threading.Event(), threading.Event()]
    for index, (request, cancellation_event) in enumerate(zip(requests, events)):
        tasks[index] = SimpleNamespace(
            request=request,
            thread=_FinishedThreadStub(),
            cancellation_event=cancellation_event,
            older=False,
        )
    window._pending_chart_load = requests[0]
    started: list[Any] = []
    monkeypatch.setattr(window, "_start_chart_load", started.append)

    window._stop_data_workers()
    window._on_chart_thread_finished(0)

    assert all(event.is_set() for event in events)
    assert window._pending_chart_load is None
    assert not window._chart_load_timer.isActive()
    assert started == []
    tasks.clear()


def test_rapid_navigation_starts_only_latest_debounced_request(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, production_load = _window(qtbot, tmp_path, monkeypatch)
    started: list[Any] = []
    monkeypatch.setattr(window, "_start_chart_load", started.append)

    for symbol in ("XLE", "TSLA", "NVDA"):
        window._state.symbol = symbol
        production_load(window)

    qtbot.waitUntil(lambda: len(started) == 1, timeout=500)

    assert started[0].symbol == "NVDA"
    assert started[0].timeframe is window._state.timeframe


def test_stale_chart_success_and_error_do_not_reach_current_ui_handlers(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _ = _window(qtbot, tmp_path, monkeypatch)
    connection = window._selected_route.connection
    old_request = window._make_chart_load_request("XLE", Timeframe.DAILY, connection)
    current_request = window._make_chart_load_request(
        "NVDA",
        Timeframe.DAILY,
        connection,
    )
    window._current_chart_request = current_request
    window._state.symbol = "NVDA"
    window._state.timeframe = Timeframe.DAILY
    accepted: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(
        window,
        "_on_fetch_done",
        lambda series: accepted.append(series.symbol),
    )
    monkeypatch.setattr(window, "_on_fetch_error", errors.append)

    window._on_chart_fetch_done(
        old_request,
        OHLCVSeries("XLE", Timeframe.DAILY, []),
    )
    window._on_chart_fetch_error(old_request, "stale failure")
    window._on_chart_fetch_done(
        current_request,
        OHLCVSeries("NVDA", Timeframe.DAILY, []),
    )

    assert accepted == ["NVDA"]
    assert errors == []


def test_terminal_chart_request_rejects_duplicate_delivery(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _ = _window(qtbot, tmp_path, monkeypatch)
    request = window._make_chart_load_request(
        "NVDA",
        Timeframe.DAILY,
        window._selected_route.connection,
    )
    window._current_chart_request = request
    window._state.symbol = request.symbol
    window._state.timeframe = request.timeframe
    accepted: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(
        window,
        "_on_fetch_done",
        lambda series: accepted.append(series.symbol),
    )
    monkeypatch.setattr(window, "_on_fetch_error", errors.append)
    series = OHLCVSeries(request.symbol, request.timeframe, [])

    window._on_chart_fetch_done(request, series)
    window._on_chart_fetch_done(request, series)
    window._on_chart_fetch_error(request, "duplicate failure")

    assert accepted == ["NVDA"]
    assert errors == []


def test_finished_task_promotes_latest_pending_request(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _ = _window(qtbot, tmp_path, monkeypatch)
    connection = window._selected_route.connection
    request = window._make_chart_load_request("NVDA", Timeframe.DAILY, connection)
    tasks = cast(dict[int, Any], window._chart_load_tasks)
    tasks[1] = _finished_task_stub(request)
    tasks[2] = _finished_task_stub(request)
    window._pending_chart_load = request
    started: list[Any] = []
    monkeypatch.setattr(window, "_start_chart_load", started.append)

    window._dispatch_pending_chart_load()
    assert started == []

    window._on_chart_thread_finished(1)

    assert started == [request]
    assert window._pending_chart_load is None
    window._on_chart_thread_finished(2)
    assert started == [request]
    assert tasks == {}


def test_finished_task_preserves_active_debounce_window(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, production_load = _window(qtbot, tmp_path, monkeypatch)
    tasks = cast(dict[int, Any], window._chart_load_tasks)
    connection = window._selected_route.connection
    old_request = window._make_chart_load_request("XLE", Timeframe.DAILY, connection)
    tasks[1] = _finished_task_stub(old_request)
    tasks[2] = _finished_task_stub(old_request)
    started: list[Any] = []
    monkeypatch.setattr(window, "_start_chart_load", started.append)

    window._state.symbol = "NVDA"
    production_load(window)
    assert window._chart_load_timer.isActive()

    window._on_chart_thread_finished(1)
    assert started == []

    qtbot.waitUntil(lambda: len(started) == 1, timeout=500)
    assert started[0].symbol == "NVDA"
    tasks.clear()


def test_route_change_before_start_terminates_current_request(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _ = _window(qtbot, tmp_path, monkeypatch)
    request = window._make_chart_load_request(
        "NVDA",
        Timeframe.DAILY,
        alpaca_paper_connection(),
    )
    window._current_chart_request = request
    window._state.symbol = request.symbol
    window._state.timeframe = request.timeframe
    errors: list[str] = []
    monkeypatch.setattr(window, "_on_fetch_error", errors.append)

    window._start_chart_load(request)

    assert errors == ["The data source changed before the chart load started."]
    assert window._terminal_chart_generation == request.generation
    assert window._chart_load_tasks == {}


def test_chart_worker_owns_resources_and_is_retained_until_thread_finishes(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _ = _window(qtbot, tmp_path, monkeypatch)
    started = threading.Event()
    release = threading.Event()
    provider_thread_ids: list[int] = []
    cache_thread_ids: list[int] = []
    cleanup_thread_ids: list[int] = []
    main_thread_id = threading.get_ident()

    class BlockingProvider(DataProvider):
        def fetch_bars(
            self,
            symbol: str,
            timeframe: Timeframe,
            start: datetime,
            end: datetime,
        ) -> list[Bar]:
            started.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test provider was not released")
            return [Bar(start, 10.0, 11.0, 9.0, 10.5, 100)]

        def fetch_snapshots(
            self,
            _symbols: list[str],
        ) -> dict[str, MarketSnapshot]:
            return {}

        def fetch_level1(self, symbol: str) -> Level1Quote | None:
            return None

        def native_timeframes(self) -> list[Timeframe]:
            return [Timeframe.DAILY]

    def create_test_provider(
        connection: ProviderConnection,
        _credential_store: CredentialStore,
    ) -> DataProvider:
        provider_thread_ids.append(threading.get_ident())
        return BlockingProvider()

    real_cache = Cache

    def create_test_cache(db_path: str) -> Cache:
        cache_thread_ids.append(threading.get_ident())
        return real_cache(db_path)

    monkeypatch.setattr(controller_module, "create_provider", create_test_provider)
    monkeypatch.setattr(controller_module, "Cache", create_test_cache)
    production_cleanup = window._on_chart_thread_finished

    def record_cleanup_thread(generation: int) -> None:
        cleanup_thread_ids.append(threading.get_ident())
        production_cleanup(generation)

    monkeypatch.setattr(window, "_on_chart_thread_finished", record_cleanup_thread)
    request = window._make_chart_load_request(
        "NVDA",
        Timeframe.DAILY,
        window._selected_route.connection,
    )
    window._current_chart_request = request
    window._state.symbol = request.symbol
    window._state.timeframe = request.timeframe
    accepted: list[str] = []
    monkeypatch.setattr(
        window,
        "_on_fetch_done",
        lambda series: accepted.append(series.symbol),
    )

    window._start_chart_load(request)
    worker_reference = weakref.ref(
        window._chart_load_tasks[request.generation].worker
    )
    terminal: list[bool] = []
    window._chart_load_tasks[request.generation].worker.completed.connect(
        lambda: terminal.append(True)
    )
    qtbot.waitUntil(started.is_set, timeout=500)
    gc.collect()

    assert request.generation in window._chart_load_tasks
    assert window._chart_load_tasks[request.generation].thread.isRunning()

    release.set()
    qtbot.waitUntil(
        lambda: request.generation not in window._chart_load_tasks,
        timeout=1000,
    )

    assert accepted == ["NVDA"]
    assert provider_thread_ids and provider_thread_ids[0] != main_thread_id
    assert cache_thread_ids and cache_thread_ids[0] != main_thread_id
    assert cleanup_thread_ids == [main_thread_id]
    assert terminal == [True]
    gc.collect()
    qtbot.waitUntil(lambda: worker_reference() is None, timeout=1000)


def test_cancelled_chart_worker_before_run_emits_only_completion(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _ = _window(qtbot, tmp_path, monkeypatch)
    request = window._make_chart_load_request(
        "NVDA",
        Timeframe.DAILY,
        window._selected_route.connection,
    )
    cancelled = threading.Event()
    cancelled.set()
    calls: list[str] = []

    def unexpected_provider(
        connection: ProviderConnection,
        credential_store: CredentialStore,
    ) -> DataProvider:
        calls.append("create")
        raise AssertionError("cancelled worker created a provider")

    monkeypatch.setattr(controller_module, "create_provider", unexpected_provider)
    worker = _FetchWorker(
        window._db_path,
        window._credential_store,
        request,
        cancelled,
    )
    finished: list[object] = []
    errors: list[str] = []
    completed: list[bool] = []
    worker.finished.connect(lambda *args: finished.append(args))
    worker.error.connect(lambda _request, message: errors.append(message))
    worker.completed.connect(lambda: completed.append(True))

    worker.run()

    assert calls == []
    assert finished == []
    assert errors == []
    assert completed == [True]


@pytest.mark.parametrize("provider_fails", [False, True])
def test_cancel_during_provider_call_persists_range_then_skips_next_call(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    provider_fails: bool,
) -> None:
    window, _ = _window(qtbot, tmp_path, monkeypatch)
    request = window._make_chart_load_request(
        "NVDA",
        Timeframe.MIN5,
        window._selected_route.connection,
    )
    cancelled = threading.Event()
    # Daily references and the intraday window are fetched concurrently, so
    # both provider calls are in flight when the user cancels.
    both_started = threading.Barrier(3)
    release_provider = threading.Event()
    calls: list[Timeframe] = []
    start = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
    returned_bar = Bar(start, 10.0, 11.0, 9.0, 10.5, 100)

    class BlockingProvider(DataProvider):
        def fetch_bars(
            self,
            symbol: str,
            timeframe: Timeframe,
            start: datetime,
            end: datetime,
        ) -> list[Bar]:
            calls.append(timeframe)
            both_started.wait(timeout=2)
            if not release_provider.wait(timeout=2):
                raise RuntimeError("test provider was not released")
            if provider_fails:
                raise RuntimeError("provider failed after cancellation")
            return [returned_bar]

        def fetch_snapshots(
            self,
            _symbols: list[str],
        ) -> dict[str, MarketSnapshot]:
            return {}

        def fetch_level1(self, symbol: str) -> Level1Quote | None:
            return None

        def native_timeframes(self) -> list[Timeframe]:
            return [Timeframe.MIN5, Timeframe.DAILY]

    monkeypatch.setattr(
        controller_module,
        "create_provider",
        lambda _connection, _store: BlockingProvider(),
    )
    worker = _FetchWorker(
        window._db_path,
        window._credential_store,
        request,
        cancelled,
    )
    finished: list[object] = []
    errors: list[str] = []
    completed: list[bool] = []
    worker.finished.connect(lambda *args: finished.append(args))
    worker.error.connect(lambda _request, message: errors.append(message))
    worker.completed.connect(lambda: completed.append(True))
    thread = threading.Thread(target=worker.run)
    thread.start()
    both_started.wait(timeout=2)
    cancelled.set()
    release_provider.set()
    thread.join(timeout=3)
    qtbot.waitUntil(lambda: len(completed) == 1)

    assert not thread.is_alive()
    # Both already-started calls finish; nothing further is requested.
    assert sorted(calls) == sorted([Timeframe.DAILY, Timeframe.MIN5])
    assert finished == []
    assert errors == []
    assert completed == [True]
    with Cache(window._db_path) as cache:
        for timeframe in (Timeframe.DAILY, Timeframe.MIN5):
            assert cache.get_bars(
                request.cache_namespace,
                request.symbol,
                timeframe,
                int(start.timestamp() * 1000),
                int(start.timestamp() * 1000),
            ) == ([] if provider_fails else [returned_bar])
            coverage = cache.get_bar_fetch_coverage(
                request.cache_namespace,
                request.symbol,
                timeframe,
            )
            assert (coverage is None) is provider_fails


def test_cancel_before_synthesized_materialization_keeps_native_cache_only(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = threading.Event()
    start = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
    end = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
    native_bar = Bar(start, 10.0, 11.0, 9.0, 10.5, 100)

    class CancellingProvider(DataProvider):
        def fetch_bars(
            self,
            symbol: str,
            timeframe: Timeframe,
            start: datetime,
            end: datetime,
        ) -> list[Bar]:
            assert timeframe is Timeframe.MIN5
            cancelled.set()
            return [native_bar]

        def fetch_snapshots(
            self,
            _symbols: list[str],
        ) -> dict[str, MarketSnapshot]:
            return {}

        def fetch_level1(self, symbol: str) -> Level1Quote | None:
            return None

        def native_timeframes(self) -> list[Timeframe]:
            return [Timeframe.MIN5]

    aggregator = Aggregator(CancellingProvider())
    materialization_calls: list[bool] = []

    def unexpected_materialization(
        _aggregator: Aggregator,
        _bars: list[Bar],
        _timeframe: Timeframe,
    ) -> list[Bar]:
        materialization_calls.append(True)
        raise AssertionError("cancelled request entered synthesized materialization")

    monkeypatch.setattr(
        Aggregator,
        "synthesize_cached_bars",
        unexpected_materialization,
    )
    namespace = "test-provider"
    with Cache(str(tmp_path / "cancel-synth.db")) as cache:
        with pytest.raises(controller_module._ChartLoadCancelled):
            controller_module._fetch_synthesized_series(
                aggregator,
                cache,
                namespace,
                "NVDA",
                Timeframe.MIN65,
                start,
                end,
                cancellation_event=cancelled,
            )
        assert cache.get_bars(
            namespace,
            "NVDA",
            Timeframe.MIN5,
            int(start.timestamp() * 1000),
            int(end.timestamp() * 1000),
        ) == [native_bar]
        assert cache.get_bar_fetch_coverage(
            namespace,
            "NVDA",
            Timeframe.MIN5,
        ) is not None
        assert cache.get_bars(
            namespace,
            "NVDA",
            Timeframe.MIN65,
            int(start.timestamp() * 1000),
            int(end.timestamp() * 1000),
        ) == []
        assert cache.get_bar_fetch_coverage(
            namespace,
            "NVDA",
            Timeframe.MIN65,
        ) is None
    assert materialization_calls == []


def test_cancel_after_synthesis_preserves_existing_materialized_cache(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = threading.Event()
    start = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
    end = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)
    old_target_bar = Bar(start, 9.0, 10.0, 8.0, 9.5, 90)
    native_bar = Bar(start, 10.0, 11.0, 9.0, 10.5, 100)

    class Provider(DataProvider):
        def fetch_bars(
            self,
            symbol: str,
            timeframe: Timeframe,
            start: datetime,
            end: datetime,
        ) -> list[Bar]:
            return [native_bar]

        def fetch_snapshots(
            self,
            _symbols: list[str],
        ) -> dict[str, MarketSnapshot]:
            return {}

        def fetch_level1(self, symbol: str) -> Level1Quote | None:
            return None

        def native_timeframes(self) -> list[Timeframe]:
            return [Timeframe.MIN5]

    aggregator = Aggregator(Provider())
    original_synthesize = Aggregator.synthesize_cached_bars
    materialization_calls: list[bool] = []

    def cancel_after_synthesis(
        self: Aggregator,
        bars: list[Bar],
        timeframe: Timeframe,
    ) -> list[Bar]:
        result = original_synthesize(self, bars, timeframe)
        materialization_calls.append(True)
        cancelled.set()
        return result

    monkeypatch.setattr(Aggregator, "synthesize_cached_bars", cancel_after_synthesis)
    namespace = "test-provider"
    target_coverage_start = int((start + timedelta(minutes=5)).timestamp() * 1000)
    target_coverage_end = int(end.timestamp() * 1000)
    with Cache(str(tmp_path / "cancel-synth-after.db")) as cache:
        cache.put_bars(namespace, "NVDA", Timeframe.MIN65, [old_target_bar])
        cache.extend_bar_fetch_coverage(
            namespace,
            "NVDA",
            Timeframe.MIN65,
            target_coverage_start,
            target_coverage_end,
        )
        with pytest.raises(controller_module._ChartLoadCancelled):
            controller_module._fetch_synthesized_series(
                aggregator,
                cache,
                namespace,
                "NVDA",
                Timeframe.MIN65,
                start,
                end,
                cancellation_event=cancelled,
            )
        assert cache.get_bars(
            namespace,
            "NVDA",
            Timeframe.MIN65,
            int(start.timestamp() * 1000),
            target_coverage_end,
        ) == [old_target_bar]
        assert cache.get_bar_fetch_coverage(
            namespace,
            "NVDA",
            Timeframe.MIN65,
        ) == (target_coverage_start, target_coverage_end)
    assert materialization_calls == [True]


def test_cancel_between_tail_refresh_and_head_extension_skips_head(
    tmp_path: Any,
) -> None:
    cancelled = threading.Event()
    history_start = datetime(2026, 11, 4, 14, 0, tzinfo=UTC)
    coverage_start = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
    coverage_end = datetime(2026, 11, 4, 16, 0, tzinfo=UTC)
    now = datetime(2026, 11, 4, 17, 0, tzinfo=UTC)
    head_bar = Bar(history_start, 10.0, 11.0, 9.0, 10.5, 100)
    old_bar = Bar(coverage_end - timedelta(minutes=5), 11.0, 12.0, 10.0, 11.5, 100)
    calls: list[tuple[datetime, datetime]] = []

    class CancellingProvider(DataProvider):
        def fetch_bars(
            self,
            symbol: str,
            timeframe: Timeframe,
            start: datetime,
            end: datetime,
        ) -> list[Bar]:
            calls.append((start, end))
            cancelled.set()
            return [head_bar]

        def fetch_snapshots(
            self,
            _symbols: list[str],
        ) -> dict[str, MarketSnapshot]:
            return {}

        def fetch_level1(self, symbol: str) -> Level1Quote | None:
            return None

        def native_timeframes(self) -> list[Timeframe]:
            return [Timeframe.MIN5]

    aggregator = Aggregator(CancellingProvider())
    namespace = "test-provider"
    coverage_start_ms = int(coverage_start.timestamp() * 1000)
    coverage_end_ms = int(coverage_end.timestamp() * 1000)
    with Cache(str(tmp_path / "cancel-head-tail.db")) as cache:
        cache.put_bars(namespace, "NVDA", Timeframe.MIN5, [old_bar])
        cache.extend_bar_fetch_coverage(
            namespace,
            "NVDA",
            Timeframe.MIN5,
            coverage_start_ms,
            coverage_end_ms,
        )
        with pytest.raises(controller_module._ChartLoadCancelled):
            controller_module._fetch_and_cache_bars(
                aggregator,
                cache,
                namespace,
                "NVDA",
                Timeframe.MIN5,
                history_start,
                now,
                cancellation_event=cancelled,
            )
        # Wave 3 (W3-5): the tail, with its basis validation bars, is fetched
        # before older history so a basis change is seen before any write.
        assert calls == [(old_bar.timestamp, now)]
        assert cache.get_bar_fetch_coverage(
            namespace,
            "NVDA",
            Timeframe.MIN5,
        ) == (coverage_start_ms, coverage_end_ms)
        assert cache.get_bars(
            namespace,
            "NVDA",
            Timeframe.MIN5,
            int(history_start.timestamp() * 1000),
            coverage_end_ms,
        ) == [old_bar]


def test_fetch_worker_uses_completed_boundaries_for_repeat_load(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _ = _window(qtbot, tmp_path, monkeypatch)
    first_now = datetime(2026, 11, 4, 15, 3, 4, tzinfo=UTC)
    second_now = datetime(2026, 11, 4, 15, 3, 8, tzinfo=UTC)
    clock = [first_now, second_now]
    calls: list[tuple[Timeframe, datetime, datetime]] = []

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "FixedDateTime":
            assert clock, "chart worker requested an unexpected extra current time"
            current = clock.pop(0)
            return cls(
                current.year,
                current.month,
                current.day,
                current.hour,
                current.minute,
                current.second,
                current.microsecond,
                tzinfo=current.tzinfo,
                fold=current.fold,
            )

    class EmptyProvider(DataProvider):
        def fetch_bars(
            self,
            symbol: str,
            timeframe: Timeframe,
            start: datetime,
            end: datetime,
        ) -> list[Bar]:
            calls.append((timeframe, start, end))
            return []

        def fetch_snapshots(
            self,
            _symbols: list[str],
        ) -> dict[str, MarketSnapshot]:
            return {}

        def fetch_level1(self, symbol: str) -> Level1Quote | None:
            return None

        def native_timeframes(self) -> list[Timeframe]:
            return [Timeframe.MIN5, Timeframe.DAILY]

    def create_test_provider(
        connection: ProviderConnection,
        _credential_store: CredentialStore,
    ) -> DataProvider:
        return EmptyProvider()

    monkeypatch.setattr(controller_module, "datetime", FixedDateTime)
    monkeypatch.setattr(controller_module, "create_provider", create_test_provider)
    request = window._make_chart_load_request(
        "AAPL",
        Timeframe.MIN5,
        window._selected_route.connection,
    )
    results: list[OHLCVSeries] = []
    errors: list[str] = []

    for _ in range(2):
        worker = _FetchWorker(
            window._db_path,
            window._credential_store,
            request,
        )
        worker.finished.connect(lambda _request, series: results.append(series))
        worker.error.connect(lambda _request, message: errors.append(message))
        worker.run()

    assert errors == []
    assert len(results) == 2
    assert len(calls) == 4
    # Daily references and the intraday window run concurrently: assert the
    # repeat load's exact ranges, not their order.
    assert set(calls[2:]) == {
        (
            Timeframe.DAILY,
            datetime(2026, 11, 4, 5, 0, tzinfo=UTC),
            second_now,
        ),
        (
            Timeframe.MIN5,
            datetime(2026, 11, 4, 15, 0, tzinfo=UTC),
            second_now,
        ),
    }


def test_fetch_worker_uses_complete_premarket_equity_cache(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _ = _window(qtbot, tmp_path, monkeypatch)
    now = datetime(2026, 11, 4, 14, 8, 54, tzinfo=UTC)
    prior_daily = datetime(2026, 11, 3, 5, 0, tzinfo=UTC)
    prior_daily_settled = datetime(2026, 11, 4, 5, 0, tzinfo=UTC)
    prior_last_5m = datetime(2026, 11, 3, 20, 55, tzinfo=UTC)
    prior_settled = datetime(2026, 11, 3, 21, 5, tzinfo=UTC)
    calls: list[tuple[Timeframe, datetime, datetime]] = []

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "FixedDateTime":
            return cls(2026, 11, 4, 14, 8, 54, tzinfo=tz or UTC)

    class NoCallProvider(DataProvider):
        def fetch_bars(
            self,
            symbol: str,
            timeframe: Timeframe,
            start: datetime,
            end: datetime,
        ) -> list[Bar]:
            calls.append((timeframe, start, end))
            raise AssertionError("complete premarket cache called the provider")

        def fetch_snapshots(
            self,
            _symbols: list[str],
        ) -> dict[str, MarketSnapshot]:
            return {}

        def fetch_level1(self, symbol: str) -> Level1Quote | None:
            return None

        def native_timeframes(self) -> list[Timeframe]:
            return [Timeframe.MIN5, Timeframe.DAILY]

    def create_test_provider(
        connection: ProviderConnection,
        _credential_store: CredentialStore,
    ) -> DataProvider:
        return NoCallProvider()

    monkeypatch.setattr(controller_module, "datetime", FixedDateTime)
    monkeypatch.setattr(controller_module, "create_provider", create_test_provider)
    request = window._make_chart_load_request(
        "AAPL",
        Timeframe.MIN5,
        window._selected_route.connection,
    )
    with Cache(window._db_path) as cache:
        cache.put_bars(
            request.cache_namespace,
            "AAPL",
            Timeframe.DAILY,
            [_bar(prior_daily)],
        )
        cache.extend_bar_fetch_coverage(
            request.cache_namespace,
            "AAPL",
            Timeframe.DAILY,
            int(datetime(2016, 1, 1, tzinfo=UTC).timestamp() * 1000),
            int(prior_daily_settled.timestamp() * 1000),
        )
        cache.put_bars(
            request.cache_namespace,
            "AAPL",
            Timeframe.MIN5,
            [_bar(prior_last_5m)],
        )
        cache.extend_bar_fetch_coverage(
            request.cache_namespace,
            "AAPL",
            Timeframe.MIN5,
            int((now - timedelta(days=600)).timestamp() * 1000),
            int(prior_settled.timestamp() * 1000),
        )

    results: list[OHLCVSeries] = []
    errors: list[str] = []
    worker = _FetchWorker(
        window._db_path,
        window._credential_store,
        request,
    )
    worker.finished.connect(lambda _request, series: results.append(series))
    worker.error.connect(lambda _request, message: errors.append(message))
    worker.run()

    assert errors == []
    assert calls == []
    assert len(results) == 1
    assert results[0].bars == [_bar(prior_last_5m)]


def test_fetch_worker_repairs_stale_premarket_equity_cache(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _ = _window(qtbot, tmp_path, monkeypatch)
    now = datetime(2026, 11, 4, 14, 8, 54, tzinfo=UTC)
    stale_daily = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
    stale_last_5m = datetime(2026, 11, 2, 20, 55, tzinfo=UTC)
    prior_daily = datetime(2026, 11, 3, 5, 0, tzinfo=UTC)
    prior_last_5m = datetime(2026, 11, 3, 20, 55, tzinfo=UTC)
    prior_close = datetime(2026, 11, 3, 21, 0, tzinfo=UTC)
    calls: list[tuple[Timeframe, datetime, datetime]] = []

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "FixedDateTime":
            return cls(2026, 11, 4, 14, 8, 54, tzinfo=tz or UTC)

    class RepairProvider(DataProvider):
        def fetch_bars(
            self,
            symbol: str,
            timeframe: Timeframe,
            start: datetime,
            end: datetime,
        ) -> list[Bar]:
            calls.append((timeframe, start, end))
            timestamp = (
                prior_daily
                if timeframe == Timeframe.DAILY
                else prior_last_5m
            )
            return [_bar(timestamp)]

        def fetch_snapshots(
            self,
            _symbols: list[str],
        ) -> dict[str, MarketSnapshot]:
            return {}

        def fetch_level1(self, symbol: str) -> Level1Quote | None:
            return None

        def native_timeframes(self) -> list[Timeframe]:
            return [Timeframe.MIN5, Timeframe.DAILY]

    def create_test_provider(
        connection: ProviderConnection,
        _credential_store: CredentialStore,
    ) -> DataProvider:
        return RepairProvider()

    monkeypatch.setattr(controller_module, "datetime", FixedDateTime)
    monkeypatch.setattr(controller_module, "create_provider", create_test_provider)
    request = window._make_chart_load_request(
        "AAPL",
        Timeframe.MIN5,
        window._selected_route.connection,
    )
    with Cache(window._db_path) as cache:
        cache.put_bars(
            request.cache_namespace,
            "AAPL",
            Timeframe.DAILY,
            [_bar(stale_daily)],
        )
        cache.extend_bar_fetch_coverage(
            request.cache_namespace,
            "AAPL",
            Timeframe.DAILY,
            int(datetime(2016, 1, 1, tzinfo=UTC).timestamp() * 1000),
            int(prior_daily.timestamp() * 1000),
        )
        cache.put_bars(
            request.cache_namespace,
            "AAPL",
            Timeframe.MIN5,
            [_bar(stale_last_5m)],
        )
        cache.extend_bar_fetch_coverage(
            request.cache_namespace,
            "AAPL",
            Timeframe.MIN5,
            int((now - timedelta(days=600)).timestamp() * 1000),
            int(prior_close.timestamp() * 1000),
        )

    results: list[OHLCVSeries] = []
    errors: list[str] = []
    worker = _FetchWorker(
        window._db_path,
        window._credential_store,
        request,
    )
    worker.finished.connect(lambda _request, series: results.append(series))
    worker.error.connect(lambda _request, message: errors.append(message))
    worker.run()

    assert errors == []
    # Concurrent daily and intraday repair: exact ranges, order not asserted.
    assert len(calls) == 2
    assert set(calls) == {
        (Timeframe.DAILY, stale_daily, now),
        (Timeframe.MIN5, stale_last_5m, now),
    }
    assert len(results) == 1
    assert results[0].bars[-1] == _bar(prior_last_5m)


def test_real_alpaca_factory_worker_materializes_65m_from_cached_5m(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _ = _window(qtbot, tmp_path, monkeypatch)
    now = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)
    daily_bar = datetime(2026, 11, 4, 5, 0, tzinfo=UTC)
    daily_settled = datetime(2026, 11, 5, 5, 0, tzinfo=UTC)
    session_open = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
    intraday_settled = datetime(2026, 11, 4, 21, 5, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "FixedDateTime":
            return cls(2026, 11, 5, 14, 0, tzinfo=tz or UTC)

    class SyntheticCredentialStore:
        def get(self, _connection_id: str) -> ProviderCredentials | None:
            return ProviderCredentials("synthetic-id", "synthetic-secret")

        def put(
            self,
            _connection_id: str,
            _credentials: ProviderCredentials,
        ) -> None:
            raise AssertionError("chart loading must not write credentials")

        def delete(self, _connection_id: str) -> None:
            raise AssertionError("chart loading must not delete credentials")

    def unexpected_fetch(
        _provider: AlpacaProvider,
        _symbol: str,
        _timeframe: Timeframe,
        _start: datetime,
        _end: datetime,
    ) -> list[Bar]:
        raise AssertionError("complete native cache called Alpaca history")

    monkeypatch.setattr(controller_module, "datetime", FixedDateTime)
    monkeypatch.setattr(AlpacaProvider, "fetch_bars", unexpected_fetch)
    connection = alpaca_paper_connection()
    request = window._make_chart_load_request(
        "QQQ",
        Timeframe.MIN65,
        connection,
    )
    base_bars = [
        _bar(session_open + timedelta(minutes=offset))
        for offset in range(0, 65, 5)
    ]
    base_bars.append(_bar(datetime(2026, 11, 4, 20, 55, tzinfo=UTC)))
    with Cache(window._db_path) as cache:
        cache.put_bars(
            request.cache_namespace,
            request.symbol,
            Timeframe.DAILY,
            [_bar(daily_bar)],
        )
        cache.extend_bar_fetch_coverage(
            request.cache_namespace,
            request.symbol,
            Timeframe.DAILY,
            int(datetime(2016, 1, 1, tzinfo=UTC).timestamp() * 1000),
            int(daily_settled.timestamp() * 1000),
        )
        cache.put_bars(
            request.cache_namespace,
            request.symbol,
            Timeframe.MIN5,
            base_bars,
        )
        cache.extend_bar_fetch_coverage(
            request.cache_namespace,
            request.symbol,
            Timeframe.MIN5,
            int((now - timedelta(days=600)).timestamp() * 1000),
            int(intraday_settled.timestamp() * 1000),
        )

    results: list[OHLCVSeries] = []
    errors: list[str] = []
    worker = _FetchWorker(
        window._db_path,
        cast(CredentialStore, SyntheticCredentialStore()),
        request,
    )
    worker.finished.connect(lambda _request, series: results.append(series))
    worker.error.connect(lambda _request, message: errors.append(message))
    worker.run()

    assert errors == []
    assert len(results) == 1
    assert results[0].symbol == "QQQ"
    assert results[0].timeframe is Timeframe.MIN65
    assert [bar.timestamp for bar in results[0].bars] == [
        session_open,
        datetime(2026, 11, 4, 20, 55, tzinfo=UTC),
    ]


def test_two_real_workers_do_not_block_latest_debounced_request(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, production_load = _window(qtbot, tmp_path, monkeypatch)
    started_symbols: set[str] = set()
    started_lock = threading.Lock()
    first_two_started = threading.Event()
    release_first_two = threading.Event()
    write_barrier = threading.Barrier(2)
    fetch_calls: list[str] = []
    fetch_calls_lock = threading.Lock()
    worker_successes: list[str] = []
    worker_errors: list[str] = []

    class CoordinatedProvider(DataProvider):
        def fetch_bars(
            self,
            symbol: str,
            timeframe: Timeframe,
            start: datetime,
            end: datetime,
        ) -> list[Bar]:
            with fetch_calls_lock:
                fetch_calls.append(symbol)
            if symbol in {"XLE", "TSLA"}:
                with started_lock:
                    started_symbols.add(symbol)
                    if started_symbols == {"XLE", "TSLA"}:
                        first_two_started.set()
                if not release_first_two.wait(timeout=2):
                    raise RuntimeError("concurrent test providers were not released")
                write_barrier.wait(timeout=2)
            return [Bar(start, 10.0, 11.0, 9.0, 10.5, 100)]

        def fetch_snapshots(
            self,
            _symbols: list[str],
        ) -> dict[str, MarketSnapshot]:
            return {}

        def fetch_level1(self, symbol: str) -> Level1Quote | None:
            return None

        def native_timeframes(self) -> list[Timeframe]:
            return [Timeframe.DAILY]

    def create_test_provider(
        connection: ProviderConnection,
        _credential_store: CredentialStore,
    ) -> DataProvider:
        return CoordinatedProvider()

    monkeypatch.setattr(controller_module, "create_provider", create_test_provider)
    production_done = window._on_chart_fetch_done
    production_error = window._on_chart_fetch_error

    def record_worker_success(request: Any, series: OHLCVSeries) -> None:
        worker_successes.append(request.symbol)
        production_done(request, series)

    def record_worker_error(request: Any, message: str) -> None:
        worker_errors.append(f"{request.symbol}: {message}")
        production_error(request, message)

    monkeypatch.setattr(window, "_on_chart_fetch_done", record_worker_success)
    monkeypatch.setattr(window, "_on_chart_fetch_error", record_worker_error)
    connection = window._selected_route.connection
    first_request = window._make_chart_load_request(
        "XLE",
        Timeframe.DAILY,
        connection,
    )
    second_request = window._make_chart_load_request(
        "TSLA",
        Timeframe.DAILY,
        connection,
    )
    window._start_chart_load(first_request)
    window._start_chart_load(second_request)

    qtbot.waitUntil(first_two_started.is_set, timeout=1000)
    window._chart_load_timer.setInterval(60_000)
    accepted: list[str] = []
    current_errors: list[str] = []
    monkeypatch.setattr(
        window,
        "_on_fetch_done",
        lambda series: accepted.append(series.symbol),
    )
    monkeypatch.setattr(window, "_on_fetch_error", current_errors.append)
    window._state.symbol = "NVDA"
    window._state.timeframe = Timeframe.DAILY
    production_load(window)

    assert len(window._chart_load_tasks) == 2
    assert window._pending_chart_load is not None
    assert window._pending_chart_load.symbol == "NVDA"
    assert window._chart_load_timer.isActive()
    assert "NVDA" not in fetch_calls

    release_first_two.set()
    qtbot.waitUntil(
        lambda: not {
            first_request.generation,
            second_request.generation,
        }.intersection(window._chart_load_tasks),
        timeout=1000,
    )

    assert window._pending_chart_load is not None
    assert window._pending_chart_load.symbol == "NVDA"
    assert "NVDA" not in fetch_calls
    assert accepted == []
    assert current_errors == []
    assert worker_successes == []
    assert worker_errors == []
    with Cache(window._db_path) as cache:
        cache_end_ms = int(datetime.now(tz=UTC).timestamp() * 1000) + 1_000
        for symbol in ("XLE", "TSLA"):
            assert cache.get_bars(
                connection.cache_namespace,
                symbol,
                Timeframe.DAILY,
                0,
                cache_end_ms,
            )
            assert cache.get_bar_fetch_coverage(
                connection.cache_namespace,
                symbol,
                Timeframe.DAILY,
            ) is not None

    window._chart_load_timer.setInterval(0)
    window._chart_load_timer.start()

    qtbot.waitUntil(
        lambda: not window._chart_load_tasks and accepted == ["NVDA"],
        timeout=1500,
    )

    assert current_errors == []
    assert worker_successes == ["NVDA"]
    assert worker_errors == []
    assert fetch_calls.count("XLE") == 1
    assert fetch_calls.count("TSLA") == 1
    assert fetch_calls.count("NVDA") == 1
    with sqlite3.connect(window._db_path) as connection_check:
        assert connection_check.execute("PRAGMA integrity_check").fetchone() == (
            "ok",
        )


def test_real_alpaca_factory_and_cache_support_two_chart_workers(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _ = _window(qtbot, tmp_path, monkeypatch)
    main_thread_id = threading.get_ident()
    credential_barrier = threading.Barrier(2)
    fetch_barrier = threading.Barrier(2)
    credential_thread_ids: list[int] = []
    credential_thread_lock = threading.Lock()
    worker_successes: list[str] = []
    worker_errors: list[str] = []

    class SyntheticCredentialStore:
        def get(self, _connection_id: str) -> ProviderCredentials | None:
            with credential_thread_lock:
                credential_thread_ids.append(threading.get_ident())
            credential_barrier.wait(timeout=2)
            return ProviderCredentials("synthetic-id", "synthetic-secret")

        def put(
            self,
            _connection_id: str,
            _credentials: ProviderCredentials,
        ) -> None:
            raise AssertionError("chart loading must not write credentials")

        def delete(self, _connection_id: str) -> None:
            raise AssertionError("chart loading must not delete credentials")

    def fetch_test_bars(
        _provider: AlpacaProvider,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        fetch_barrier.wait(timeout=2)
        return [Bar(start, 10.0, 11.0, 9.0, 10.5, 100)]

    production_done = window._on_chart_fetch_done
    production_error = window._on_chart_fetch_error

    def record_worker_success(request: Any, series: OHLCVSeries) -> None:
        worker_successes.append(request.symbol)
        production_done(request, series)

    def record_worker_error(request: Any, message: str) -> None:
        worker_errors.append(f"{request.symbol}: {message}")
        production_error(request, message)

    monkeypatch.setattr(AlpacaProvider, "fetch_bars", fetch_test_bars)
    monkeypatch.setattr(window, "_on_chart_fetch_done", record_worker_success)
    monkeypatch.setattr(window, "_on_chart_fetch_error", record_worker_error)
    window._credential_store = SyntheticCredentialStore()
    connection = alpaca_paper_connection()
    window._selected_route = cast(Any, SimpleNamespace(connection=connection))
    requests = [
        window._make_chart_load_request(symbol, Timeframe.DAILY, connection)
        for symbol in ("XLE", "TSLA")
    ]

    for request in requests:
        window._start_chart_load(request)

    qtbot.waitUntil(lambda: not window._chart_load_tasks, timeout=3000)

    assert sorted(worker_successes) == ["TSLA", "XLE"]
    assert worker_errors == []
    assert len(credential_thread_ids) == 2
    assert len(set(credential_thread_ids)) == 2
    assert main_thread_id not in credential_thread_ids


def test_main_window_constructor_has_no_diagnostics_parameter() -> None:
    assert "diagnostics" not in inspect.signature(MainWindow.__init__).parameters
