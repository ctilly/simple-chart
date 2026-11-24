import json
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialogButtonBox, QLabel, QLineEdit, QPushButton,
    QTableWidget, QToolButton,
)
from pytestqt.qtbot import QtBot

from app import controller
from app.application_settings import ApplicationSettingsDialog, ConnectionDialog
from app.controller import MainWindow
from data.models import Bar, MarketSnapshot, SymbolMatch, Timeframe
from data.provider.credentials import (
    CredentialStoreAccess, ProviderCredentials, initialize_keyring_credential_store,
)
from data.provider.registry import ProviderRegistry, discover_providers
from data.session import MARKET_TIMEZONE
from fixtures.providers.fakesource.implementation import (
    BAR_CLOSE, FAILURE_SENTINEL, PROFILE_DESCRIPTION, TEST_TOKEN, FakeSource,
)
from providers.yahoo import implementation as yahoo
from realistic_errors import assert_secret_absent
from ui_text import ShownMessage, capture_message_dialogs


_CONNECTION_ID = "fakesource-work"
_CONNECTION_LABEL = "Fake Source Work"
_PROFILE_LABEL = "Fake Source"
_YAHOO_LABEL = "Yahoo Finance"
_YAHOO_HEADLINE = "News from the Yahoo fixture"


class MemoryKeyring:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, payload: str) -> None:
        self.entries[service, username] = payload

    def delete_password(self, service: str, username: str) -> None:
        del self.entries[service, username]


@dataclass
class ProviderWindow:
    window: MainWindow
    keyring: MemoryKeyring
    messages: list[ShownMessage]
    credentials: list[ProviderCredentials]
    yahoo_bar_calls: list[str]
    earnings_day: date


@pytest.fixture
def provider_window(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ProviderWindow]:
    registry = discover_providers("providers", "fixtures.providers")
    keyring = MemoryKeyring()
    credentials: list[ProviderCredentials] = []
    yahoo_bar_calls: list[str] = []
    earnings_day = datetime.now(MARKET_TIMEZONE).date() + timedelta(days=3)
    original_init = ProviderCredentials.__init__

    def record_credentials(
        instance: ProviderCredentials, values: Mapping[str, str],
    ) -> None:
        original_init(instance, values)
        credentials.append(instance)

    def initialize(*, registry: ProviderRegistry) -> CredentialStoreAccess:
        return initialize_keyring_credential_store(keyring, registry=registry)

    def fetch_bars(
        _provider: yahoo.YFinanceProvider, symbol: str, timeframe: Timeframe,
        start: datetime, end: datetime,
    ) -> list[Bar]:
        yahoo_bar_calls.append(symbol)
        return []

    def fetch_snapshots(
        _provider: yahoo.YFinanceProvider, symbols: list[str],
    ) -> dict[str, MarketSnapshot]:
        return {}

    class TickerFixture:
        def __init__(self, symbol: str) -> None:
            self.info = {"longName": f"{symbol} Yahoo fixture company"}

        def get_news(self, *, count: int) -> list[dict[str, object]]:
            return [{"content": {"title": _YAHOO_HEADLINE, "pubDate": "2026-01-05T10:00:00-05:00"}}]

        def get_calendar(self) -> dict[str, list[date]]:
            return {"Earnings Date": [earnings_day]}

    class SearchFixture:
        def __init__(self, query: str, **options: object) -> None:
            self.quotes = [{"symbol": query, "longname": "Yahoo fixture match", "exchange": "NYSE"}]

    monkeypatch.setattr(ProviderCredentials, "__init__", record_credentials)
    monkeypatch.setattr(controller, "initialize_keyring_credential_store", initialize)
    monkeypatch.setattr(yahoo.YFinanceProvider, "fetch_bars", fetch_bars)
    monkeypatch.setattr(yahoo.YFinanceProvider, "fetch_snapshots", fetch_snapshots)
    monkeypatch.setattr(yahoo.yf, "Ticker", TickerFixture)
    monkeypatch.setattr(yahoo.yf, "Search", SearchFixture)
    messages = capture_message_dialogs(monkeypatch)
    window = MainWindow(str(tmp_path / "chart.db"), provider_registry=registry)
    try:
        qtbot.waitUntil(lambda: window._pending_chart_load is None and not window._data_workers_busy())
        messages.clear()
        yahoo_bar_calls.clear()
        yield ProviderWindow(window, keyring, messages, credentials, yahoo_bar_calls, earnings_day)
    finally:
        window.close()
        qtbot.waitUntil(lambda: window._close_finalized, timeout=5000)
        window.deleteLater()


