from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
import threading
from typing import Any

import pytest
from PyQt6.QtCore import QTimer

import app.controller as controller_module
from app.controller import MainWindow, _DataRoute
from data.aggregator import Aggregator
from data.cache import Cache
from data.models import Bar, Level1Quote, MarketSnapshot, Timeframe
from data.provider import ProviderAvailability
from data.provider.base import DataProvider
from data.provider.config import (
    ProviderConnection,
    alpaca_paper_connection,
    yfinance_connection,
)
from data.provider.credentials import CredentialStore


def _quote(symbol: str, price: float) -> Level1Quote:
    return Level1Quote(
        symbol=symbol, company_name="Test company", last_price=price,
        change=None, change_percent=None, bid=None, bid_size=None,
        ask=None, ask_size=None, open=None, high=None, low=None,
        volume=None, previous_close=None,
    )


class _QuoteProvider(DataProvider):
    def __init__(self, value: float) -> None:
        self.value = value
        self.snapshot_started = threading.Event()
        self.quote_started = threading.Event()
        self.release_snapshots = threading.Event()
        self.release_quotes = threading.Event()
        self.release_snapshots.set()
        self.release_quotes.set()
        self.snapshot_calls: list[list[str]] = []
        self.quote_calls: list[str] = []
        self.snapshot_error = False
        self.quote_error = False
        self.quote_none = False
        self.reference_error = False

    def fetch_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime,
    ) -> list[Bar]:
        return []

    def native_timeframes(self) -> list[Timeframe]:
        return [Timeframe.DAILY]

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        value = self.value
        fail = self.snapshot_error
        self.snapshot_calls.append(list(symbols))
        self.snapshot_started.set()
        self.release_snapshots.wait(timeout=3)
        if fail:
            raise RuntimeError("snapshot failed")
        return {
            symbol: MarketSnapshot(symbol, value, None, value, None, None)
            for symbol in symbols
        }

    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        value = self.value
        fail = self.quote_error
        missing = self.quote_none
        self.quote_calls.append(symbol)
        self.quote_started.set()
        self.release_quotes.wait(timeout=3)
        if fail:
            raise RuntimeError("quote failed")
        return None if missing else _quote(symbol, value)

    def fetch_company_name(self, symbol: str) -> str | None:
        if self.reference_error:
            raise RuntimeError("reference failed")
        return "Test company"


