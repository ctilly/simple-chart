"""Production display paths of MainWindow show only shielded messages.

Each failure is what a real provider method raised for a real library
failure (or a real library exception the provider leaves untranslated),
carrying a synthetic secret; the fake provider or store only re-raises it
where the real provider call would.
"""

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import finplot as fplt
import pytest

import app.controller as controller_module
from app.controller import (
    MainWindow,
    _DataRoute,
    _FetchWorker,
    _Level1Request,
    _Level1Worker,
    _ProviderValidationWorker,
    _RefreshReload,
    _SnapshotBatch,
    _SnapshotWorker,
)
from chart.window import ChartWidget
from data.aggregator import Aggregator
from data.models import Bar, Level1Quote, MarketSnapshot, OHLCVSeries, Timeframe
from data.errors import UserFacingError
from data.provider import ProviderAvailability
from data.provider.base import DataProvider
from data.provider.config import (
    ALPACA_PAPER_CONNECTION_ID,
    alpaca_paper_connection,
)
from data.provider.credentials import (
    CredentialStoreAccess,
    ProviderCredentials,
    UnavailableCredentialStore,
    initialize_keyring_credential_store,
)
from realistic_errors import (
    PROVIDER_SOURCES,
    SECRET,
    SOURCES,
    LocalErrorServer,
    SecretLeakingKeyring,
    assert_secret_absent,
)
from ui_text import capture_message_dialogs, widget_text


_FIRST = datetime(2026, 6, 1, 13, 30, tzinfo=UTC)

# Failure source -> expected message template. A provider's neutral error
# carries its own text; anything else gets the generic message.
_SITE_SOURCES: dict[str, str] = {
    "alpaca_invalid_header": (
        "The saved Alpaca Paper credentials are malformed. Re-enter them in Settings."
    ),
    "alpaca_http_client_invalid_header": "{action} failed. Details were logged.",
    "alpaca_api_401": "Alpaca Paper rejected the credentials.",
    "yfinance_connection_error": "Couldn't reach Yahoo Finance. Check your connection.",
    "unknown": "{action} failed. Details were logged.",
}
_ALL_SOURCES = {**SOURCES, **PROVIDER_SOURCES}


@pytest.fixture(autouse=True)
def _isolated_finplot_windows() -> Iterator[None]:
    fplt.windows.clear()
    yield
    fplt.windows.clear()


@pytest.fixture(scope="module")
def error_server() -> Iterator[LocalErrorServer]:
    with LocalErrorServer() as server:
        yield server


def _real_error(server: LocalErrorServer, label: str) -> BaseException:
    return _ALL_SOURCES[label](server)


def _expected(label: str, action: str) -> str:
    return _SITE_SOURCES[label].format(action=action)


class _FailingProvider(DataProvider):
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        raise self._exc

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        raise self._exc

    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        raise self._exc

    def native_timeframes(self) -> list[Timeframe]:
        return [Timeframe.MIN1, Timeframe.MIN5, Timeframe.DAILY, Timeframe.WEEKLY]


class _MemoryCredentialStore:
    def __init__(self) -> None:
        self.credentials: dict[str, ProviderCredentials] = {}

    def get(self, connection_id: str) -> ProviderCredentials | None:
        return self.credentials.get(connection_id)

    def put(self, connection_id: str, credentials: ProviderCredentials) -> None:
        self.credentials[connection_id] = credentials

    def delete(self, connection_id: str) -> None:
        self.credentials.pop(connection_id, None)


def _window(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    access: CredentialStoreAccess | None = None,
    connection_id: str | None = None,
) -> MainWindow:
    # Never touch the host OS keyring from these tests.
    credential_access = access or CredentialStoreAccess(
        UnavailableCredentialStore("test"), False, "test"
    )
    monkeypatch.setattr(
        controller_module,
        "initialize_keyring_credential_store",
        lambda: credential_access,
    )
    production_load = MainWindow._load
    monkeypatch.setattr(MainWindow, "_load", lambda window: None)
    monkeypatch.setattr(MainWindow, "_refresh_watchlist_snapshots", lambda window: None)
    monkeypatch.setattr(MainWindow, "_refresh_level1", lambda window: None)
    window = MainWindow(str(tmp_path / "test.db"), connection_id)
    qtbot.addWidget(window)
    monkeypatch.setattr(MainWindow, "_load", production_load)
    return window


