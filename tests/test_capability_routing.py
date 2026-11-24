from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Event
from types import ModuleType

import pytest
from pytestqt.qtbot import QtBot
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

import app.controller as controller
from app.application_settings import ApplicationSettingsDialog
from app.controller import MainWindow
from data.models import (
    Bar, Level1Quote, MarketSnapshot, NewsArticle, SymbolMatch, SymbolProfile, Timeframe,
)
from data.provider.base import DataProvider
from data.provider.config import ProviderConnection
from data.provider.credentials import ProviderCredentials
from data.provider.registry import discover_providers
from data.provider.spec import Capability, ConnectionTemplate, NamespaceSpec, ProviderSpec
from providers.alpaca import PROVIDER as ALPACA
from providers.yahoo import PROVIDER as YAHOO


class RecordingProvider(DataProvider):
    def __init__(self, name: str, month: int) -> None:
        self.name = name
        self.day = date(2026, month, 17)
        self.calls: list[tuple[Capability, str]] = []
        self.checked = Event()
        self.started: dict[Capability, Event] = {}
        self.release = Event()
        self.release.set()

    def record(self, capability: Capability, symbol: str) -> None:
        self.calls.append((capability, symbol))
        if capability in self.started:
            self.started[capability].set()
            assert self.release.wait(5)

    def check_connection(self) -> None:
        self.checked.set()

    def fetch_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime,
    ) -> list[Bar]:
        self.record(Capability.BARS, symbol)
        return []

    def native_timeframes(self) -> list[Timeframe]:
        return [Timeframe.DAILY]

    def fetch_level1(self, symbol: str) -> Level1Quote:
        self.record(Capability.LEVEL1, symbol)
        return Level1Quote(symbol, None, 100, None, None, None, None, None, None, None, None,
                           None, None, None)

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        for symbol in symbols:
            self.record(Capability.SNAPSHOTS, symbol)
        return {}

    def fetch_company_name(self, symbol: str) -> str:
        self.record(Capability.COMPANY_NAME, symbol)
        return f"{self.name} Legal Name"

    def fetch_profile(self, symbol: str) -> SymbolProfile:
        self.record(Capability.PROFILE, symbol)
        return SymbolProfile(name=f"{self.name} Profile Name", description=f"{self.name} description")

    def fetch_news(self, symbol: str) -> tuple[NewsArticle, ...]:
        self.record(Capability.NEWS, symbol)
        return (
            NewsArticle(f"{self.name} headline", "Wire", datetime(2026, 1, 5, 15, tzinfo=UTC), None),
        )

    def search_symbols(self, query: str) -> tuple[SymbolMatch, ...]:
        self.record(Capability.SEARCH, query)
        return (SymbolMatch(query, self.name, "Exchange"),)

    def fetch_upcoming_earnings(self, symbol: str) -> tuple[date, date]:
        self.record(Capability.UPCOMING_EARNINGS, symbol)
        return self.day, self.day

    def fetch_past_earnings(self, symbol: str) -> tuple[date, ...]:
        self.record(Capability.PAST_EARNINGS, symbol)
        return (self.day,)

    def fetch_dividend_dates(self, symbol: str) -> tuple[date, ...]:
        self.record(Capability.DIVIDENDS, symbol)
        return (self.day,)


def _module(spec: ProviderSpec, provider: RecordingProvider) -> ModuleType:
    def factory(
        connection: ProviderConnection, credentials: ProviderCredentials | None,
    ) -> DataProvider:
        return provider

    module = ModuleType(spec.name)
    module.PROVIDER = replace(spec, factory=factory)
    return module


def _spec(name: str, display_name: str, capabilities: frozenset[Capability]) -> ProviderSpec:
    return ProviderSpec(
        name, display_name, (ConnectionTemplate(name, f"{display_name} connection"),),
        capabilities, (NamespaceSpec(name, display_name, (), True),),
        YAHOO.factory,
    )


