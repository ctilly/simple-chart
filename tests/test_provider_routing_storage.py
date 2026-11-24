import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot
from PyQt6.QtWidgets import QLineEdit

import app.application_settings as settings
from app.application_settings import ConnectionDialog
from data.cache import Cache
from data.provider.credentials import CredentialStoreUnavailableError, ProviderCredentials
from data.provider.registry import ProviderRegistry
from data.provider.spec import CredentialField
from providers.yahoo.implementation import YFinanceProvider


def test_asset_reference_migration_and_source_miss(
    provider_registry: ProviderRegistry, tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "old.db")
    now = datetime(2026, 1, 5, 15, tzinfo=UTC)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE asset_reference "
            "(symbol TEXT PRIMARY KEY, company_name TEXT NOT NULL, refreshed_at INTEGER NOT NULL)"
        )
        conn.execute("INSERT INTO asset_reference VALUES (?, ?, ?)",
                     ("ACME", "Legacy Name", int(now.timestamp() * 1000)))
    fallback = provider_registry.build_connection(provider_registry.fallback_connection_id).provider_name
    for _ in range(2):
        with Cache(db_path, registry=provider_registry) as cache:
            legacy = cache.get_asset_reference("ACME", source=fallback)
            assert legacy is not None
            assert legacy.company_name == "Legacy Name"
            assert cache.get_asset_reference("ACME", source="atlas") is None
    with Cache(db_path, registry=provider_registry) as cache:
        cache.put_asset_reference("ACME", "Atlas Name", now, source="atlas")
        assert cache.get_asset_reference("ACME", source=fallback) is None
        current = cache.get_asset_reference("ACME", source="atlas")
        assert current is not None
        assert current.company_name == "Atlas Name"


def test_removed_stored_option_uses_declared_default(
    provider_registry: ProviderRegistry, tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "options.db")
    with Cache(db_path, registry=provider_registry):
        pass
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE provider_connections SET options = ? WHERE connection_id = ?",
            ('{"feed": "removed", "environment": "paper"}', "alpaca-paper"),
        )
    with Cache(db_path, registry=provider_registry) as cache:
        connection = cache.get_provider_connection("alpaca-paper")
        assert connection == provider_registry.build_connection("alpaca-paper")
        assert connection in cache.get_provider_connections()


@pytest.mark.parametrize("close_by_saving", [True, False])
def test_dialog_retains_inputs_until_success_or_cancel(
    provider_registry: ProviderRegistry, qtbot: QtBot, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, close_by_saving: bool,
) -> None:
    class Store:
        unavailable = True

        def read(
            self, connection_id: str, fields: tuple[CredentialField, ...], display_name: str,
        ) -> ProviderCredentials | None:
            return None

        def put(self, connection_id: str, credentials: ProviderCredentials) -> None:
            if self.unavailable:
                raise CredentialStoreUnavailableError("Test keyring unavailable.")

        def delete(self, connection_id: str) -> None:
            raise AssertionError("Unexpected credential deletion")

    warnings: list[str] = []
    monkeypatch.setattr(settings, "show_warning", lambda parent, title, message: warnings.append(title))
    store = Store()
    with Cache(str(tmp_path / "settings.db"), registry=provider_registry) as cache:
        dialog = ConnectionDialog(provider_registry.build_connection("alpaca-paper"), cache, store)
        qtbot.addWidget(dialog)
        key_id = dialog.findChild(QLineEdit, "credential_api_key_id")
        secret = dialog.findChild(QLineEdit, "credential_api_secret")
        assert key_id is not None and secret is not None
        dialog.show()
        key_id.setText("synthetic-test-id")
        dialog.accept()
        assert dialog.isVisible()
        assert key_id.text() == "synthetic-test-id"
        secret.setText("synthetic-test-value")
        dialog.accept()
        assert dialog.isVisible()
        assert key_id.text() == "synthetic-test-id" and secret.text() == "synthetic-test-value"
        assert warnings == ["Incomplete Credentials", "Credential Store Error"]

        if close_by_saving:
            store.unavailable = False
            dialog.accept()
        else:
            dialog.reject()
        assert not dialog.isVisible()
        assert key_id.text() == secret.text() == ""


def test_yahoo_normalizes_offset_news_timestamp_to_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    def get_news(ticker: object, *, count: int) -> list[dict[str, object]]:
        return [
            {"content": {"title": "Offset timestamp", "pubDate": "2026-01-05T10:00:00-05:00"}},
            {"content": {"title": "Naive timestamp", "pubDate": "2026-01-05T10:00:00"}},
        ]

    monkeypatch.setattr("providers.yahoo.implementation.yf.Ticker.get_news", get_news)
    articles = YFinanceProvider().fetch_news("ACME")
    assert articles is not None
    first, second = articles
    assert first.published == datetime(2026, 1, 5, 15, tzinfo=UTC)
    assert first.published.tzinfo is UTC
    assert second.published is None
