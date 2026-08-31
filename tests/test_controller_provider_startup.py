from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtWidgets import QDialog

import app.controller as controller_module
from app.bar_comparison import BarComparisonService
from app.controller import MainWindow, _build_startup_routes
from data.cache import Cache
from data.models import Bar, BarCorrection, OHLCVSeries, Timeframe
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
            current_cache_namespace: str | None = None,
            current_symbol: str | None = None,
            current_timeframe: Timeframe | None = None,
            comparison_service: object | None = None,
        ) -> None:
            settings_arguments["credential_store"] = credential_store
            settings_arguments["availability"] = availability
            settings_arguments["active_connection_id"] = active_connection_id
            settings_arguments["current_cache_namespace"] = (
                current_cache_namespace
            )
            settings_arguments["current_symbol"] = current_symbol
            settings_arguments["current_timeframe"] = current_timeframe
            settings_arguments["comparison_service"] = comparison_service

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Rejected

        def bars_changed(self) -> bool:
            return False

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
    assert settings_arguments["current_cache_namespace"] == "yfinance"
    assert settings_arguments["current_symbol"] == "SPY"
    assert settings_arguments["current_timeframe"] == Timeframe.DAILY
    assert isinstance(
        settings_arguments["comparison_service"],
        BarComparisonService,
    )


def test_settings_bar_change_reloads_chart_when_dialog_is_cancelled(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reload_calls: list[tuple[bool, bool]] = []

    class SettingsDialog:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Rejected

        def bars_changed(self) -> bool:
            return True

    monkeypatch.setattr(MainWindow, "_load", lambda window: None)
    monkeypatch.setattr(
        MainWindow,
        "_refresh_watchlist_snapshots",
        lambda window: None,
    )
    monkeypatch.setattr(
        MainWindow,
        "_reload_extensions",
        lambda window, *, draw_bars=True, preserve_view=True: reload_calls.append(
            (draw_bars, preserve_view)
        ),
    )
    monkeypatch.setattr(
        controller_module,
        "ApplicationSettingsDialog",
        SettingsDialog,
    )

    window = MainWindow(str(tmp_path / "test.db"))
    qtbot.addWidget(window)
    window._on_application_settings()

    assert reload_calls == [(True, True)]


def test_extension_only_reload_reuses_existing_correction_notice(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice_calls: list[OHLCVSeries] = []
    timestamp = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)
    series = OHLCVSeries(
        "SPY",
        Timeframe.DAILY,
        [Bar(timestamp, 100.0, 110.0, 90.0, 100.0, 1_000)],
    )
    monkeypatch.setattr(MainWindow, "_load", lambda window: None)
    monkeypatch.setattr(
        MainWindow,
        "_refresh_watchlist_snapshots",
        lambda window: None,
    )
    monkeypatch.setattr(
        MainWindow,
        "_update_bar_correction_notice",
        lambda window, current: notice_calls.append(current),
    )

    window = MainWindow(str(tmp_path / "test.db"))
    qtbot.addWidget(window)
    window._current_series = series
    monkeypatch.setattr(
        window._extension_runtime,
        "render_all",
        lambda current: [],
    )
    monkeypatch.setattr(
        window,
        "_remove_stale_extension_renders",
        lambda render_passes: None,
    )
    monkeypatch.setattr(
        window._chart.plot_manager,
        "refresh",
        lambda *, preserve_view: None,
    )

    window._reload_extensions(draw_bars=False)

    assert notice_calls == []


def test_fetch_completion_surfaces_conflicted_bar_correction_notice(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MainWindow, "_load", lambda window: None)
    monkeypatch.setattr(
        MainWindow,
        "_refresh_watchlist_snapshots",
        lambda window: None,
    )
    monkeypatch.setattr(MainWindow, "_refresh_level1", lambda window: None)
    monkeypatch.setattr(MainWindow, "_render", lambda window, series: None)

    window = MainWindow(str(tmp_path / "test.db"))
    qtbot.addWidget(window)
    timestamp = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)
    namespace = window._selected_route.cache_namespace
    window._cache.put_bars(
        namespace,
        "SPY",
        Timeframe.DAILY,
        [Bar(timestamp, 100.0, 110.0, 90.0, 100.0, 1_000)],
    )
    window._cache.put_bar_correction(
        BarCorrection(
            namespace,
            "SPY",
            Timeframe.DAILY,
            timestamp,
            low=95.0,
        )
    )
    window._cache.put_bars(
        namespace,
        "SPY",
        Timeframe.DAILY,
        [Bar(timestamp, 80.0, 90.0, 70.0, 80.0, 1_000)],
    )
    bars = window._cache.get_bars(
        namespace,
        "SPY",
        Timeframe.DAILY,
        int(timestamp.timestamp() * 1000),
        int(timestamp.timestamp() * 1000),
    )

    window._on_fetch_done(OHLCVSeries("SPY", Timeframe.DAILY, bars))

    status_bar = window.statusBar()
    assert status_bar is not None
    assert status_bar.currentMessage() == (
        "1 bar correction needs review in Settings > Data Quality."
    )

    window._cache.delete_bar_correction(
        namespace,
        "SPY",
        Timeframe.DAILY,
        timestamp,
    )
    window._on_fetch_done(
        OHLCVSeries(
            "SPY",
            Timeframe.DAILY,
            window._cache.get_bars(
                namespace,
                "SPY",
                Timeframe.DAILY,
                int(timestamp.timestamp() * 1000),
                int(timestamp.timestamp() * 1000),
            ),
        )
    )

    assert status_bar.currentMessage() == ""


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
