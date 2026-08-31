from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

import pytest

from app.bar_comparison import (
    BarComparisonResult,
    BarComparisonRow,
    BarComparisonRowKind,
    BarComparisonService,
)
from data.models import Bar, Level1Quote, MarketSnapshot, Timeframe
from data.provider import ProviderAvailability
from data.provider.base import DataProvider, MarketDataEntitlementError
from data.provider.config import (
    ALPACA_LIVE_CONNECTION_ID,
    ALPACA_PAPER_CONNECTION_ID,
    MarketDataFeed,
    ProviderConnection,
    alpaca_live_connection,
    alpaca_paper_connection,
    yfinance_connection,
)
from data.provider.credentials import CredentialStore, ProviderCredentials


_SESSION = datetime(2026, 2, 2, 5, 0, tzinfo=UTC)
_AVAILABLE = {
    "yfinance": ProviderAvailability(True, None),
    "alpaca": ProviderAvailability(True, None),
}


class _MemoryCredentialStore:
    def __init__(
        self,
        configured: bool = True,
        connection_ids: set[str] | None = None,
    ) -> None:
        self._connection_ids = (
            {ALPACA_PAPER_CONNECTION_ID}
            if configured and connection_ids is None
            else set() if connection_ids is None else connection_ids
        )

    def get(self, connection_id: str) -> ProviderCredentials | None:
        if connection_id not in self._connection_ids:
            return None
        return ProviderCredentials("synthetic-key", "synthetic-secret")

    def put(
        self,
        connection_id: str,
        credentials: ProviderCredentials,
    ) -> None:
        raise NotImplementedError

    def delete(self, connection_id: str) -> None:
        raise NotImplementedError