_REFERENCE_CAPABILITIES = frozenset({
    Capability.COMPANY_NAME, Capability.PROFILE, Capability.NEWS,
    Capability.UPCOMING_EARNINGS, Capability.PAST_EARNINGS, Capability.DIVIDENDS,
})


@pytest.fixture
def routing_window(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_credentials: None,
) -> Iterator[tuple[MainWindow, dict[str, RecordingProvider]]]:
    providers = {
        "atlas": RecordingProvider("Atlas", 2),
        "beacon": RecordingProvider("Beacon", 3),
        "profileonly": RecordingProvider("Profile Only", 4),
        "alpaca": RecordingProvider("Alpaca", 5),
        YAHOO.name: RecordingProvider("Yahoo", 6),
    }
    registry = discover_providers((
        _module(YAHOO, providers[YAHOO.name]),
        _module(replace(ALPACA, credential_fields=(), required_modules=()), providers["alpaca"]),
        _module(_spec("atlas", "Atlas Data", _REFERENCE_CAPABILITIES), providers["atlas"]),
        _module(_spec("beacon", "Beacon Data", _REFERENCE_CAPABILITIES), providers["beacon"]),
        _module(_spec("profileonly", "Profile Only Data", frozenset({Capability.PROFILE})),
                providers["profileonly"]),
    ))
    db_path = str(tmp_path / "routing.db")
    with controller.Cache(db_path, registry=registry) as cache:
        for symbol in cache.get_watchlist():
            cache.remove_from_watchlist(symbol)
        cache.add_to_watchlist("ACME")
        cache.set_chart_event_visibility(True, True)
    with monkeypatch.context() as startup:
        startup.setattr(MainWindow, "_load", lambda window: None)
        startup.setattr(MainWindow, "_refresh_watchlist_snapshots", lambda window: None)
        window = MainWindow(db_path, provider_registry=registry)
    monkeypatch.setattr(window, "_on_fetch_error", lambda message: None)
    monkeypatch.setattr(window, "_render", lambda series, history_complete: None)
    monkeypatch.setattr(controller, "show_warning", lambda parent, title, message: None)
    window._snapshot_timer.stop()
    window._state.symbol = "ACME"
    try:
        yield window, providers
    finally:
        for provider in providers.values():
            provider.release.set()
        window.close()
        qtbot.waitUntil(lambda: window._close_finalized, timeout=5000)
        window.deleteLater()


def _settings_activate(window: MainWindow, connection_id: str, qtbot: QtBot) -> None:
    def select_connection() -> None:
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, ApplicationSettingsDialog)
        index = dialog._active_source.findData(connection_id)
        assert index >= 0
        dialog._active_source.setCurrentIndex(index)
        dialog._active_source.activated.emit(index)
        dialog.accept()

    QTimer.singleShot(0, select_connection)
    window._on_application_settings()
    qtbot.waitUntil(lambda: window._validation_thread is None, timeout=5000)


def _activate(window: MainWindow, connection_id: str, qtbot: QtBot) -> None:
    window._request_provider_activation(connection_id)
    qtbot.waitUntil(lambda: window._validation_thread is None, timeout=5000)