def _save_and_activate(window: MainWindow, token: str, qtbot: QtBot) -> None:
    def configure() -> None:
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, ConnectionDialog)
        try:
            fields = dialog.findChildren(QLineEdit)
            assert len(fields) == 1
            assert fields[0].objectName() == "credential_token"
            assert fields[0].echoMode() == QLineEdit.EchoMode.Password
            tiers = dialog.findChildren(QComboBox)
            assert len(tiers) == 1
            assert [tiers[0].itemText(index) for index in range(tiers[0].count())] == ["Basic", "Pro"]
            tiers[0].setCurrentIndex(tiers[0].findData("pro"))
            fields[0].setText(token)
            buttons = dialog.findChild(QDialogButtonBox)
            assert buttons is not None
            buttons.button(QDialogButtonBox.StandardButton.Save).click()
            assert fields[0].text() == ""
        finally:
            if dialog.isVisible():
                dialog.reject()

    def settings() -> None:
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, ApplicationSettingsDialog)
        try:
            table = dialog.findChild(QTableWidget, "providerConnections")
            assert table is not None
            rows = table.findItems(_CONNECTION_LABEL, Qt.MatchFlag.MatchExactly)
            assert len(rows) == 1
            row = rows[0].row()
            assert table.item(row, 3).text() == "Not configured"
            configure_button = dialog.findChild(QPushButton, f"configure_{_CONNECTION_ID}")
            assert configure_button is not None and configure_button.isEnabled()
            QTimer.singleShot(0, configure)
            configure_button.click()
            assert table.item(row, 2).text() == "Pro"
            assert table.item(row, 3).text() == "Stored"

            index = dialog._active_source.findData(_CONNECTION_ID)
            assert index >= 0
            dialog._active_source.setCurrentIndex(index)
            dialog._active_source.activated.emit(index)
            buttons = dialog.findChild(QDialogButtonBox)
            assert buttons is not None
            buttons.button(QDialogButtonBox.StandardButton.Ok).click()
        finally:
            if dialog.isVisible():
                dialog.reject()

    settings_button = window.findChild(QToolButton, "applicationSettingsButton")
    assert settings_button is not None
    QTimer.singleShot(0, settings)
    settings_button.click()
    qtbot.waitUntil(lambda: window._validation_thread is None and window._pending_route is None)


def _assert_no_credential_output(
    scenario: ProviderWindow, caplog: pytest.LogCaptureFixture, secret: str,
) -> None:
    database = Path(scenario.window._db_path)
    wal = database.with_name(database.name + "-wal")
    assert wal.is_file()
    for path in (database, wal, database.with_name(database.name + "-journal")):
        if path.exists():
            assert_secret_absent(path.read_bytes().decode("latin1"), secret)
    assert_secret_absent(caplog.text, secret)
    assert scenario.credentials
    for credentials in scenario.credentials:
        assert_secret_absent(repr(credentials), secret)
        assert_secret_absent(str(credentials), secret)
    for message in scenario.messages:
        assert_secret_absent(message.all_text, secret)


def test_discovered_provider_works_through_mainwindow_settings(
    provider_window: ProviderWindow, qtbot: QtBot,
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    window = provider_window.window
    checked: list[FakeSource] = []
    check_connection = FakeSource.check_connection

    def record_check(provider: FakeSource) -> None:
        check_connection(provider)
        checked.append(provider)

    monkeypatch.setattr(FakeSource, "check_connection", record_check)
    assert not provider_window.keyring.entries
    _save_and_activate(window, TEST_TOKEN, qtbot)
    assert window._cache.get_active_provider_connection_id() == _CONNECTION_ID
    assert len(checked) == 1
    assert checked[0].tier == "pro"
    entries = provider_window.keyring.entries
    assert list(entries) == [("simplechart.provider-credentials", _CONNECTION_ID)]
    payload = next(iter(entries.values()))
    if json.loads(payload) != {"token": TEST_TOKEN}:
        pytest.fail("The in-memory keyring did not receive the submitted credential.")

    window._symbol_bar.symbol_changed.emit("ACME")
    qtbot.waitUntil(lambda: window._pending_chart_load is None and not window._data_workers_busy(), timeout=5000)
    series = window._current_series
    assert series is not None and series.symbol == "ACME" and series.bars
    assert all(bar.close == BAR_CLOSE for bar in series.bars)
    source = window.findChild(QLabel, "dataSourceStatusLabel")
    assert source is not None and source.text() == f"{_CONNECTION_LABEL} / Pro"
    assert "Last data request succeeded" in source.toolTip()
    assert provider_window.yahoo_bar_calls == []

    window._app_header.details_requested.emit()
    dialog = window._symbol_details.dialog
    assert dialog is not None
    qtbot.waitUntil(lambda: not window._symbol_details.busy)
    assert PROFILE_DESCRIPTION in dialog._profile.toPlainText()
    assert f"Source: {_PROFILE_LABEL}" in dialog._profile.toPlainText()
    assert _YAHOO_HEADLINE in dialog._news.toPlainText()
    assert _YAHOO_LABEL in dialog._tabs.tabToolTip(dialog._tabs.indexOf(dialog._news))
    with qtbot.waitSignal(window._symbol_bar.search.results, timeout=5000) as result:
        window._symbol_bar.search.edit("ACME")
    assert result.args == [(SymbolMatch("ACME", "Yahoo fixture match", "NYSE"),)]
    quote = window._app_header._quote_label
    assert f"Next earnings source: {_YAHOO_LABEL}." in quote.toolTip()
    assert provider_window.earnings_day.strftime("%b %d").replace(" 0", " ") in quote.text()
    assert provider_window.messages == []
    _assert_no_credential_output(provider_window, caplog, TEST_TOKEN)


def test_provider_probe_hides_an_unlabelled_secret(
    provider_window: ProviderWindow, qtbot: QtBot, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    window = provider_window.window
    _save_and_activate(window, FAILURE_SENTINEL, qtbot)
    assert window._cache.get_active_provider_connection_id() == window._provider_registry.fallback_connection_id
    _assert_no_credential_output(provider_window, caplog, FAILURE_SENTINEL)
    assert [(message.title, message.message) for message in provider_window.messages] == [
        ("Connection Failed", "Could not activate Fake Source Work.\n\n"
         "Testing Fake Source Work failed. Details were logged."),
    ]