class _Provider(DataProvider):
    def __init__(
        self,
        bars: list[Bar] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.bars = [] if bars is None else bars
        self.error = error
        self.calls: list[tuple[str, Timeframe, datetime, datetime]] = []

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        self.calls.append((symbol, timeframe, start, end))
        if self.error is not None:
            raise self.error
        return self.bars

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        return {}

    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        return None

    def native_timeframes(self) -> list[Timeframe]:
        return [
            Timeframe.MIN5,
            Timeframe.MIN15,
            Timeframe.MIN30,
            Timeframe.DAILY,
            Timeframe.WEEKLY,
        ]


class _ExclusiveStartProvider(_Provider):
    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        self.calls.append((symbol, timeframe, start, end))
        return [bar for bar in self.bars if start < bar.timestamp < end]


class _ProviderFactory:
    def __init__(self, providers: Mapping[str, DataProvider]) -> None:
        self.providers = dict(providers)
        self.connections: list[ProviderConnection] = []

    def __call__(
        self,
        connection: ProviderConnection,
        credential_store: CredentialStore,
    ) -> DataProvider:
        self.connections.append(connection)
        provider = self.providers.get(connection.cache_namespace)
        if provider is None:
            raise RuntimeError("provider construction failed: synthetic-secret")
        return provider


def test_sip_origin_constructs_feed_specific_alpaca_and_yahoo_providers() -> None:
    cached = _bar(low=68.64)
    factory = _ProviderFactory(
        {
            "alpaca:delayed_sip": _Provider([_bar(low=685.77)]),
            "alpaca:iex": _Provider([_bar(low=685.4)]),
            "yfinance": _Provider(
                [_bar(timestamp=datetime(2026, 2, 2, 0, 0, tzinfo=UTC))]
            ),
        }
    )

    result = _service(factory).compare(
        "alpaca:delayed_sip",
        "SPY",
        Timeframe.DAILY,
        cached,
    )

    assert [row.kind for row in result.rows] == [
        BarComparisonRowKind.CACHED_ORIGIN,
        BarComparisonRowKind.REFRESHED_ORIGIN,
        BarComparisonRowKind.CORROBORATION,
        BarComparisonRowKind.CORROBORATION,
    ]
    assert [row.source_namespace for row in result.rows] == [
        "alpaca:delayed_sip",
        "alpaca:delayed_sip",
        "alpaca:iex",
        "yfinance",
    ]
    alpaca_connections = [
        connection
        for connection in factory.connections
        if connection.provider_name == "alpaca"
    ]
    assert [connection.feed for connection in alpaca_connections] == [
        MarketDataFeed.DELAYED_SIP,
        MarketDataFeed.IEX,
    ]
    assert all(
        connection.connection_id == ALPACA_PAPER_CONNECTION_ID
        for connection in alpaca_connections
    )


def test_yahoo_origin_without_alpaca_credentials_has_only_origin_rows() -> None:
    factory = _ProviderFactory({"yfinance": _Provider([_bar()])})
    service = _service(factory, credentials=False)

    result = service.compare("yfinance", "SPY", Timeframe.DAILY, _bar())

    assert [row.kind for row in result.rows] == [
        BarComparisonRowKind.CACHED_ORIGIN,
        BarComparisonRowKind.REFRESHED_ORIGIN,
    ]
    assert [connection.provider_name for connection in factory.connections] == [
        "yfinance"
    ]


def test_yahoo_origin_with_alpaca_credentials_adds_iex_and_sip_rows() -> None:
    yahoo_timestamp = datetime(2026, 2, 2, 0, 0, tzinfo=UTC)
    factory = _ProviderFactory(
        {
            "yfinance": _Provider([_bar(timestamp=yahoo_timestamp)]),
            "alpaca:iex": _Provider([_bar()]),
            "alpaca:delayed_sip": _Provider([_bar()]),
        }
    )

    result = _service(factory).compare(
        "yfinance",
        "SPY",
        Timeframe.DAILY,
        _bar(timestamp=yahoo_timestamp),
    )

    assert [row.source_namespace for row in result.rows] == [
        "yfinance",
        "yfinance",
        "alpaca:iex",
        "alpaca:delayed_sip",
    ]


def test_comparison_prefers_active_alpaca_connection_for_feed_variants() -> None:
    factory = _ProviderFactory(
        {
            "alpaca:delayed_sip": _Provider([_bar()]),
            "alpaca:iex": _Provider([_bar()]),
            "yfinance": _Provider([_bar()]),
        }
    )
    service = BarComparisonService(
        credential_store=_MemoryCredentialStore(
            connection_ids={
                ALPACA_PAPER_CONNECTION_ID,
                ALPACA_LIVE_CONNECTION_ID,
            }
        ),
        provider_connections=[
            yfinance_connection(),
            alpaca_paper_connection(),
            alpaca_live_connection(),
        ],
        provider_availability=_AVAILABLE,
        provider_factory=factory,
        preferred_connection_id=ALPACA_LIVE_CONNECTION_ID,
    )

    service.compare(
        "alpaca:delayed_sip",
        "SPY",
        Timeframe.DAILY,
        _bar(),
    )

    assert {
        connection.connection_id
        for connection in factory.connections
        if connection.provider_name == "alpaca"
    } == {ALPACA_LIVE_CONNECTION_ID}


def test_daily_comparison_matches_yahoo_and_alpaca_timestamp_styles() -> None:
    yahoo_midnight = datetime(2026, 2, 2, 0, 0, tzinfo=UTC)
    factory = _ProviderFactory(
        {
            "alpaca:delayed_sip": _Provider([_bar(low=685.77)]),
            "alpaca:iex": _Provider([_bar(low=685.5)]),
            "yfinance": _Provider([_bar(timestamp=yahoo_midnight, low=685.8)]),
        }
    )

    result = _service(factory).compare(
        "alpaca:delayed_sip",
        "SPY",
        Timeframe.DAILY,
        _bar(low=68.64),
    )

    yahoo = _row(result, "yfinance")
    assert yahoo.bar is not None
    assert yahoo.bar.timestamp == yahoo_midnight
    yahoo_provider = cast(_Provider, factory.providers["yfinance"])
    assert len(yahoo_provider.calls) == 1
    assert yahoo_provider.calls[0][2] == datetime(2026, 2, 2, 0, 0, tzinfo=UTC)
    assert yahoo_provider.calls[0][3] == datetime(2026, 2, 3, 0, 0, tzinfo=UTC)


def test_intraday_request_window_is_padded_around_target_bar() -> None:
    timestamp = datetime(2026, 2, 2, 14, 30, tzinfo=UTC)
    provider = _ExclusiveStartProvider([_bar(timestamp=timestamp)])
    factory = _ProviderFactory({"yfinance": provider})

    result = _service(factory, credentials=False).compare(
        "yfinance",
        "SPY",
        Timeframe.MIN15,
        _bar(timestamp=timestamp),
    )

    refreshed = _row(result, "yfinance", refreshed=True)
    assert refreshed.bar is not None
    assert provider.calls[0][2] == datetime(2026, 2, 2, 14, 15, tzinfo=UTC)
    assert provider.calls[0][3] == datetime(2026, 2, 2, 15, 0, tzinfo=UTC)


def test_weekly_comparison_matches_sunday_stamp_to_monday_anchor() -> None:
    monday = datetime(2026, 2, 2, 5, 0, tzinfo=UTC)
    sunday = datetime(2026, 2, 8, 15, 0, tzinfo=UTC)
    factory = _ProviderFactory(
        {
            "alpaca:delayed_sip": _Provider([_bar(timestamp=monday)]),
            "alpaca:iex": _Provider([_bar(timestamp=monday)]),
            "yfinance": _Provider([_bar(timestamp=sunday)]),
        }
    )

    result = _service(factory).compare(
        "alpaca:delayed_sip",
        "SPY",
        Timeframe.WEEKLY,
        _bar(timestamp=monday),
    )

    yahoo = _row(result, "yfinance")
    assert yahoo.bar is not None
    assert yahoo.bar.timestamp == sunday


def test_provider_failures_are_isolated_and_sanitized() -> None:
    factory = _ProviderFactory(
        {
            "alpaca:delayed_sip": _Provider(
                error=MarketDataEntitlementError("account detail")
            ),
            "alpaca:iex": _Provider(error=RuntimeError("synthetic-secret")),
            "yfinance": _Provider([]),
        }
    )

    result = _service(factory).compare(
        "alpaca:delayed_sip",
        "SPY",
        Timeframe.DAILY,
        _bar(),
    )

    refreshed = _row(result, "alpaca:delayed_sip", refreshed=True)
    iex = _row(result, "alpaca:iex")
    yahoo = _row(result, "yfinance")
    assert refreshed.error == "Market-data entitlement does not permit this request."
    assert iex.error == "The provider request failed."
    assert yahoo.error == "No matching bar was returned."
    assert "synthetic-secret" not in repr(result)


def test_provider_construction_error_is_sanitized() -> None:
    factory = _ProviderFactory(
        {
            "alpaca:delayed_sip": _Provider([_bar()]),
            "yfinance": _Provider([_bar()]),
        }
    )

    result = _service(factory).compare(
        "alpaca:delayed_sip",
        "SPY",
        Timeframe.DAILY,
        _bar(),
    )

    iex = _row(result, "alpaca:iex")
    assert iex.error == "The provider request failed."
    assert "synthetic-secret" not in repr(result)


def test_multiple_matching_provider_bars_are_reported_as_ambiguous() -> None:
    second_stamp = datetime(2026, 2, 2, 6, 0, tzinfo=UTC)
    factory = _ProviderFactory(
        {
            "yfinance": _Provider([_bar(), _bar(timestamp=second_stamp)]),
        }
    )

    result = _service(factory, credentials=False).compare(
        "yfinance",
        "SPY",
        Timeframe.DAILY,
        _bar(timestamp=datetime(2026, 2, 2, 0, 0, tzinfo=UTC)),
    )

    refreshed = _row(result, "yfinance", refreshed=True)
    assert refreshed.error == "Multiple matching bars were returned."


def test_directional_corroboration_accepts_sources_near_revision_not_suspect() -> None:
    cached = _bar(low=68.64)
    factory = _ProviderFactory(
        {
            "alpaca:delayed_sip": _Provider([_bar(low=685.77)]),
            "alpaca:iex": _Provider([_bar(low=685.4)]),
            "yfinance": _Provider([_bar(low=686.1)]),
        }
    )

    result = _service(factory).compare(
        "alpaca:delayed_sip",
        "SPY",
        Timeframe.DAILY,
        cached,
    )

    low = next(item for item in result.suggestions if item.field == "low")
    assert low.refreshed_value == pytest.approx(685.77)
    assert low.corroborating_sources == ("alpaca:iex", "yfinance")


def test_source_near_suspect_does_not_corroborate_revision() -> None:
    cached = _bar(low=680.0)
    factory = _ProviderFactory(
        {
            "alpaca:delayed_sip": _Provider([_bar(low=685.77)]),
            "alpaca:iex": _Provider([_bar(low=680.1)]),
            "yfinance": _Provider([_bar(low=685.7)]),
        }
    )

    result = _service(factory).compare(
        "alpaca:delayed_sip",
        "SPY",
        Timeframe.DAILY,
        cached,
    )

    low = next(item for item in result.suggestions if item.field == "low")
    assert low.corroborating_sources == ("yfinance",)


def test_uniform_price_ratio_is_flagged_as_adjustment_basis_difference() -> None:
    cached = Bar(_SESSION, 100.0, 110.0, 90.0, 105.0, 1_000)
    refreshed = Bar(_SESSION, 100.0, 110.0, 90.0, 105.0, 1_000)
    adjusted = Bar(_SESSION, 50.0, 55.0, 45.0, 52.5, 2_000)
    factory = _ProviderFactory(
        {
            "alpaca:delayed_sip": _Provider([refreshed]),
            "alpaca:iex": _Provider([adjusted]),
            "yfinance": _Provider([refreshed]),
        }
    )

    result = _service(factory).compare(
        "alpaca:delayed_sip",
        "SPY",
        Timeframe.DAILY,
        cached,
    )

    assert _row(result, "alpaca:iex").adjustment_basis_difference
    assert not _row(result, "yfinance").adjustment_basis_difference


def test_unchanged_origin_has_no_suggestions() -> None:
    factory = _ProviderFactory({"yfinance": _Provider([_bar()])})

    result = _service(factory, credentials=False).compare(
        "yfinance",
        "SPY",
        Timeframe.DAILY,
        _bar(),
    )

    assert result.origin_unchanged
    assert result.suggestions == ()


def test_subcent_provider_wobble_is_treated_as_unchanged() -> None:
    cached = _bar()
    refreshed = Bar(
        _SESSION,
        cached.open + 0.005,
        cached.high,
        cached.low,
        cached.close,
        cached.volume,
        cached.vwap,
    )
    factory = _ProviderFactory({"yfinance": _Provider([refreshed])})

    result = _service(factory, credentials=False).compare(
        "yfinance",
        "SPY",
        Timeframe.DAILY,
        cached,
    )

    assert result.origin_unchanged
    assert result.suggestions == ()


def test_synthesized_timeframe_is_rejected_without_provider_requests() -> None:
    factory = _ProviderFactory({"yfinance": _Provider([_bar()])})

    with pytest.raises(ValueError, match="synthesized"):
        _service(factory).compare(
            "yfinance",
            "SPY",
            Timeframe.MIN39,
            _bar(),
        )

    assert factory.connections == []


def test_cancellation_stops_before_starting_the_next_provider_request() -> None:
    factory = _ProviderFactory(
        {
            "alpaca:delayed_sip": _Provider([_bar()]),
            "alpaca:iex": _Provider([_bar()]),
            "yfinance": _Provider([_bar()]),
        }
    )
    checks = 0

    def should_cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    with pytest.raises(InterruptedError):
        _service(factory).compare(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            _bar(),
            should_cancel=should_cancel,
        )

    assert [
        connection.cache_namespace for connection in factory.connections
    ] == ["alpaca:delayed_sip"]


def _service(
    factory: _ProviderFactory,
    *,
    credentials: bool = True,
) -> BarComparisonService:
    return BarComparisonService(
        credential_store=_MemoryCredentialStore(credentials),
        provider_connections=[
            yfinance_connection(),
            alpaca_paper_connection(),
        ],
        provider_availability=_AVAILABLE,
        provider_factory=factory,
        preferred_connection_id=ALPACA_PAPER_CONNECTION_ID,
    )


def _row(
    result: BarComparisonResult,
    namespace: str,
    *,
    refreshed: bool = False,
) -> BarComparisonRow:
    kind = (
        BarComparisonRowKind.REFRESHED_ORIGIN
        if refreshed
        else BarComparisonRowKind.CORROBORATION
    )
    return next(
        row
        for row in result.rows
        if row.source_namespace == namespace and row.kind == kind
    )


def _bar(
    *,
    timestamp: datetime = _SESSION,
    low: float = 685.77,
) -> Bar:
    return Bar(timestamp, 685.9, 693.21, low, 691.7, 79_286_521, 691.25)
