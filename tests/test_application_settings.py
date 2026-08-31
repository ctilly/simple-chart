from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QToolButton,
    QWidget,
)

import app.application_settings as application_settings_module
from app.application_settings import (
    AlpacaConnectionDialog,
    ApplicationSettingsDialog,
)
from data.cache import Cache
from data.provider import ProviderAvailability
from data.provider.config import (
    ALPACA_PAPER_CONNECTION_ID,
    MarketDataFeed,
    YFINANCE_CONNECTION_ID,
)
from data.provider.credentials import ProviderCredentials


_AVAILABLE_PROVIDERS = {
    "yfinance": ProviderAvailability(True, None),
    "alpaca": ProviderAvailability(True, None),
}


class _MemoryCredentialStore:
    def __init__(self) -> None:
        self.credentials: dict[str, ProviderCredentials] = {}

    def get(self, connection_id: str) -> ProviderCredentials | None:
        return self.credentials.get(connection_id)

    def put(
        self,
        connection_id: str,
        credentials: ProviderCredentials,
    ) -> None:
        self.credentials[connection_id] = credentials

    def delete(self, connection_id: str) -> None:
        self.credentials.pop(connection_id, None)


def test_application_settings_has_fixed_connections_and_ui_tabs(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        dialog = ApplicationSettingsDialog(
            cache,
            _MemoryCredentialStore(),
            _AVAILABLE_PROVIDERS,
        )
        qtbot.addWidget(dialog)

        tabs = dialog.findChild(QTabWidget, "applicationSettingsTabs")
        table = dialog.findChild(QTableWidget, "providerConnections")
        dark_mode = dialog.findChild(QCheckBox, "darkMode")
        title_bar = dialog.findChild(QFrame, "dialogTitleBar")

        assert tabs is not None
        assert [tabs.tabText(index) for index in range(tabs.count())] == [
            "Connections",
            "UI",
            "Data Quality",
        ]
        assert table is not None
        assert table.rowCount() == 3
        connection_names: list[str] = []
        for row in range(3):
            item = table.item(row, 0)
            assert item is not None
            connection_names.append(item.text())
        assert connection_names == [
            "Yahoo Finance",
            "Alpaca Paper",
            "Alpaca Live",
        ]
        assert dark_mode is not None
        assert not dark_mode.isEnabled()
        assert dialog.objectName() == "applicationSettingsDialog"
        assert "background:" in dialog.styleSheet()
        assert "border: 2px solid" in dialog.styleSheet()
        assert title_bar is not None
        assert "background:" in title_bar.styleSheet()
        assert "border-bottom: 1px solid" in title_bar.styleSheet()


def test_application_settings_returns_configured_source_without_persisting_it(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    store = _MemoryCredentialStore()
    store.put(
        ALPACA_PAPER_CONNECTION_ID,
        ProviderCredentials("paper-key", "paper-secret"),
    )
    with Cache(str(tmp_path / "test.db")) as cache:
        dialog = ApplicationSettingsDialog(cache, store, _AVAILABLE_PROVIDERS)
        qtbot.addWidget(dialog)
        active = dialog.findChild(QComboBox, "activeDataSource")
        assert active is not None
        active.setCurrentIndex(active.findData(ALPACA_PAPER_CONNECTION_ID))

        dialog.accept()

        assert dialog.selected_connection_id() == ALPACA_PAPER_CONNECTION_ID
        assert cache.get_active_provider_connection_id() == YFINANCE_CONNECTION_ID


def test_application_settings_disables_unavailable_credential_provider(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason = "Required package 'alpaca-py' is not installed."
    explanations: list[tuple[str, str]] = []

    def capture_explanation(
        parent: QWidget | None,
        title: str,
        message: str,
    ) -> None:
        explanations.append((title, message))

    monkeypatch.setattr(
        application_settings_module,
        "show_information",
        capture_explanation,
    )
    availability = {
        "yfinance": ProviderAvailability(True, None),
        "alpaca": ProviderAvailability(False, reason),
    }
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.set_active_provider_connection_id(ALPACA_PAPER_CONNECTION_ID)
        dialog = ApplicationSettingsDialog(
            cache,
            _MemoryCredentialStore(),
            availability,
        )
        qtbot.addWidget(dialog)

        active = dialog.findChild(QComboBox, "activeDataSource")
        table = dialog.findChild(QTableWidget, "providerConnections")
        configure = dialog.findChild(
            QPushButton,
            f"configure_{ALPACA_PAPER_CONNECTION_ID}",
        )
        info = dialog.findChild(
            QToolButton,
            f"unavailable_{ALPACA_PAPER_CONNECTION_ID}",
        )

        assert active is not None
        assert active.currentData() == YFINANCE_CONNECTION_ID
        model = active.model()
        assert model is not None
        paper_index = active.findData(ALPACA_PAPER_CONNECTION_ID)
        assert not (
            model.flags(model.index(paper_index, 0))
            & Qt.ItemFlag.ItemIsEnabled
        )
        assert active.itemData(paper_index, Qt.ItemDataRole.ToolTipRole) == reason
        assert table is not None
        credential_status = table.item(1, 3)
        assert credential_status is not None
        assert credential_status.text() == "Unavailable"
        assert credential_status.toolTip() == reason
        assert configure is not None
        assert not configure.isEnabled()
        assert configure.toolTip() == reason
        assert info is not None
        assert info.toolTip() == reason
        info.click()
        assert explanations == [
            ("Provider Unavailable", f"Alpaca Paper\n\n{reason}")
        ]


def test_application_settings_rejects_programmatic_unavailable_selection(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    availability = {
        "yfinance": ProviderAvailability(True, None),
        "alpaca": ProviderAvailability(False, "Secure storage unavailable."),
    }
    with Cache(str(tmp_path / "test.db")) as cache:
        dialog = ApplicationSettingsDialog(
            cache,
            _MemoryCredentialStore(),
            availability,
        )
        qtbot.addWidget(dialog)
        active = dialog.findChild(QComboBox, "activeDataSource")
        assert active is not None
        active.setCurrentIndex(active.findData(ALPACA_PAPER_CONNECTION_ID))

        dialog.accept()

        assert dialog.result() != QDialog.DialogCode.Accepted
        with pytest.raises(RuntimeError, match="not accepted"):
            dialog.selected_connection_id()


def test_alpaca_connection_dialog_masks_and_saves_credentials(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    store = _MemoryCredentialStore()
    with Cache(str(tmp_path / "test.db")) as cache:
        connection = cache.get_provider_connection(ALPACA_PAPER_CONNECTION_ID)
        assert connection is not None
        dialog = AlpacaConnectionDialog(connection, cache, store)
        qtbot.addWidget(dialog)
        title_bar = dialog.findChild(QFrame, "dialogTitleBar")
        key_id = dialog.findChild(QLineEdit, "alpacaApiKeyId")
        secret = dialog.findChild(QLineEdit, "alpacaApiSecret")
        feed = dialog.findChild(QComboBox, "alpacaFeed")
        assert key_id is not None
        assert secret is not None
        assert feed is not None
        assert dialog.objectName() == "alpacaConnectionDialog"
        assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
        assert "background:" in dialog.styleSheet()
        assert "border: 2px solid" in dialog.styleSheet()
        assert title_bar is not None
        assert "background:" in title_bar.styleSheet()
        assert "border-bottom: 1px solid" in title_bar.styleSheet()
        assert [feed.itemText(index) for index in range(feed.count())] == [
            "IEX (real-time)",
            "SIP (15-minute delayed)",
            "SIP (real-time, subscription required)",
        ]
        assert key_id.echoMode() == QLineEdit.EchoMode.Password
        assert secret.echoMode() == QLineEdit.EchoMode.Password

        key_id.setText("paper-key")
        secret.setText("paper-secret")
        feed.setCurrentIndex(feed.findData(MarketDataFeed.DELAYED_SIP))
        dialog.accept()

        assert store.get(ALPACA_PAPER_CONNECTION_ID) == ProviderCredentials(
            "paper-key",
            "paper-secret",
        )
        updated = cache.get_provider_connection(ALPACA_PAPER_CONNECTION_ID)
        assert updated is not None
        assert updated.feed == MarketDataFeed.DELAYED_SIP


def test_alpaca_connection_dialog_keeps_stored_credentials_when_blank(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    stored = ProviderCredentials("paper-key", "paper-secret")
    store = _MemoryCredentialStore()
    store.put(ALPACA_PAPER_CONNECTION_ID, stored)
    with Cache(str(tmp_path / "test.db")) as cache:
        connection = cache.get_provider_connection(ALPACA_PAPER_CONNECTION_ID)
        assert connection is not None
        dialog = AlpacaConnectionDialog(connection, cache, store)
        qtbot.addWidget(dialog)
        key_id = dialog.findChild(QLineEdit, "alpacaApiKeyId")
        secret = dialog.findChild(QLineEdit, "alpacaApiSecret")
        assert key_id is not None
        assert secret is not None
        assert key_id.text() == ""
        assert secret.text() == ""

        dialog.accept()

        assert store.get(ALPACA_PAPER_CONNECTION_ID) == stored
