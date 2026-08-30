from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.common.exceptions import APIError

from data.models import Timeframe
from data.provider import create_provider
from data.provider.alpaca_provider import AlpacaProvider
from data.provider.base import MarketDataEntitlementError, UnsupportedTimeframeError
from data.provider.config import MarketDataFeed, alpaca_paper_connection
from data.provider.credentials import ProviderCredentials


@dataclass
class _FakeBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None


@dataclass
class _FakeTrade:
    timestamp: datetime
    price: float


@dataclass
class _FakeQuote:
    bid_price: float
    bid_size: float
    ask_price: float
    ask_size: float


@dataclass
class _FakeSnapshot:
    latest_trade: _FakeTrade | None
    latest_quote: _FakeQuote | None
    daily_bar: _FakeBar | None
    previous_daily_bar: _FakeBar | None


@dataclass
class _FakeBarSet:
    data: dict[str, list[_FakeBar]]


class _FakeClient:
    def __init__(
        self,
        bars: dict[str, list[_FakeBar]] | None = None,
        snapshots: dict[str, _FakeSnapshot] | None = None,
    ) -> None:
        self.bars = {} if bars is None else bars
        self.snapshots = {} if snapshots is None else snapshots
        self.bar_requests: list[StockBarsRequest] = []
        self.snapshot_requests: list[StockSnapshotRequest] = []

    def get_stock_bars(self, request_params: StockBarsRequest) -> _FakeBarSet:
        self.bar_requests.append(request_params)
        return _FakeBarSet(self.bars)

    def get_stock_snapshot(
        self,
        request_params: StockSnapshotRequest,
    ) -> dict[str, _FakeSnapshot]:
        self.snapshot_requests.append(request_params)
        return self.snapshots


class _EntitlementErrorClient(_FakeClient):
    def get_stock_bars(self, request_params: StockBarsRequest) -> _FakeBarSet:
        raise APIError(  # type: ignore[no-untyped-call]
            '{"code":40310000,"message":"market data access denied"}'
        )

    def get_stock_snapshot(
        self,
        request_params: StockSnapshotRequest,
    ) -> dict[str, _FakeSnapshot]:
        raise APIError(  # type: ignore[no-untyped-call]
            '{"code":40310000,"message":"market data access denied"}'
        )


class _NonEntitlementErrorClient(_FakeClient):
    def get_stock_snapshot(
        self,
        request_params: StockSnapshotRequest,
    ) -> dict[str, _FakeSnapshot]:
        raise APIError(  # type: ignore[no-untyped-call]
            '{"code":40010001,"message":"subscription does not permit this request"}'
        )


class _CredentialStore:
    def __init__(self, credentials: ProviderCredentials) -> None:
        self._credentials = credentials

    def get(self, connection_id: str) -> ProviderCredentials | None:
        return self._credentials

    def put(
        self,
        connection_id: str,
        credentials: ProviderCredentials,
    ) -> None:
        self._credentials = credentials

    def delete(self, connection_id: str) -> None:
        self._credentials = ProviderCredentials("", "")


def test_provider_factory_constructs_alpaca_provider() -> None:
    provider = create_provider(
        alpaca_paper_connection(),
        _CredentialStore(ProviderCredentials("key", "secret")),
    )

    assert isinstance(provider, AlpacaProvider)


def test_fetch_bars_maps_request_and_filters_extended_hours() -> None:
    regular_later = _FakeBar(
        datetime(2026, 1, 5, 15, 30, tzinfo=UTC),
        101.0,
        103.0,
        100.0,
        102.0,
        1_200.0,
        101.5,
    )
    regular_open = _FakeBar(
        datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
        100.0,
        102.0,
        99.0,
        101.0,
        1_000.0,
        100.5,
    )
    premarket = _FakeBar(
        datetime(2026, 1, 5, 13, 0, tzinfo=UTC),
        98.0,
        100.0,
        97.0,
        99.0,
        500.0,
    )
    client = _FakeClient(
        bars={"SPY": [regular_later, premarket, regular_open]}
    )
    provider = AlpacaProvider(
        "key",
        "secret",
        MarketDataFeed.SIP,
        client,
    )
    start = datetime(2026, 1, 5, tzinfo=UTC)
    end = datetime(2026, 1, 6, tzinfo=UTC)

    bars = provider.fetch_bars(" spy ", Timeframe.MIN5, start, end)

    assert [bar.timestamp for bar in bars] == [
        regular_open.timestamp,
        regular_later.timestamp,
    ]
    assert bars[0].volume == 1_000
    assert bars[0].vwap == pytest.approx(100.5)
    request = client.bar_requests[0]
    assert request.symbol_or_symbols == "SPY"
    assert str(request.timeframe) == "5Min"
    assert request.adjustment == Adjustment.ALL
    assert request.feed == DataFeed.SIP