@pytest.fixture
def quote_window(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Iterator[tuple[MainWindow, _QuoteProvider, _QuoteProvider]]:
    if getattr(request, "param", False):
        monkeypatch.setattr(
            controller_module,
            "is_wayland_platform",
            lambda _platform: True,
        )
    real_load = MainWindow._load
    real_snapshots = MainWindow._refresh_watchlist_snapshots
    real_level1 = MainWindow._refresh_level1
    monkeypatch.setattr(MainWindow, "_load", lambda window: None)
    monkeypatch.setattr(MainWindow, "_refresh_watchlist_snapshots", lambda window: None)
    monkeypatch.setattr(MainWindow, "_refresh_level1", lambda window: None)
    window = MainWindow(str(tmp_path / "test.db"))
    qtbot.addWidget(window)
    window._snapshot_timer.stop()
    market = _QuoteProvider(1.0)
    yahoo = _QuoteProvider(20.0)
    window._selected_route = _DataRoute(Aggregator(market), alpaca_paper_connection())
    window._yahoo_route = _DataRoute(Aggregator(yahoo), yfinance_connection())
    for symbol in window._watchlist.symbols():
        window._cache.remove_from_watchlist(symbol)
        window._watchlist.remove_symbol(symbol)
    window._cache.add_to_watchlist("SPY")
    window._watchlist.add_symbol("SPY")
    window._state.symbol = "SPY"
    monkeypatch.setattr(MainWindow, "_load", real_load)
    monkeypatch.setattr(MainWindow, "_refresh_watchlist_snapshots", real_snapshots)
    monkeypatch.setattr(MainWindow, "_refresh_level1", real_level1)
    monkeypatch.setattr(window, "_start_chart_load", lambda request: None)
    try:
        yield window, market, yahoo
    finally:
        for provider in (market, yahoo):
            provider.release_snapshots.set()
            provider.release_quotes.set()
        window._stop_data_workers()
        qtbot.waitUntil(
            lambda: window._snapshot_thread is None
            and window._level1_thread is None
            and not window._chart_load_tasks
            and (
                window._validation_thread is None
                or not window._validation_thread.isRunning()
            ),
            timeout=3000,
        )


def test_old_route_snapshot_is_rejected_and_new_route_refreshes_after_completion(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, market, yahoo = quote_window
    updates: list[dict[str, float | None]] = []
    monkeypatch.setattr(window._watchlist, "set_percent_changes", updates.append)
    market.release_snapshots.clear()
    window._refresh_watchlist_snapshots()
    qtbot.waitUntil(market.snapshot_started.is_set)
    window._commit_provider_route(window._yahoo_route)
    market.release_snapshots.set()
    qtbot.waitUntil(lambda: bool(updates))
    assert all(update.get("SPY") != 1.0 for update in updates)
    qtbot.waitUntil(lambda: any(update.get("SPY") == 20.0 for update in updates))
    assert len(yahoo.snapshot_calls) == 1


def test_same_symbol_level1_rejects_old_route_across_switch_away_and_back(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, market, yahoo = quote_window
    received: list[Level1Quote] = []
    monkeypatch.setattr(window._app_header, "set_quote", received.append)
    market.release_quotes.clear()
    window._refresh_level1()
    qtbot.waitUntil(market.quote_started.is_set)
    original_route = window._selected_route
    window._commit_provider_route(window._yahoo_route)
    window._commit_provider_route(original_route)
    market.value = 2.0
    market.release_quotes.set()
    qtbot.waitUntil(lambda: bool(received))
    assert all(quote.last_price != 1.0 for quote in received)
    qtbot.waitUntil(lambda: received[-1].last_price == 2.0)
    assert yahoo.quote_calls == []


def test_removed_and_readded_symbol_rejects_earlier_membership_snapshot(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, market, _ = quote_window
    updates: list[dict[str, float | None]] = []
    monkeypatch.setattr(window._watchlist, "set_percent_changes", updates.append)
    market.release_snapshots.clear()
    window._refresh_watchlist_snapshots()
    qtbot.waitUntil(market.snapshot_started.is_set)
    window._on_watchlist_remove("SPY")
    window._on_watchlist_add("SPY")
    market.value = 2.0
    market.release_snapshots.set()
    qtbot.waitUntil(lambda: bool(updates))
    assert all(update.get("SPY") != 1.0 for update in updates)
    qtbot.waitUntil(lambda: any(update.get("SPY") == 2.0 for update in updates))


def test_mixed_snapshots_keep_yahoo_futures_across_equity_route_epochs(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, market, _ = quote_window
    window._cache.add_to_watchlist("ES=F")
    window._watchlist.add_symbol("ES=F")
    updates: list[dict[str, float | None]] = []
    monkeypatch.setattr(window._watchlist, "set_percent_changes", updates.append)
    market.release_snapshots.clear()
    window._refresh_watchlist_snapshots()
    qtbot.waitUntil(market.snapshot_started.is_set)
    original_route = window._selected_route
    window._commit_provider_route(window._yahoo_route)
    window._commit_provider_route(original_route)
    market.value = 2.0
    market.release_snapshots.set()
    qtbot.waitUntil(lambda: any(update.get("SPY") == 2.0 for update in updates))
    assert all(update.get("SPY") != 1.0 for update in updates)
    assert {"ES=F": 20.0} in updates
    assert market.snapshot_calls == [["SPY"], ["SPY"]]


def test_level1_rejects_earlier_same_symbol_selection_episode(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, market, _ = quote_window
    received: list[Level1Quote] = []
    monkeypatch.setattr(window._app_header, "set_quote", received.append)
    market.release_quotes.clear()
    window._refresh_level1()
    qtbot.waitUntil(market.quote_started.is_set)
    window._on_symbol_changed("QQQ")
    window._on_symbol_changed("SPY")
    market.value = 2.0
    market.release_quotes.set()
    qtbot.waitUntil(lambda: bool(received))
    assert all(quote.last_price != 1.0 for quote in received)
    assert market.quote_calls == ["SPY", "SPY"]


def test_stale_quote_errors_do_not_reach_new_route_status(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, market, yahoo = quote_window
    status = window.statusBar()
    assert status is not None
    messages: list[str] = []

    def show_message(message: str, timeout: int = 0) -> None:
        messages.append(message)

    monkeypatch.setattr(status, "showMessage", show_message)
    market.snapshot_error = True
    market.quote_error = True
    market.release_snapshots.clear()
    market.release_quotes.clear()
    window._refresh_watchlist_snapshots()
    window._refresh_level1()
    qtbot.waitUntil(market.snapshot_started.is_set)
    qtbot.waitUntil(market.quote_started.is_set)
    window._commit_provider_route(window._yahoo_route)
    market.release_snapshots.set()
    market.release_quotes.set()
    qtbot.waitUntil(lambda: bool(yahoo.snapshot_calls) and bool(yahoo.quote_calls))
    qtbot.waitUntil(lambda: window._snapshot_thread is None and window._level1_thread is None)
    assert all("failed" not in message for message in messages)


def test_stale_quote_errors_do_not_reach_route_after_switch_away_and_back(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, market, yahoo = quote_window
    status = window.statusBar()
    assert status is not None
    messages: list[str] = []
    snapshots: list[dict[str, float | None]] = []
    quotes: list[Level1Quote] = []
    monkeypatch.setattr(status, "showMessage", lambda message, timeout=0: messages.append(message))
    monkeypatch.setattr(window._watchlist, "set_percent_changes", snapshots.append)
    monkeypatch.setattr(window._app_header, "set_quote", quotes.append)
    market.snapshot_error = True
    market.quote_error = True
    market.release_snapshots.clear()
    market.release_quotes.clear()
    window._refresh_watchlist_snapshots()
    window._refresh_level1()
    qtbot.waitUntil(market.snapshot_started.is_set)
    qtbot.waitUntil(market.quote_started.is_set)

    original_route = window._selected_route
    window._commit_provider_route(window._yahoo_route)
    window._commit_provider_route(original_route)
    market.snapshot_error = False
    market.quote_error = False
    market.value = 2.0
    market.release_snapshots.set()
    market.release_quotes.set()

    qtbot.waitUntil(lambda: len(market.snapshot_calls) == 2 and len(market.quote_calls) == 2)
    qtbot.waitUntil(lambda: window._snapshot_thread is None and window._level1_thread is None)
    assert snapshots and all(update.get("SPY") != 1.0 for update in snapshots)
    assert snapshots[-1].get("SPY") == 2.0
    assert quotes and quotes[-1].last_price == 2.0
    assert all("failed" not in message for message in messages)
    assert yahoo.snapshot_calls == []
    assert yahoo.quote_calls == []


def test_stale_level1_error_does_not_reach_reselected_symbol_episode(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, market, _ = quote_window
    status = window.statusBar()
    assert status is not None
    messages: list[str] = []
    quotes: list[Level1Quote] = []
    monkeypatch.setattr(status, "showMessage", lambda message, timeout=0: messages.append(message))
    monkeypatch.setattr(window._app_header, "set_quote", quotes.append)
    market.quote_error = True
    market.release_quotes.clear()
    window._refresh_level1()
    qtbot.waitUntil(market.quote_started.is_set)

    window._on_symbol_changed("QQQ")
    window._on_symbol_changed("SPY")
    market.quote_error = False
    market.value = 2.0
    market.release_quotes.set()

    qtbot.waitUntil(lambda: len(market.quote_calls) == 2)
    qtbot.waitUntil(lambda: window._level1_thread is None)
    assert quotes and quotes[-1].last_price == 2.0
    assert all(quote.last_price != 1.0 for quote in quotes)
    assert all("failed" not in message for message in messages)


def test_stopping_workers_rejects_saved_quote_successes_and_errors(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, market, _ = quote_window
    status = window.statusBar()
    assert status is not None
    messages: list[str] = []
    snapshots: list[dict[str, float | None]] = []
    quotes: list[Level1Quote] = []
    monkeypatch.setattr(status, "showMessage", lambda message, timeout=0: messages.append(message))
    monkeypatch.setattr(window._watchlist, "set_percent_changes", snapshots.append)
    monkeypatch.setattr(window._app_header, "set_quote", quotes.append)
    market.release_snapshots.clear()
    market.release_quotes.clear()
    window._refresh_watchlist_snapshots()
    window._refresh_level1()
    qtbot.waitUntil(market.snapshot_started.is_set)
    qtbot.waitUntil(market.quote_started.is_set)
    snapshot_request = window._snapshot_request
    level1_request = window._level1_request
    assert snapshot_request is not None
    assert level1_request is not None
    market.release_snapshots.set()
    market.release_quotes.set()
    qtbot.waitUntil(lambda: window._snapshot_thread is None and window._level1_thread is None)
    snapshots.clear()
    quotes.clear()
    messages.clear()

    window._stop_data_workers()
    window._on_watchlist_snapshots(snapshot_request, {
        "SPY": MarketSnapshot("SPY", 1.0, None, 1.0, None, None),
    })
    window._on_watchlist_snapshot_error(snapshot_request, "stale snapshot failure")
    window._on_level1(level1_request, _quote("SPY", 1.0))
    window._on_level1_error(level1_request, "stale quote failure")

    assert snapshots == []
    assert quotes == []
    assert messages == []


@pytest.mark.parametrize("validation_fails", [False, True])
def test_close_drains_chart_quotes_and_validation_while_gui_stays_responsive(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
    validation_fails: bool,
) -> None:
    window, market, yahoo = quote_window
    market.release_snapshots.clear()
    market.release_quotes.clear()
    yahoo.release_snapshots.set()
    window._cache.add_to_watchlist("ES=F")
    window._watchlist.add_symbol("ES=F")
    snapshot_started = market.snapshot_started
    quote_started = market.quote_started
    chart_started = threading.Event()
    release_chart = threading.Event()
    validation_started = threading.Event()
    release_validation = threading.Event()
    chart_calls: list[str] = []
    validation_calls: list[str] = []
    provider_factory_calls: list[str] = []
    reference_calls: list[str] = []
    snapshot_completed: list[bool] = []
    level1_completed: list[bool] = []
    heartbeat: list[bool] = []
    cache_closes: list[bool] = []
    warnings: list[str] = []
    main_thread = threading.get_ident()
    original_close = window._cache.close

    def close_cache() -> None:
        cache_closes.append(True)
        original_close()

    def record_reference_call(symbol: str) -> str:
        reference_calls.append(symbol)
        return "Yahoo company"

    def record_heartbeat() -> None:
        heartbeat.append(True)

    def release_all_fallback() -> None:
        release_chart.set()
        market.release_snapshots.set()
        market.release_quotes.set()
        release_validation.set()

    status = window.statusBar()
    assert status is not None
    central_widget = window.centralWidget()
    assert central_widget is not None

    monkeypatch.setattr(window._cache, "close", close_cache)
    monkeypatch.setattr(
        yahoo,
        "fetch_company_name",
        record_reference_call,
    )

    class ChartProvider(DataProvider):
        def fetch_bars(
            self,
            symbol: str,
            timeframe: Timeframe,
            start: datetime,
            end: datetime,
        ) -> list[Bar]:
            chart_calls.append(symbol)
            chart_started.set()
            if not release_chart.wait(timeout=4):
                raise RuntimeError("chart provider was not released")
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

    class ValidationProvider(DataProvider):
        def fetch_bars(
            self,
            symbol: str,
            timeframe: Timeframe,
            start: datetime,
            end: datetime,
        ) -> list[Bar]:
            return []

        def fetch_snapshots(
            self,
            _symbols: list[str],
        ) -> dict[str, MarketSnapshot]:
            return {}

        def fetch_level1(self, symbol: str) -> Level1Quote | None:
            validation_calls.append(symbol)
            validation_started.set()
            if not release_validation.wait(timeout=4):
                raise RuntimeError("validation provider was not released")
            if validation_fails:
                raise RuntimeError("validation failed after closing")
            return _quote(symbol, 30.0)

        def native_timeframes(self) -> list[Timeframe]:
            return [Timeframe.DAILY]

    chart_provider = ChartProvider()
    validation_provider = ValidationProvider()

    def create_test_provider(
        connection: ProviderConnection,
        _credential_store: CredentialStore,
    ) -> DataProvider:
        provider_factory_calls.append(connection.connection_id)
        return (
            validation_provider
            if threading.get_ident() == main_thread
            else chart_provider
        )

    monkeypatch.setattr(controller_module, "create_provider", create_test_provider)
    monkeypatch.setattr(
        controller_module,
        "show_warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    monkeypatch.setattr(
        window,
        "_start_chart_load",
        MainWindow._start_chart_load.__get__(window, MainWindow),
    )
    window._provider_availability["alpaca"] = ProviderAvailability(True, None)
    connection = next(
        item
        for item in window._cache.get_provider_connections()
        if item.provider_name == "alpaca"
    )
    original_route = window._selected_route

    window._refresh_watchlist_snapshots()
    window._refresh_level1()
    chart_request = window._make_chart_load_request(
        "NVDA",
        Timeframe.DAILY,
        window._selected_route.connection,
    )
    window._current_chart_request = chart_request
    window._state.symbol = chart_request.symbol
    window._state.timeframe = chart_request.timeframe
    window._start_chart_load(chart_request)
    window._request_provider_activation(connection.connection_id)
    qtbot.waitUntil(validation_started.is_set, timeout=1000)
    pending_validation_route = window._pending_route
    assert pending_validation_route is not None
    window.show()
    timer = QTimer(window)
    timer.setInterval(10)
    timer.timeout.connect(record_heartbeat)
    timer.start()

    # Ensure a failing implementation that blocks close drains its workers.
    release_fallback = threading.Timer(
        1.5,
        release_all_fallback,
    )
    release_fallback.daemon = True
    release_fallback.start()
    try:
        qtbot.waitUntil(snapshot_started.is_set, timeout=1000)
        qtbot.waitUntil(quote_started.is_set, timeout=1000)
        qtbot.waitUntil(chart_started.is_set, timeout=1000)
        qtbot.waitUntil(validation_started.is_set, timeout=1000)
        assert window._snapshot_worker is not None
        assert window._level1_worker is not None
        window._snapshot_worker.completed.connect(
            lambda: snapshot_completed.append(True)
        )
        window._level1_worker.completed.connect(
            lambda: level1_completed.append(True)
        )
        QTimer.singleShot(0, window.close)
        qtbot.waitUntil(
            lambda: status.currentMessage() == "Closing...",
            timeout=500,
        )
        heartbeat_after_close = len(heartbeat)
        assert window.isVisible()
        assert central_widget.isEnabled()
        assert not window._app_header.isEnabled()
        assert not window._watchlist.isEnabled()
        assert not window._symbol_bar.isEnabled()
        assert not window._chart.isEnabled()
        assert cache_closes == []
        window._request_provider_activation(connection.connection_id)
        assert provider_factory_calls == [
            connection.connection_id,
            chart_request.connection.connection_id,
        ]
        callback_delivered = threading.Event()

        def deliver_queued_validation_callbacks() -> None:
            window._on_provider_validation_error("late validation error")
            window._pending_route = pending_validation_route
            window._on_provider_validation_done()
            callback_delivered.set()

        QTimer.singleShot(0, deliver_queued_validation_callbacks)
        qtbot.waitUntil(callback_delivered.is_set, timeout=500)
        assert status.currentMessage() == "Closing..."
        assert window._pending_route is pending_validation_route
        assert window._selected_route is original_route
        assert warnings == []
        window.close()
        qtbot.waitUntil(
            lambda: len(heartbeat) >= heartbeat_after_close + 2,
            timeout=500,
        )

        release_chart.set()
        qtbot.waitUntil(lambda: not window._chart_load_tasks, timeout=1000)
        assert window.isVisible()
        assert cache_closes == []

        market.release_snapshots.set()
        qtbot.waitUntil(lambda: window._snapshot_thread is None, timeout=1000)
        qtbot.waitUntil(lambda: snapshot_completed == [True], timeout=1000)
        assert yahoo.snapshot_calls == []
        assert cache_closes == []

        market.release_quotes.set()
        qtbot.waitUntil(lambda: window._level1_thread is None, timeout=1000)
        qtbot.waitUntil(lambda: level1_completed == [True], timeout=1000)
        assert reference_calls == []
        assert cache_closes == []

        release_validation.set()
        qtbot.waitUntil(lambda: window._validation_thread is None, timeout=1000)
        qtbot.waitUntil(lambda: not window.isVisible(), timeout=1000)
        assert window._selected_route is original_route
        assert window._pending_route is None
        assert warnings == []
        assert cache_closes == [True]
    finally:
        release_fallback.cancel()
        release_chart.set()
        market.release_snapshots.set()
        market.release_quotes.set()
        release_validation.set()
        timer.stop()


def test_close_without_workers_defers_final_close_and_closes_cache_once(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _, _ = quote_window
    cache_closes: list[bool] = []
    original_close = window._cache.close

    def close_cache() -> None:
        cache_closes.append(True)
        original_close()

    monkeypatch.setattr(window._cache, "close", close_cache)
    window.show()

    window.close()
    assert window.isVisible()
    assert cache_closes == []
    window.close()

    qtbot.waitUntil(lambda: not window.isVisible(), timeout=1000)
    assert cache_closes == [True]


@pytest.mark.parametrize("quote_window", [True], indirect=True)
def test_custom_chrome_stays_enabled_and_minimizes_during_close(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any,
) -> None:
    window, market, _ = quote_window
    title_bar = window._window_title_bar
    assert title_bar is not None
    assert window._uses_custom_window_chrome
    market.release_quotes.clear()
    window._refresh_level1()
    qtbot.waitUntil(market.quote_started.is_set, timeout=1000)
    window.show()
    QTimer.singleShot(0, window.close)
    status = window.statusBar()
    assert status is not None
    qtbot.waitUntil(
        lambda: status.currentMessage() == "Closing...",
        timeout=1000,
    )

    central_widget = window.centralWidget()
    assert central_widget is not None
    assert title_bar._minimize_button.isEnabled()
    assert central_widget.isEnabled()
    assert not window._app_header.isEnabled()
    assert not window._watchlist.isEnabled()
    assert not window._symbol_bar.isEnabled()
    assert not window._chart.isEnabled()
    title_bar._minimize_button.click()
    assert window.isMinimized()

    market.release_quotes.set()
    qtbot.waitUntil(lambda: window._level1_thread is None, timeout=1000)
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=1000)


def test_quote_errors_finish_and_allow_current_route_retry(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, market, _ = quote_window
    updates: list[dict[str, float | None]] = []
    received: list[Level1Quote] = []
    monkeypatch.setattr(window._watchlist, "set_percent_changes", updates.append)
    monkeypatch.setattr(window._app_header, "set_quote", received.append)
    status = window.statusBar()
    assert status is not None
    messages: list[str] = []

    def show_message(message: str, timeout: int = 0) -> None:
        messages.append(message)

    monkeypatch.setattr(status, "showMessage", show_message)
    market.snapshot_error = True
    market.quote_error = True
    window._refresh_watchlist_snapshots()
    window._refresh_level1()
    qtbot.waitUntil(lambda: window._snapshot_thread is None and window._level1_thread is None)
    assert updates == []
    assert received == []
    assert window._snapshot_worker is None
    assert window._level1_worker is None
    assert any("snapshot failed" in message for message in messages)
    assert any("quote failed" in message for message in messages)
    market.snapshot_error = False
    market.quote_error = False
    window._refresh_watchlist_snapshots()
    window._refresh_level1()
    qtbot.waitUntil(lambda: bool(updates) and bool(received))


def test_level1_cancellation_during_cache_open_skips_provider_call(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, market, _ = quote_window
    cache_started = threading.Event()
    release_cache = threading.Event()
    completed: list[bool] = []

    class BlockingCache(Cache):
        def __init__(self, db_path: str) -> None:
            cache_started.set()
            if not release_cache.wait(timeout=3):
                raise RuntimeError("test cache was not released")
            super().__init__(db_path)

    monkeypatch.setattr(controller_module, "Cache", BlockingCache)
    try:
        window._refresh_level1()
        qtbot.waitUntil(cache_started.is_set, timeout=1000)
        worker = window._level1_worker
        assert worker is not None
        worker.completed.connect(lambda: completed.append(True))
        window._stop_data_workers()
    finally:
        release_cache.set()

    qtbot.waitUntil(lambda: window._level1_thread is None, timeout=1000)
    qtbot.waitUntil(lambda: completed == [True], timeout=1000)
    assert market.quote_calls == []


@pytest.mark.parametrize("outcome", ["success", "none", "provider_error", "reference_error", "cache_error"])
def test_level1_worker_owns_cache_and_cleans_up_all_terminal_paths(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    window, market, yahoo = quote_window
    main_thread = threading.get_ident()
    opened: list[int] = []
    closed: list[int] = []
    entered = threading.Event()
    release = threading.Event()

    class TrackedCache(Cache):
        def __init__(self, db_path: str) -> None:
            entered.set()
            release.wait(timeout=3)
            if outcome == "cache_error":
                raise RuntimeError("cache failed")
            super().__init__(db_path)
            opened.append(threading.get_ident())

        def close(self) -> None:
            super().close()
            closed.append(threading.get_ident())

    monkeypatch.setattr(controller_module, "Cache", TrackedCache)
    market.quote_none = outcome == "none"
    market.quote_error = outcome == "provider_error"
    yahoo.reference_error = outcome == "reference_error"
    window._refresh_level1()
    terminal: list[bool] = []
    results: list[Level1Quote | None] = []
    errors: list[str] = []
    try:
        qtbot.waitUntil(entered.is_set)
        worker = window._level1_worker
        assert worker is not None
        worker.completed.connect(lambda: terminal.append(True))
        worker.finished.connect(lambda request, quote: results.append(quote))
        worker.error.connect(lambda request, message: errors.append(message))
    finally:
        release.set()
    qtbot.waitUntil(lambda: window._level1_thread is None)
    assert terminal == [True]
    assert len(results) == (1 if outcome in ("success", "none") else 0)
    assert len(errors) == (0 if outcome in ("success", "none") else 1)
    assert window._level1_worker is None
    assert len(opened) == len(closed) == (0 if outcome == "cache_error" else 1)
    assert all(identity != main_thread for identity in opened + closed)
    assert window._cache.get_watchlist() == ["SPY"]
    if outcome == "success":
        assert window._cache.get_asset_reference("SPY") is not None


def test_stopping_workers_clears_replacements_and_ignores_late_quote_delivery(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, market, yahoo = quote_window
    received: list[Level1Quote] = []
    monkeypatch.setattr(window._app_header, "set_quote", received.append)
    market.release_quotes.clear()
    window._refresh_level1()
    qtbot.waitUntil(market.quote_started.is_set)
    window._commit_provider_route(window._yahoo_route)
    market.release_quotes.set()
    window._stop_data_workers()
    qtbot.waitUntil(lambda: window._level1_thread is None)
    window._refresh_level1()
    window._refresh_watchlist_snapshots()
    assert not window._level1_refresh_pending
    assert yahoo.quote_calls == []
    assert received == []


def test_reordering_and_periodic_ticks_do_not_queue_extra_refreshes(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any,
) -> None:
    window, market, _ = quote_window
    window._cache.add_to_watchlist("QQQ")
    window._watchlist.add_symbol("QQQ")
    market.release_snapshots.clear()
    market.release_quotes.clear()
    window._refresh_watchlist_snapshots()
    window._refresh_level1()
    qtbot.waitUntil(market.snapshot_started.is_set)
    qtbot.waitUntil(market.quote_started.is_set)
    window._watchlist.remove_symbol("SPY")
    window._watchlist.add_symbol("SPY")
    assert window._watchlist.symbols() == ["QQQ", "SPY"]
    window._on_watchlist_reorder(["QQQ", "SPY"])
    for _ in range(3):
        window._refresh_watchlist_snapshots()
        window._refresh_level1()
    market.release_snapshots.set()
    market.release_quotes.set()
    qtbot.waitUntil(lambda: window._snapshot_thread is None and window._level1_thread is None)
    assert len(market.snapshot_calls) == len(market.quote_calls) == 1
    assert window._snapshot_timer.interval() == 60_000


def test_level1_futures_identity_stays_valid_when_selected_equity_route_changes(
    quote_window: tuple[MainWindow, _QuoteProvider, _QuoteProvider],
    qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _, yahoo = quote_window
    window._state.symbol = "ES=F"
    received: list[Level1Quote] = []
    monkeypatch.setattr(window._app_header, "set_quote", received.append)
    yahoo.release_quotes.clear()
    window._refresh_level1()
    qtbot.waitUntil(yahoo.quote_started.is_set)
    window._commit_provider_route(window._yahoo_route)
    yahoo.release_quotes.set()
    qtbot.waitUntil(lambda: window._level1_thread is None)
    assert [quote.symbol for quote in received] == ["ES=F"]
    assert yahoo.quote_calls == ["ES=F"]