def _foreign_messages(exc: BaseException) -> list[str]:
    # Text of every exception in the chain that SimpleChart did not compose.
    messages: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        if not isinstance(current, UserFacingError) and str(current):
            messages.append(str(current))
        current = current.__cause__ or current.__context__
    return messages


def _assert_shielded(
    texts: list[str],
    exc: BaseException,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for text in (*texts, caplog.text):
        assert_secret_absent(text)
        for foreign in _foreign_messages(exc):
            assert foreign not in text
    # The site logged describe()'s safe facts.
    assert type(exc).__qualname__ in caplog.text


def _status_text(window: MainWindow) -> str:
    status_bar = window.statusBar()
    assert status_bar is not None
    return status_bar.currentMessage()


def _current_request(window: MainWindow, timeframe: Timeframe) -> Any:
    request = window._make_chart_load_request(
        "XBI", timeframe, window._selected_route.connection,
    )
    window._current_chart_request = request
    window._state.symbol = "XBI"
    window._state.timeframe = timeframe
    return request


def _series(count: int) -> OHLCVSeries:
    return OHLCVSeries(
        "XBI",
        Timeframe.MIN5,
        [
            Bar(_FIRST + timedelta(minutes=5 * i), 100.0, 101.0, 99.0, 100.5, 1_000)
            for i in range(count)
        ],
    )


def _capture_logs(caplog: pytest.LogCaptureFixture) -> None:
    # Harness libraries may log the secret while producing the failure;
    # only what the display path writes afterwards is under test, at the
    # production threshold.
    caplog.clear()
    caplog.set_level(logging.WARNING)


@pytest.mark.parametrize("label", sorted(_SITE_SOURCES))
def test_whole_load_error_dialog_and_refresh_status_are_shielded(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_server: LocalErrorServer,
    label: str,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    shown = capture_message_dialogs(monkeypatch)
    exc = _real_error(error_server, label)
    monkeypatch.setattr(
        controller_module,
        "create_provider",
        lambda connection, store: _FailingProvider(exc),
    )
    request = _current_request(window, Timeframe.DAILY)
    window._refresh_reload = _RefreshReload(request, datetime.now(tz=UTC))
    _capture_logs(caplog)

    window._start_chart_load(request)
    qtbot.waitUntil(lambda: bool(shown) and not window._chart_load_tasks, timeout=5000)

    assert [(item.title, item.message) for item in shown] == [
        ("Load Error", _expected(label, "Loading XBI daily history")),
    ]
    status = window.symbol_refresh_status(request.cache_namespace, "XBI")
    assert status is not None
    assert status.text == (
        "XBI refresh stopped: the download failed. "
        "The missing data downloads on the next load."
    )
    _assert_shielded(
        [shown[0].all_text, status.text, widget_text(window)], exc, caplog
    )


@pytest.mark.parametrize("label", sorted(_SITE_SOURCES))
def test_background_history_error_status_bar_is_shielded(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_server: LocalErrorServer,
    label: str,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    shown = capture_message_dialogs(monkeypatch)
    exc = _real_error(error_server, label)
    monkeypatch.setattr(
        controller_module,
        "create_provider",
        lambda connection, store: _FailingProvider(exc),
    )
    request = _current_request(window, Timeframe.MIN5)
    window._on_chart_recent_ready(request, _series(300))
    worker = _FetchWorker(window._db_path, window._credential_store, request)
    worker.error.connect(window._on_chart_fetch_error)
    _capture_logs(caplog)

    worker.run()

    expected = _expected(label, "Loading XBI 5m history")
    assert shown == []
    assert _status_text(window) == (
        f"Older history could not be loaded: {expected} "
        "It will be retried on the next load."
    )
    _assert_shielded([widget_text(window)], exc, caplog)


@pytest.mark.parametrize("label", sorted(_SITE_SOURCES))
def test_quote_error_status_bar_is_shielded(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_server: LocalErrorServer,
    label: str,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    exc = _real_error(error_server, label)
    window._state.symbol = "XBI"
    request = _Level1Request(
        "XBI",
        window._level1_symbol_generation,
        window._quote_route_identity("XBI"),
    )
    aggregator = Aggregator(_FailingProvider(exc))
    worker = _Level1Worker(
        aggregator,
        aggregator,
        window._db_path,
        request,
        False,
        datetime.now(tz=UTC),
    )
    worker.error.connect(window._on_level1_error)
    _capture_logs(caplog)

    worker.run()

    expected = _expected(label, "Loading the XBI quote")
    assert _status_text(window) == f"Quote unavailable: {expected}"
    _assert_shielded([widget_text(window)], exc, caplog)


@pytest.mark.parametrize("label", sorted(_SITE_SOURCES))
def test_watchlist_quote_error_status_bar_is_shielded(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_server: LocalErrorServer,
    label: str,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    exc = _real_error(error_server, label)
    worker = _SnapshotWorker(
        [_SnapshotBatch(Aggregator(_FailingProvider(exc)), ["XBI"])],
        window._make_snapshot_request(),
    )
    worker.error.connect(window._on_watchlist_snapshot_error)
    _capture_logs(caplog)

    worker.run()

    expected = _expected(label, "Loading watchlist quotes")
    assert _status_text(window) == f"Watchlist quotes unavailable: {expected}"
    _assert_shielded([widget_text(window)], exc, caplog)


@pytest.mark.parametrize("label", sorted(_SITE_SOURCES))
def test_provider_validation_error_dialog_is_shielded(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_server: LocalErrorServer,
    label: str,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    shown = capture_message_dialogs(monkeypatch)
    exc = _real_error(error_server, label)
    worker = _ProviderValidationWorker(_FailingProvider(exc), "Alpaca Paper")
    worker.error.connect(window._on_provider_validation_error)
    _capture_logs(caplog)

    worker.run()

    expected = _expected(label, "Testing Alpaca Paper")
    assert [(item.title, item.message) for item in shown] == [
        ("Connection Failed", f"Could not activate the selected provider.\n\n{expected}"),
    ]
    _assert_shielded([shown[0].all_text, widget_text(window)], exc, caplog)


def _available_alpaca(window: MainWindow) -> None:
    window._provider_availability = {
        "yfinance": ProviderAvailability(True, None),
        "alpaca": ProviderAvailability(True, None),
    }


@pytest.mark.parametrize(
    ("operation", "reason"),
    [
        ("read", "Unable to read provider credentials from the OS keyring."),
        ("unavailable", "The operating-system credential store is unavailable."),
    ],
)
def test_provider_configure_credential_store_error_is_shielded(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    operation: str,
    reason: str,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    shown = capture_message_dialogs(monkeypatch)
    access = initialize_keyring_credential_store(
        SecretLeakingKeyring(SECRET, available=operation == "read")
    )
    window._credential_store = access.store
    _available_alpaca(window)
    with pytest.raises(RuntimeError) as raised:
        access.store.get(ALPACA_PAPER_CONNECTION_ID)
    _capture_logs(caplog)

    window._request_provider_activation(ALPACA_PAPER_CONNECTION_ID)

    assert [(item.title, item.message) for item in shown] == [
        (
            "Connection Failed",
            f"Could not configure Alpaca Paper.\n\n{reason}",
        ),
    ]
    assert window._validation_thread is None
    _assert_shielded([shown[0].all_text, widget_text(window)], raised.value, caplog)


def test_provider_configure_missing_credentials_is_shielded(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    shown = capture_message_dialogs(monkeypatch)
    window._credential_store = _MemoryCredentialStore()
    _available_alpaca(window)
    _capture_logs(caplog)

    window._request_provider_activation(ALPACA_PAPER_CONNECTION_ID)

    assert [(item.title, item.message) for item in shown] == [
        (
            "Connection Failed",
            "Could not configure Alpaca Paper.\n\n"
            "Alpaca Paper credentials are not configured.",
        ),
    ]


def test_startup_unavailable_provider_status_names_the_provider_once(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The saved Alpaca connection has no credentials in the store.
    _capture_logs(caplog)

    window = _window(
        qtbot,
        tmp_path,
        monkeypatch,
        CredentialStoreAccess(_MemoryCredentialStore(), True, None),
        ALPACA_PAPER_CONNECTION_ID,
    )

    status = _status_text(window)
    assert status == (
        "Alpaca Paper credentials are not configured. "
        "Using Yahoo Finance for this session."
    )
    assert status.count("Alpaca Paper") == 1
    assert "ProviderConfigurationError" in caplog.text


def _occupied_panel_limit(render_target: str) -> Any:
    # The real limit branch, without building extension panels.
    occupied = SimpleNamespace(
        _extension_slots=[
            SimpleNamespace(name=name) for name in ("rsi", "macd", "volume_profile")
        ]
    )
    return ChartWidget.ensure_extension_panel(cast(ChartWidget, occupied), render_target)


def _raise_unknown(render_target: str) -> Any:
    raise RuntimeError(f"panel failure with key {SECRET}")


@pytest.mark.parametrize(
    ("ensure_panel", "expected"),
    [
        (
            _occupied_panel_limit,
            "All 3 extension panel slots are in use. "
            "Remove an existing panel extension before adding another.",
        ),
        (_raise_unknown, "Adding the indicator panel failed. Details were logged."),
    ],
)
def test_panel_limit_dialog_shows_guidance_and_shields_other_errors(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ensure_panel: Callable[[str], Any],
    expected: str,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    shown = capture_message_dialogs(monkeypatch)
    monkeypatch.setattr(window._chart, "ensure_extension_panel", ensure_panel)
    _capture_logs(caplog)

    window._draw_extension_render(cast(Any, SimpleNamespace(render_target="stochastic")))

    assert [(item.title, item.message) for item in shown] == [
        ("Panel Limit Reached", expected),
    ]
    assert_secret_absent(shown[0].all_text)
    assert_secret_absent(caplog.text)


@pytest.fixture
def wired_window(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[MainWindow]:
    # Quote and validation workers run on their own threads through the
    # production dispatch methods.
    real_snapshots = MainWindow._refresh_watchlist_snapshots
    real_level1 = MainWindow._refresh_level1
    window = _window(qtbot, tmp_path, monkeypatch)
    window._snapshot_timer.stop()
    for symbol in window._watchlist.symbols():
        window._cache.remove_from_watchlist(symbol)
        window._watchlist.remove_symbol(symbol)
    window._cache.add_to_watchlist("SPY")
    window._watchlist.add_symbol("SPY")
    window._state.symbol = "SPY"
    monkeypatch.setattr(MainWindow, "_refresh_watchlist_snapshots", real_snapshots)
    monkeypatch.setattr(MainWindow, "_refresh_level1", real_level1)
    monkeypatch.setattr(window, "_start_chart_load", lambda request: None)
    try:
        yield window
    finally:
        window._stop_data_workers()
        qtbot.waitUntil(
            lambda: window._snapshot_thread is None
            and window._level1_thread is None
            and (
                window._validation_thread is None
                or not window._validation_thread.isRunning()
            ),
            timeout=3000,
        )


@pytest.mark.parametrize(
    ("dispatch", "expected"),
    [
        (
            "_refresh_watchlist_snapshots",
            "Watchlist quotes unavailable: The saved Alpaca Paper credentials "
            "are malformed. Re-enter them in Settings.",
        ),
        (
            "_refresh_level1",
            "Quote unavailable: The saved Alpaca Paper credentials are malformed. "
            "Re-enter them in Settings.",
        ),
    ],
)
def test_threaded_quote_errors_reach_status_bar_shielded(
    wired_window: MainWindow,
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_server: LocalErrorServer,
    dispatch: str,
    expected: str,
) -> None:
    window = wired_window
    exc = _real_error(error_server, "alpaca_invalid_header")
    window._selected_route = _DataRoute(
        Aggregator(_FailingProvider(exc)),
        alpaca_paper_connection(),
    )
    status = window.statusBar()
    assert status is not None
    messages: list[str] = []
    monkeypatch.setattr(
        status,
        "showMessage",
        lambda message, timeout=0: messages.append(message),
    )
    _capture_logs(caplog)

    getattr(window, dispatch)()
    qtbot.waitUntil(lambda: bool(messages), timeout=3000)

    assert messages == [expected]
    _assert_shielded([*messages, widget_text(window)], exc, caplog)


def test_threaded_provider_validation_error_is_shielded(
    wired_window: MainWindow,
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_server: LocalErrorServer,
) -> None:
    window = wired_window
    shown = capture_message_dialogs(monkeypatch)
    exc = _real_error(error_server, "alpaca_api_401")
    _available_alpaca(window)
    monkeypatch.setattr(
        controller_module,
        "create_provider",
        lambda connection, store: _FailingProvider(exc),
    )
    _capture_logs(caplog)

    window._request_provider_activation(ALPACA_PAPER_CONNECTION_ID)
    qtbot.waitUntil(lambda: bool(shown), timeout=3000)

    assert [(item.title, item.message) for item in shown] == [
        (
            "Connection Failed",
            "Could not activate Alpaca Paper.\n\n"
            "Alpaca Paper rejected the credentials.",
        ),
    ]
    _assert_shielded([shown[0].all_text, widget_text(window)], exc, caplog)
