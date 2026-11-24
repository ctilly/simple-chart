import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

from data.provider.spec import CredentialField
from data.cache import Cache
from data.models import Bar, Timeframe
from data.provider import ProviderConfigurationError
from providers.alpaca import PROVIDER as ALPACA_PROVIDER
from data.provider.base import DataProvider
from data.provider.registry import FALLBACK_PROVIDER as YFINANCE_CONNECTION_ID
from providers.alpaca import (
    ALPACA_LIVE_CONNECTION_ID,
    ALPACA_PAPER_CONNECTION_ID,
    ConnectionEnvironment,
    MarketDataFeed,
)
from provider_support import alpaca_paper_connection, yfinance_connection, startup_registry
from data.provider.credentials import (
    CredentialStore,
    KeyringCredentialStore,
    ProviderCredentials,
)
from data.provider.registry import ProviderRegistry, discover_providers
from providers.yahoo.implementation import YFinanceProvider


class _FakeKeyring:
    def __init__(self) -> None:
        self.passwords: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.passwords.get((service_name, username))

    def set_password(
        self,
        service_name: str,
        username: str,
        password: str,
    ) -> None:
        self.passwords[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        del self.passwords[(service_name, username)]


class _MemoryCredentialStore:
    def __init__(self) -> None:
        self.credentials: dict[str, ProviderCredentials] = {}

    def get(self, connection_id: str) -> ProviderCredentials | None:
        return self.credentials.get(connection_id)

    def read(
        self,
        connection_id: str,
        fields: tuple[CredentialField, ...],
        display_name: str,
    ) -> ProviderCredentials | None:
        return self.get(connection_id)

    def put(
        self,
        connection_id: str,
        credentials: ProviderCredentials,
    ) -> None:
        self.credentials[connection_id] = credentials

    def delete(self, connection_id: str) -> None:
        self.credentials.pop(connection_id, None)


def test_cache_creates_three_fixed_connections_with_yahoo_active(
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db"), registry=startup_registry()) as cache:
        connections = cache.get_provider_connections()

        assert [connection.connection_id for connection in connections] == [
            YFINANCE_CONNECTION_ID,
            ALPACA_PAPER_CONNECTION_ID,
            ALPACA_LIVE_CONNECTION_ID,
        ]
        assert dict(connections[0].options).get("environment") is None
        assert dict(connections[0].options).get("feed") is None
        assert dict(connections[1].options).get("environment") == ConnectionEnvironment.PAPER
        assert dict(connections[1].options).get("feed") == MarketDataFeed.IEX
        assert dict(connections[2].options).get("environment") == ConnectionEnvironment.LIVE
        assert dict(connections[2].options).get("feed") == MarketDataFeed.IEX
        assert cache.get_active_provider_connection_id() == YFINANCE_CONNECTION_ID


def test_cache_updates_feed_and_selects_fixed_connection(tmp_path: Path) -> None:
    with Cache(str(tmp_path / "test.db"), registry=startup_registry()) as cache:
        cache.set_provider_connection_options(
            ALPACA_PAPER_CONNECTION_ID,
            {"feed": MarketDataFeed.SIP.value},
        )
        cache.set_active_provider_connection_id(ALPACA_PAPER_CONNECTION_ID)

        connection = cache.get_provider_connection(ALPACA_PAPER_CONNECTION_ID)
        assert connection is not None
        assert dict(connection.options).get("feed") == MarketDataFeed.SIP
        assert connection.cache_namespace == "alpaca:sip"
        assert cache.get_active_provider_connection_id() == ALPACA_PAPER_CONNECTION_ID


def test_delayed_sip_has_independent_cache_namespace(tmp_path: Path) -> None:
    with Cache(str(tmp_path / "test.db"), registry=startup_registry()) as cache:
        cache.set_provider_connection_options(
            ALPACA_PAPER_CONNECTION_ID,
            {"feed": MarketDataFeed.DELAYED_SIP.value},
        )

        connection = cache.get_provider_connection(ALPACA_PAPER_CONNECTION_ID)

    assert connection is not None
    assert dict(connection.options).get("feed") == MarketDataFeed.DELAYED_SIP
    assert connection.cache_namespace == "alpaca:delayed_sip"


def test_cache_rejects_feed_for_yahoo_and_unknown_active_connection(
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db"), registry=startup_registry()) as cache:
        with pytest.raises(ValueError, match="Invalid option for the data connection"):
            cache.set_provider_connection_options(
                YFINANCE_CONNECTION_ID,
                {"feed": MarketDataFeed.IEX.value},
            )
        with pytest.raises(ValueError, match="Unknown provider connection"):
            cache.set_active_provider_connection_id("another-provider")


def test_create_provider_constructs_yahoo_without_credentials(provider_registry: ProviderRegistry) -> None:
    provider = provider_registry.create_provider(yfinance_connection(), _MemoryCredentialStore())

    assert isinstance(provider, YFinanceProvider)


def test_create_provider_requires_alpaca_credentials(provider_registry: ProviderRegistry) -> None:
    with pytest.raises(ProviderConfigurationError, match="credentials"):
        provider_registry.create_provider(alpaca_paper_connection(), _MemoryCredentialStore())


def test_environment_variables_are_not_credential_fallback(
    provider_registry: ProviderRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APCA_API_KEY_ID", "environment-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "environment-secret")

    with pytest.raises(ProviderConfigurationError, match="credentials"):
        provider_registry.create_provider(alpaca_paper_connection(), _MemoryCredentialStore())


def test_create_provider_injects_connection_and_credentials() -> None:
    connection = alpaca_paper_connection(MarketDataFeed.SIP)
    credentials = ProviderCredentials({"api_key_id": "test-key-id", "api_secret": "test-api-secret"})
    store = _MemoryCredentialStore()
    store.put(connection.connection_id, credentials)
    received: list[tuple[object, object]] = []

    def factory(
        configured_connection: object,
        configured_credentials: object,
    ) -> DataProvider:
        received.append((configured_connection, configured_credentials))
        return YFinanceProvider()

    module = ModuleType("test_alpaca_provider")
    module.PROVIDER = replace(ALPACA_PROVIDER, factory=factory)
    registry = discover_providers(("providers.yahoo", module))

    provider = registry.create_provider(connection, store)

    assert isinstance(provider, YFinanceProvider)
    assert received == [(connection, credentials)]


def test_bar_cache_is_isolated_by_provider_feed(tmp_path: Path) -> None:
    timestamp = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
    iex_bar = _bar(timestamp, 100.0)
    sip_bar = _bar(timestamp, 101.0)
    timestamp_ms = int(timestamp.timestamp() * 1000)

    with Cache(str(tmp_path / "test.db"), registry=startup_registry()) as cache:
        cache.put_bars("alpaca:iex", "SPY", Timeframe.MIN5, [iex_bar])
        cache.put_bars("alpaca:sip", "SPY", Timeframe.MIN5, [sip_bar])

        assert cache.get_bars(
            "alpaca:iex", "SPY", Timeframe.MIN5, timestamp_ms, timestamp_ms
        ) == [iex_bar]
        assert cache.get_bars(
            "alpaca:sip", "SPY", Timeframe.MIN5, timestamp_ms, timestamp_ms
        ) == [sip_bar]
        assert cache.get_bars(
            "yfinance", "SPY", Timeframe.MIN5, timestamp_ms, timestamp_ms
        ) == []


def test_legacy_bars_migrate_to_yfinance_namespace(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    timestamp = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
    timestamp_ms = int(timestamp.timestamp() * 1000)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE bars (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER NOT NULL,
                vwap REAL,
                PRIMARY KEY (symbol, timeframe, timestamp)
            );
            """
        )
        connection.execute(
            """
            INSERT INTO bars (
                symbol, timeframe, timestamp, open, high, low, close, volume, vwap
            ) VALUES ('SPY', '5m', ?, 100, 102, 99, 101, 1000, NULL)
            """,
            (timestamp_ms,),
        )

    with Cache(str(db_path), registry=startup_registry()) as cache:
        yahoo_bars = cache.get_bars(
            "yfinance", "SPY", Timeframe.MIN5, timestamp_ms, timestamp_ms
        )
        alpaca_bars = cache.get_bars(
            "alpaca:iex", "SPY", Timeframe.MIN5, timestamp_ms, timestamp_ms
        )

    assert len(yahoo_bars) == 1
    assert yahoo_bars[0].close == 101.0
    assert alpaca_bars == []


def test_keyring_store_round_trips_and_deletes_credentials(provider_registry: ProviderRegistry) -> None:
    backend = _FakeKeyring()
    store: CredentialStore = KeyringCredentialStore(backend, registry=provider_registry)
    credentials = ProviderCredentials({"api_key_id": "test-key-id", "api_secret": "test-api-secret"})

    store.put(ALPACA_PAPER_CONNECTION_ID, credentials)

    stored = store.get(ALPACA_PAPER_CONNECTION_ID)
    assert stored is not None
    assert stored.get("api_key_id") == credentials.get("api_key_id")
    assert stored.get("api_secret") == credentials.get("api_secret")
    stored_payload = next(iter(backend.passwords.values()))
    assert json.loads(stored_payload) == {
        "api_key_id": "test-key-id",
        "api_secret": "test-api-secret",
    }

    store.delete(ALPACA_PAPER_CONNECTION_ID)
    assert store.get(ALPACA_PAPER_CONNECTION_ID) is None
    store.delete(ALPACA_PAPER_CONNECTION_ID)
    store.delete(ALPACA_LIVE_CONNECTION_ID)
    assert backend.passwords == {}


def _bar(timestamp: datetime, close: float) -> Bar:
    return Bar(
        timestamp=timestamp,
        open=close - 1.0,
        high=close + 1.0,
        low=close - 2.0,
        close=close,
        volume=1_000,
    )
