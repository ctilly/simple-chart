from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtWidgets import QDialog

import app.controller as controller_module
from app.controller import MainWindow, _build_startup_routes
from data.cache import Cache
from data.provider import ProviderAvailability
from data.provider.config import (
    ALPACA_PAPER_CONNECTION_ID,
    YFINANCE_CONNECTION_ID,
)
from data.provider.credentials import (
    CredentialStore,
    CredentialStoreAccess,
    ProviderCredentials,
    UnavailableCredentialStore,
)


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


def test_main_window_preflights_once_and_passes_result_to_settings(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason = "The operating-system credential store is unavailable."
    access = CredentialStoreAccess(
        UnavailableCredentialStore(reason),
        False,
        reason,
    )
    preflight_calls = 0
    settings_arguments: dict[str, object] = {}

    def initialize_store() -> CredentialStoreAccess:
        nonlocal preflight_calls
        preflight_calls += 1
        return access

    def skip_load(window: MainWindow) -> None:
        pass

    def skip_snapshots(window: MainWindow) -> None:
        pass

    class SettingsDialog:
        def __init__(
            self,
            cache: Cache,
            credential_store: CredentialStore,
            availability: Mapping[str, ProviderAvailability],
            parent: object | None = None,
            active_connection_id: str | None = None,
        ) -> None:
            settings_arguments["credential_store"] = credential_store
            settings_arguments["availability"] = availability
            settings_arguments["active_connection_id"] = active_connection_id

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(
        controller_module,
        "initialize_keyring_credential_store",
        initialize_store,
    )
    monkeypatch.setattr(MainWindow, "_load", skip_load)
    monkeypatch.setattr(
        MainWindow,
        "_refresh_watchlist_snapshots",
        skip_snapshots,
    )
    monkeypatch.setattr(
        controller_module,
        "ApplicationSettingsDialog",
        SettingsDialog,
    )

    window = MainWindow(str(tmp_path / "test.db"))
    qtbot.addWidget(window)
    window._on_application_settings()

    assert preflight_calls == 1
    assert not window._provider_availability["alpaca"].available
    assert settings_arguments["credential_store"] is access.store
    assert settings_arguments["availability"] is window._provider_availability
    assert settings_arguments["active_connection_id"] == YFINANCE_CONNECTION_ID


def test_startup_uses_yahoo_when_saved_alpaca_dependency_is_unavailable(
    tmp_path: Path,
) -> None:
    availability = {
        "yfinance": ProviderAvailability(True, None),
        "alpaca": ProviderAvailability(
            False,
            "Required package 'alpaca-py' is not installed.",
        ),
    }
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.set_active_provider_connection_id(ALPACA_PAPER_CONNECTION_ID)

        routes = _build_startup_routes(
            cache,
            ALPACA_PAPER_CONNECTION_ID,
            _MemoryCredentialStore(),
            availability,
        )

        assert routes.selected.connection.connection_id == YFINANCE_CONNECTION_ID
        assert routes.yahoo.connection.connection_id == YFINANCE_CONNECTION_ID
        assert routes.fallback_reason is not None
        assert "alpaca-py" in routes.fallback_reason
        assert (
            cache.get_active_provider_connection_id()
            == ALPACA_PAPER_CONNECTION_ID
        )


def test_startup_uses_yahoo_when_saved_alpaca_credentials_are_missing(
    tmp_path: Path,
) -> None:
    availability = {
        "yfinance": ProviderAvailability(True, None),
        "alpaca": ProviderAvailability(True, None),
    }
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.set_active_provider_connection_id(ALPACA_PAPER_CONNECTION_ID)

        routes = _build_startup_routes(
            cache,
            ALPACA_PAPER_CONNECTION_ID,
            _MemoryCredentialStore(),
            availability,
        )

        assert routes.selected.connection.connection_id == YFINANCE_CONNECTION_ID
        assert routes.fallback_reason is not None
        assert "credentials are not configured" in routes.fallback_reason
        assert (
            cache.get_active_provider_connection_id()
            == ALPACA_PAPER_CONNECTION_ID
        )