def test_fetch_bars_keeps_daily_data_outside_intraday_filter() -> None:
    daily = _FakeBar(
        datetime(2026, 1, 5, 5, 0, tzinfo=UTC),
        100.0,
        102.0,
        99.0,
        101.0,
        1_000.0,
    )
    client = _FakeClient(bars={"SPY": [daily]})
    provider = AlpacaProvider("key", "secret", MarketDataFeed.IEX, client)

    bars = provider.fetch_bars(
        "SPY",
        Timeframe.DAILY,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 6, tzinfo=UTC),
    )

    assert len(bars) == 1
    assert str(client.bar_requests[0].timeframe) == "1Day"
    assert client.bar_requests[0].feed == DataFeed.IEX


def test_delayed_sip_maps_feed_and_delays_latest_history_end() -> None:
    client = _FakeClient()
    provider = AlpacaProvider(
        "key",
        "secret",
        MarketDataFeed.DELAYED_SIP,
        client,
    )
    now = datetime(2026, 1, 5, 20, 0, tzinfo=UTC)

    provider.fetch_bars(
        "SPY",
        Timeframe.DAILY,
        datetime(2026, 1, 1, tzinfo=UTC),
        now,
    )

    assert client.bar_requests[0].feed == DataFeed.SIP
    assert provider.latest_history_end(Timeframe.DAILY, now) == datetime(
        2026, 1, 5, 19, 44, tzinfo=UTC
    )


def test_delayed_sip_uses_delayed_feed_for_snapshots() -> None:
    client = _FakeClient(snapshots={"SPY": _snapshot()})
    provider = AlpacaProvider(
        "key",
        "secret",
        MarketDataFeed.DELAYED_SIP,
        client,
    )

    provider.fetch_level1("SPY")

    assert client.snapshot_requests[0].feed == DataFeed.DELAYED_SIP


def test_realtime_sip_entitlement_error_is_actionable() -> None:
    provider = AlpacaProvider(
        "key",
        "secret",
        MarketDataFeed.SIP,
        _EntitlementErrorClient(),
    )

    with pytest.raises(
        MarketDataEntitlementError,
        match="15-minute delayed SIP or IEX",
    ):
        provider.fetch_level1("SPY")


def test_delayed_sip_entitlement_error_is_actionable() -> None:
    provider = AlpacaProvider(
        "key",
        "secret",
        MarketDataFeed.DELAYED_SIP,
        _EntitlementErrorClient(),
    )

    with pytest.raises(
        MarketDataEntitlementError,
        match="delayed SIP request as too recent",
    ):
        provider.fetch_bars(
            "SPY",
            Timeframe.DAILY,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 5, tzinfo=UTC),
        )


def test_non_entitlement_code_is_not_classified_by_message_text() -> None:
    provider = AlpacaProvider(
        "key",
        "secret",
        MarketDataFeed.SIP,
        _NonEntitlementErrorClient(),
    )

    with pytest.raises(APIError) as captured:
        provider.fetch_level1("SPY")

    assert captured.value.code == 40010001


def test_fetch_bars_rejects_synthesized_timeframe() -> None:
    provider = AlpacaProvider(
        "key",
        "secret",
        MarketDataFeed.IEX,
        _FakeClient(),
    )

    with pytest.raises(UnsupportedTimeframeError, match="does not support"):
        provider.fetch_bars(
            "SPY",
            Timeframe.MIN39,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("open", float("nan")),
        ("high", float("inf")),
        ("vwap", float("nan")),
    ],
)
def test_fetch_bars_rejects_non_finite_values(field: str, value: float) -> None:
    bar = _FakeBar(
        datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
        100.0,
        102.0,
        99.0,
        101.0,
        1_000.0,
        100.5,
    )
    setattr(bar, field, value)
    provider = AlpacaProvider(
        "key",
        "secret",
        MarketDataFeed.IEX,
        _FakeClient(bars={"SPY": [bar]}),
    )

    with pytest.raises(ValueError, match="non-finite"):
        provider.fetch_bars(
            "SPY",
            Timeframe.MIN5,
            datetime(2026, 1, 5, tzinfo=UTC),
            datetime(2026, 1, 6, tzinfo=UTC),
        )