def test_settings_activates_with_provider_owned_probe(
    routing_window: tuple[MainWindow, dict[str, RecordingProvider]],
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, providers = routing_window
    warnings: list[str] = []
    monkeypatch.setattr(controller, "show_warning", lambda parent, title, message: warnings.append(message) if title == "Connection Failed" else None)
    _settings_activate(window, "atlas", qtbot)
    atlas = providers["atlas"]
    assert atlas.checked.is_set()
    assert window._cache.get_active_provider_connection_id() == "atlas"
    assert warnings == []
    assert (Capability.LEVEL1, "SPY") not in atlas.calls


def test_capabilities_route_each_production_request_and_attribute_details(
    routing_window: tuple[MainWindow, dict[str, RecordingProvider]], qtbot: QtBot,
) -> None:
    window, providers = routing_window
    atlas = providers["atlas"]
    fallback = providers[YAHOO.name]
    _activate(window, "atlas", qtbot)
    qtbot.waitUntil(lambda: window._pending_chart_load is None and not window._chart_load_tasks)
    window._open_symbol_details()
    search_results: list[tuple[SymbolMatch, ...]] = []
    window._symbol_bar.search.results.connect(search_results.append)
    window._symbol_bar.search.edit("ACME")
    qtbot.waitUntil(lambda: bool(search_results), timeout=5000)
    qtbot.waitUntil(lambda: not window._symbol_details.busy and not window._earnings.busy)
    qtbot.waitUntil(lambda: window._level1_thread is None and not window._chart_load_tasks)
    dialog = window._symbol_details.dialog
    assert dialog is not None
    assert set(atlas.calls) == {(capability, "ACME") for capability in _REFERENCE_CAPABILITIES}
    assert {capability for capability, _ in fallback.calls} == {
        Capability.BARS, Capability.SNAPSHOTS, Capability.LEVEL1, Capability.SEARCH,
    }
    assert "Atlas Data" in dialog._profile.toPlainText()
    assert "Atlas Data" in dialog._tabs.tabToolTip(dialog._tabs.indexOf(dialog._news))
    assert "Wire · Jan 05, 2026 · 10:00 ET" in dialog._news.toPlainText()
    assert "Next earnings source: Atlas Data." in window._app_header._quote_label.toolTip()
    assert "ACME · Atlas Legal Name" in dialog.windowTitle()
    assert search_results[-1] == (SymbolMatch("ACME", "Yahoo", "Exchange"),)

    _activate(window, "profileonly", qtbot)
    qtbot.waitUntil(lambda: window._pending_chart_load is None and not window._data_workers_busy())
    assert "Profile Only Data" in dialog._profile.toPlainText()
    assert "Yahoo Finance" in dialog._tabs.tabToolTip(dialog._tabs.indexOf(dialog._news))
    assert (Capability.NEWS, "ACME") not in providers["profileonly"].calls


def test_alpaca_futures_use_fallback_for_bars_snapshots_and_level1(
    routing_window: tuple[MainWindow, dict[str, RecordingProvider]], qtbot: QtBot,
) -> None:
    window, providers = routing_window
    _activate(window, "alpaca-paper", qtbot)
    market = {Capability.BARS, Capability.SNAPSHOTS, Capability.LEVEL1}
    qtbot.waitUntil(lambda: market <= {
        capability for capability, symbol in providers["alpaca"].calls if symbol == "ACME"
    }, timeout=5000)
    window._on_watchlist_add("ES=F")
    window._on_symbol_changed("ES=F")
    qtbot.waitUntil(lambda: market <= {
        capability for capability, symbol in providers[YAHOO.name].calls if symbol == "ES=F"
    }, timeout=5000)
    assert all(symbol != "ES=F" for _, symbol in providers["alpaca"].calls)
    assert market <= {capability for capability, symbol in providers["alpaca"].calls if symbol == "ACME"}
    assert not market.intersection({
        capability for capability, symbol in providers[YAHOO.name].calls if symbol == "ACME"
    })


def test_same_symbol_switch_discards_in_flight_details_earnings_and_name(
    routing_window: tuple[MainWindow, dict[str, RecordingProvider]], qtbot: QtBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, providers = routing_window
    atlas = providers["atlas"]
    beacon = providers["beacon"]
    fallback = providers[YAHOO.name]
    blocked_capabilities = (
        Capability.PROFILE, Capability.NEWS, Capability.UPCOMING_EARNINGS,
        Capability.PAST_EARNINGS, Capability.COMPANY_NAME,
    )
    for provider in (atlas, fallback):
        provider.started = {capability: Event() for capability in blocked_capabilities}
        provider.release.clear()
    _activate(window, "atlas", qtbot)
    window._open_symbol_details()
    dialog = window._symbol_details.dialog
    assert dialog is not None
    qtbot.waitUntil(lambda: all(
        atlas.started[capability].is_set() or fallback.started[capability].is_set()
        for capability in blocked_capabilities
    ))
    displayed_names: list[str | None] = []
    displayed_earnings: list[str] = []
    displayed_profiles: list[str] = []
    displayed_news: list[str] = []
    displayed_history: list[tuple[tuple[date, ...], tuple[date, ...]]] = []
    window._earnings.history_changed.connect(lambda: displayed_history.append(window._earnings.history))
    original_set_quote = window._app_header.set_quote
    original_set_earnings = window._app_header.set_earnings_date

    def set_quote(quote: Level1Quote) -> None:
        displayed_names.append(quote.company_name)
        original_set_quote(quote)

    def set_earnings(label: str, *, highlight: bool = False) -> None:
        displayed_earnings.append(label)
        original_set_earnings(label, highlight=highlight)

    monkeypatch.setattr(window._app_header, "set_quote", set_quote)
    monkeypatch.setattr(window._app_header, "set_earnings_date", set_earnings)
    dialog._profile.textChanged.connect(lambda: displayed_profiles.append(dialog._profile.toPlainText()))
    dialog._news.textChanged.connect(lambda: displayed_news.append(dialog._news.toPlainText()))
    _activate(window, "beacon", qtbot)
    atlas.release.set()
    fallback.release.set()
    qtbot.waitUntil(
        lambda: not window._symbol_details.busy and not window._earnings.busy
        and window._level1_thread is None,
        timeout=5000,
    )
    assert displayed_names and set(displayed_names) == {"Beacon Legal Name"}
    assert displayed_earnings[-1] == "Mar 17"
    assert "Feb 17" not in displayed_earnings
    assert all("Atlas description" not in result for result in displayed_profiles)
    assert all("Atlas headline" not in result for result in displayed_news)
    assert "Beacon description" in dialog._profile.toPlainText()
    assert "Beacon headline" in dialog._news.toPlainText()
    assert window._earnings.history == ((beacon.day,), (beacon.day,))
    assert all(atlas.day not in earnings + dividends for earnings, dividends in displayed_history)
    assert (Capability.COMPANY_NAME, "ACME") in beacon.calls
    assert "ACME · Beacon Legal Name" in dialog.windowTitle()


def test_company_name_retries_when_switching_back_to_a_cancelled_connection(
    routing_window: tuple[MainWindow, dict[str, RecordingProvider]], qtbot: QtBot,
) -> None:
    window, providers = routing_window
    atlas = providers["atlas"]
    started = Event()
    atlas.started = {Capability.COMPANY_NAME: started}
    atlas.release.clear()
    _activate(window, "atlas", qtbot)
    qtbot.waitUntil(started.is_set)
    _activate(window, "beacon", qtbot)
    _activate(window, "atlas", qtbot)
    atlas.release.set()
    qtbot.waitUntil(lambda: window._level1_thread is None)
    assert "Atlas Legal Name" in window._app_header._quote_label.text()
    assert atlas.calls.count((Capability.COMPANY_NAME, "ACME")) == 2


def test_data_provider_can_declare_reference_only_capabilities() -> None:
    class ReferenceProvider(DataProvider):
        def fetch_company_name(self, symbol: str) -> str:
            return symbol

    provider = ReferenceProvider()
    from data.provider.base import UnsupportedCapabilityError

    now = datetime.now(UTC)
    for call in (
        lambda: provider.fetch_bars("ACME", Timeframe.DAILY, now, now),
        lambda: provider.fetch_snapshots(["ACME"]),
        lambda: provider.fetch_level1("ACME"),
        provider.native_timeframes,
        provider.check_connection,
    ):
        with pytest.raises(UnsupportedCapabilityError):
            call()