def test_fetch_bars_rejects_timezone_naive_timestamp() -> None:
    bar = _FakeBar(
        datetime(2026, 1, 5, 14, 30),
        100.0,
        102.0,
        99.0,
        101.0,
        1_000.0,
    )
    provider = AlpacaProvider(
        "key",
        "secret",
        MarketDataFeed.IEX,
        _FakeClient(bars={"SPY": [bar]}),
    )

    with pytest.raises(ValueError, match="timezone-naive"):
        provider.fetch_bars(
            "SPY",
            Timeframe.DAILY,
            datetime(2026, 1, 5, tzinfo=UTC),
            datetime(2026, 1, 6, tzinfo=UTC),
        )


def test_fetch_snapshots_normalizes_symbols_and_maps_change() -> None:
    snapshot = _snapshot()
    client = _FakeClient(snapshots={"SPY": snapshot})
    provider = AlpacaProvider("key", "secret", MarketDataFeed.SIP, client)

    snapshots = provider.fetch_snapshots([" spy ", "SPY", "", "missing"])

    assert list(snapshots) == ["SPY"]
    converted = snapshots["SPY"]
    assert converted.last_price == pytest.approx(110.0)
    assert converted.previous_close == pytest.approx(100.0)
    assert converted.change == pytest.approx(10.0)
    assert converted.change_percent == pytest.approx(10.0)
    assert converted.timestamp == datetime(2026, 1, 5, 20, 0, tzinfo=UTC)
    request = client.snapshot_requests[0]
    assert request.symbol_or_symbols == ["SPY", "MISSING"]
    assert request.feed == DataFeed.SIP


def test_fetch_snapshots_skips_snapshot_without_price_data() -> None:
    empty = _FakeSnapshot(None, None, None, None)
    client = _FakeClient(snapshots={"SPY": empty})
    provider = AlpacaProvider("key", "secret", MarketDataFeed.IEX, client)

    assert provider.fetch_snapshots(["SPY"]) == {}


def test_fetch_snapshots_avoids_request_for_empty_symbols() -> None:
    client = _FakeClient()
    provider = AlpacaProvider("key", "secret", MarketDataFeed.IEX, client)

    assert provider.fetch_snapshots([" "]) == {}
    assert client.snapshot_requests == []


def test_fetch_level1_maps_snapshot_fields() -> None:
    client = _FakeClient(snapshots={"SPY": _snapshot()})
    provider = AlpacaProvider("key", "secret", MarketDataFeed.IEX, client)

    quote = provider.fetch_level1(" spy ")

    assert quote is not None
    assert quote.symbol == "SPY"
    assert quote.company_name is None
    assert quote.last_price == pytest.approx(110.0)
    assert quote.change_percent == pytest.approx(10.0)
    assert quote.bid == pytest.approx(109.9)
    assert quote.bid_size == 8
    assert quote.ask == pytest.approx(110.1)
    assert quote.ask_size == 12
    assert quote.open == pytest.approx(103.0)
    assert quote.high == pytest.approx(111.0)
    assert quote.low == pytest.approx(102.0)
    assert quote.volume == 2_500
    assert quote.previous_close == pytest.approx(100.0)


def test_fetch_level1_returns_none_for_missing_symbol() -> None:
    provider = AlpacaProvider(
        "key",
        "secret",
        MarketDataFeed.IEX,
        _FakeClient(),
    )

    assert provider.fetch_level1("") is None
    assert provider.fetch_level1("SPY") is None


def test_fetch_level1_returns_none_for_empty_snapshot() -> None:
    client = _FakeClient(
        snapshots={"SPY": _FakeSnapshot(None, None, None, None)}
    )
    provider = AlpacaProvider("key", "secret", MarketDataFeed.IEX, client)

    assert provider.fetch_level1("SPY") is None


def _snapshot() -> _FakeSnapshot:
    return _FakeSnapshot(
        latest_trade=_FakeTrade(
            datetime(2026, 1, 5, 20, 0, tzinfo=UTC),
            110.0,
        ),
        latest_quote=_FakeQuote(109.9, 8.0, 110.1, 12.0),
        daily_bar=_FakeBar(
            datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
            103.0,
            111.0,
            102.0,
            110.0,
            2_500.0,
        ),
        previous_daily_bar=_FakeBar(
            datetime(2026, 1, 2, 14, 30, tzinfo=UTC),
            99.0,
            101.0,
            98.0,
            100.0,
            2_000.0,
        ),
    )
