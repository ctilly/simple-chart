import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.common.exceptions import APIError
from alpaca.common import rest as alpaca_rest
from requests import PreparedRequest, Response, Session
from requests.adapters import HTTPAdapter

from data.models import Timeframe
from data.provider import alpaca_provider, create_provider
from data.provider.alpaca_provider import (
    AlpacaProvider,
    _create_stock_historical_data_client,
)
from data.provider.base import (
    MarketDataEntitlementError,
    RequestCancelledError,
    UnsupportedTimeframeError,
    background_requests,
)
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


def test_production_alpaca_transport_supplies_default_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeouts: list[object] = []

    def record_send(
        _adapter: HTTPAdapter,
        _request: PreparedRequest,
        stream: bool = False,
        timeout: object = None,
        verify: bool | str = True,
        cert: str | tuple[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> Response:
        observed_timeouts.append(timeout)
        return Response()

    monkeypatch.setattr(HTTPAdapter, "send", record_send)
    client = _create_stock_historical_data_client("key", "secret")
    session = cast(Session, cast(Any, client)._session)
    adapter = session.get_adapter("https://data.alpaca.markets/v2/stocks/bars")
    request = PreparedRequest()
    request.prepare(
        method="GET",
        url="https://data.alpaca.markets/v2/stocks/bars",
    )

    adapter.send(request)
    adapter.send(request, timeout=2.0)

    assert observed_timeouts == [(5.0, 15.0), 2.0]


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


def test_history_page_span_chunks_intraday_history_only() -> None:
    provider = AlpacaProvider("key", "secret", MarketDataFeed.IEX, _FakeClient())

    for timeframe in (Timeframe.MIN1, Timeframe.MIN5, Timeframe.MIN15, Timeframe.MIN30):
        assert provider.history_page_span(timeframe) == timedelta(days=30)
    assert provider.history_page_span(Timeframe.DAILY) is None
    assert provider.history_page_span(Timeframe.WEEKLY) is None


def test_each_thread_uses_its_own_production_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[_FakeClient, int]] = []

    def create_client(api_key_id: str, api_secret: str) -> _FakeClient:
        client = _FakeClient()
        created.append((client, threading.get_ident()))
        return client

    monkeypatch.setattr(
        alpaca_provider,
        "_create_stock_historical_data_client",
        create_client,
    )
    provider = AlpacaProvider("key", "secret", MarketDataFeed.IEX)
    start = datetime(2026, 1, 5, tzinfo=UTC)
    end = datetime(2026, 1, 6, tzinfo=UTC)

    provider.fetch_bars("SPY", Timeframe.MIN5, start, end)
    provider.fetch_bars("SPY", Timeframe.MIN5, start, end)
    worker = threading.Thread(
        target=provider.fetch_bars,
        args=("SPY", Timeframe.MIN5, start, end),
    )
    worker.start()
    worker.join()

    assert len(created) == 2
    (main_client, main_thread), (worker_client, worker_thread) = created
    assert main_thread == threading.get_ident()
    assert worker_thread != main_thread
    assert len(main_client.bar_requests) == 2
    assert len(worker_client.bar_requests) == 1


def test_provider_state_does_not_expose_credential_values() -> None:
    provider = AlpacaProvider(
        "synthetic-key-id",
        "synthetic-secret-value",
        MarketDataFeed.IEX,
        _FakeClient(),
    )

    rendered = repr(provider) + repr(vars(provider))
    assert "synthetic-key-id" not in rendered
    assert "synthetic-secret-value" not in rendered


def _json_response(status: int, body: dict[str, object]) -> Response:
    response = Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    response.headers["Content-Type"] = "application/json"
    return response


def _raw_bar(timestamp: str, close: float) -> dict[str, object]:
    return {
        "t": timestamp, "o": close, "h": close, "l": close, "c": close,
        "v": 100, "n": 1, "vw": close,
    }


def test_every_sdk_http_attempt_is_spaced_and_retry_statuses_are_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    responses = [
        _json_response(429, {"message": "too many requests"}),
        _json_response(
            200,
            {"bars": {"SPY": [_raw_bar("2026-01-05T15:00:00Z", 1.0)]},
             "next_page_token": "page-2"},
        ),
        _json_response(504, {"message": "gateway timeout"}),
        _json_response(
            200,
            {"bars": {"SPY": [_raw_bar("2026-01-05T15:05:00Z", 2.0)]},
             "next_page_token": None},
        ),
    ]
    sent_urls: list[str] = []

    def send(
        _adapter: HTTPAdapter,
        request: PreparedRequest,
        **_kwargs: object,
    ) -> Response:
        sent_urls.append(str(request.url))
        response = responses.pop(0)
        response.request = request
        return response

    spaced: list[int] = []
    monkeypatch.setattr(HTTPAdapter, "send", send)
    monkeypatch.setattr(alpaca_rest.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        alpaca_provider,
        "_REQUEST_LIMITER",
        _RecordingSpacer(spaced),
    )
    monkeypatch.setattr(
        alpaca_provider,
        "_RETRY_STATUS_COUNTER",
        alpaca_provider._RetryStatusCounter(),
    )
    provider = AlpacaProvider(
        "synthetic-key-id",
        "synthetic-secret-value",
        MarketDataFeed.IEX,
    )

    with caplog.at_level(logging.WARNING, logger=alpaca_provider.__name__):
        bars = provider.fetch_bars(
            "SPY",
            Timeframe.MIN5,
            datetime(2026, 1, 5, tzinfo=UTC),
            datetime(2026, 1, 6, tzinfo=UTC),
        )

    assert [bar.close for bar in bars] == [1.0, 2.0]
    assert len(sent_urls) == 4
    assert len(spaced) == 4
    retry_messages = [record.getMessage() for record in caplog.records]
    assert len(retry_messages) == 2
    assert "HTTP 429" in retry_messages[0]
    assert "(1 retryable responses" in retry_messages[0]
    assert "HTTP 504" in retry_messages[1]
    assert "(2 retryable responses" in retry_messages[1]
    for message in retry_messages:
        assert "synthetic" not in message
        assert "http" not in message.replace("HTTP", "")
        assert "page-2" not in message


class _RecordingSpacer:
    def __init__(self, calls: list[int]) -> None:
        self._calls = calls

    def wait(self, cancellation: threading.Event | None = None) -> None:
        self._calls.append(1)


def test_cancelled_background_request_sends_no_further_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = threading.Event()
    sent: list[str] = []

    def send(
        _adapter: HTTPAdapter,
        request: PreparedRequest,
        **_kwargs: object,
    ) -> Response:
        sent.append(str(request.url))
        cancellation.set()  # the user leaves while the first page is in flight
        response = _json_response(
            200,
            {"bars": {"SPY": [_raw_bar("2026-01-05T15:00:00Z", 1.0)]},
             "next_page_token": "page-2" if len(sent) < 3 else None},
        )
        response.request = request
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    monkeypatch.setattr(
        alpaca_provider, "_REQUEST_LIMITER", _unlimited(),
    )
    provider = AlpacaProvider("synthetic-key-id", "synthetic-secret-value", MarketDataFeed.IEX)

    with background_requests(cancellation):
        with pytest.raises(RequestCancelledError):
            provider.fetch_bars(
                "SPY",
                Timeframe.MIN5,
                datetime(2026, 1, 5, tzinfo=UTC),
                datetime(2026, 1, 6, tzinfo=UTC),
            )

    assert len(sent) == 1


def test_cancellation_during_sdk_retry_wait_sends_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = threading.Event()
    sent: list[str] = []

    def send(
        _adapter: HTTPAdapter,
        request: PreparedRequest,
        **_kwargs: object,
    ) -> Response:
        sent.append(str(request.url))
        response = _json_response(429, {"message": "too many requests"})
        response.request = request
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    monkeypatch.setattr(alpaca_rest.time, "sleep", lambda _seconds: cancellation.set())
    monkeypatch.setattr(
        alpaca_provider, "_REQUEST_LIMITER", _unlimited(),
    )
    monkeypatch.setattr(
        alpaca_provider, "_RETRY_STATUS_COUNTER", alpaca_provider._RetryStatusCounter(),
    )
    provider = AlpacaProvider("synthetic-key-id", "synthetic-secret-value", MarketDataFeed.IEX)

    with background_requests(cancellation):
        with pytest.raises(RequestCancelledError):
            provider.fetch_bars(
                "SPY",
                Timeframe.MIN5,
                datetime(2026, 1, 5, tzinfo=UTC),
                datetime(2026, 1, 6, tzinfo=UTC),
            )

    assert len(sent) == 1



def _unlimited() -> "alpaca_provider._RequestRateLimiter":
    return alpaca_provider._RequestRateLimiter(1e9, 1_000_000, 0)


class _FakeTime:
    """Fake monotonic clock; sleep advances it and records the requested delay."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _limiter(fake: _FakeTime) -> "alpaca_provider._RequestRateLimiter":
    return alpaca_provider._RequestRateLimiter(2.0, 6, 3, fake.clock, fake.sleep)


def test_limiter_admits_a_burst_of_six_then_paces_at_two_per_second() -> None:
    fake = _FakeTime()
    limiter = _limiter(fake)

    for _ in range(6):
        limiter.wait()
    assert fake.sleeps == []
    limiter.wait()

    assert fake.sleeps == [pytest.approx(0.5)]


def test_long_idle_restores_exactly_the_burst_capacity() -> None:
    fake = _FakeTime()
    limiter = _limiter(fake)
    for _ in range(6):
        limiter.wait()

    fake.now += 3600.0
    for _ in range(6):
        limiter.wait()
    assert fake.sleeps == []
    limiter.wait()

    assert fake.sleeps == [pytest.approx(0.5)]


def test_background_keeps_a_reserve_for_foreground_and_polls_credit_once() -> None:
    fake = _FakeTime()
    limiter = _limiter(fake)
    for _ in range(3):
        limiter.wait()  # three tokens left: exactly the reserve
    start = fake.now

    limiter.wait(threading.Event())

    # Needs a fourth token: 0.5 s at 2 tokens/s, credited once across 50 ms polls.
    assert fake.now - start == pytest.approx(0.5)
    assert all(delay <= 0.05 + 1e-9 for delay in fake.sleeps)
    fake.sleeps.clear()
    for _ in range(3):
        limiter.wait()  # the reserve is intact for a click
    assert fake.sleeps == []


def test_background_is_not_admitted_while_foreground_waits() -> None:
    fake = _FakeTime()
    limiter = _limiter(fake)
    limiter._foreground_waiting = 1
    polls: list[float] = []

    def sleep(seconds: float) -> None:
        polls.append(seconds)
        fake.now += seconds
        if len(polls) == 2:
            limiter._foreground_waiting = 0

    limiter._sleep = sleep

    limiter.wait(threading.Event())  # full bucket, yet must wait for foreground

    assert len(polls) == 2


def test_cancelled_background_waiter_consumes_no_token() -> None:
    fake = _FakeTime()
    cancellation = threading.Event()
    limiter = _limiter(fake)
    for _ in range(6):
        limiter.wait()

    def sleep(seconds: float) -> None:
        cancellation.set()
        fake.now += seconds

    limiter._sleep = sleep
    with pytest.raises(RequestCancelledError):
        limiter.wait(cancellation)

    limiter._sleep = fake.sleep
    tokens_before = limiter._tokens
    limiter.wait()  # the cancelled caller left its credit for this one
    assert tokens_before >= 0.0


def test_late_wakeups_share_one_token_balance() -> None:
    fake = _FakeTime()
    limiter = _limiter(fake)
    for _ in range(6):
        limiter.wait()
    admitted: list[float] = []
    nested = [False]

    def sleep(seconds: float) -> None:
        fake.now += seconds
        if not nested[0]:
            # While the first caller sleeps, a newer caller takes the token
            # that its wakeup was counting on.
            nested[0] = True
            limiter.wait()
            admitted.append(fake.now)

    limiter._sleep = sleep
    limiter.wait()
    admitted.append(fake.now)

    assert len(admitted) == 2
    assert admitted[1] - 1000.0 >= 1.0 - 1e-9  # two refilled tokens needed 1.0 s


def test_no_sixty_second_window_exceeds_126_admissions() -> None:
    fake = _FakeTime()
    limiter = _limiter(fake)
    admissions: list[float] = []
    # Saturating mixed traffic with idle gaps, for five minutes.
    while fake.now < 1300.0:
        limiter.wait(threading.Event() if len(admissions) % 3 else None)
        admissions.append(fake.now)
        if len(admissions) % 50 == 0:
            fake.now += 7.0
    for index, start in enumerate(admissions):
        in_window = [t for t in admissions[index:] if t < start + 60.0]
        assert len(in_window) <= 126


def test_production_limiter_stays_well_under_alpacas_minute_limit() -> None:
    worst_minute = (
        alpaca_provider._REQUEST_BURST + 60 * alpaca_provider._REQUEST_RATE_PER_SECOND
    )

    assert worst_minute <= 150  # Alpaca allows 200 historical requests/minute
    # Two consecutive new-symbol clicks (up to 5 requests each) fit in one burst.
    assert alpaca_provider._REQUEST_BURST >= 10


def test_cancelled_background_caller_leaves_the_reserve_intact() -> None:
    fake = _FakeTime()
    limiter = _limiter(fake)
    for _ in range(3):
        limiter.wait()  # exactly the 3-token reserve remains
    cancellation = threading.Event()

    def cancel_on_sleep(seconds: float) -> None:
        cancellation.set()

    limiter._sleep = cancel_on_sleep
    with pytest.raises(RequestCancelledError):
        limiter.wait(cancellation)

    limiter._sleep = fake.sleep
    for _ in range(3):
        limiter.wait()
    assert fake.sleeps == []


def test_already_cancelled_background_caller_takes_nothing_from_a_full_bucket() -> None:
    fake = _FakeTime()
    limiter = _limiter(fake)
    cancellation = threading.Event()
    cancellation.set()

    with pytest.raises(RequestCancelledError):
        limiter.wait(cancellation)

    for _ in range(6):
        limiter.wait()
    assert fake.sleeps == []


def test_real_waiting_foreground_caller_blocks_background_admission() -> None:
    fake = _FakeTime()
    limiter = _limiter(fake)
    for _ in range(6):
        limiter.wait()  # empty bucket: the next foreground caller must wait
    background_blocked: list[float] = []
    background_cancel = threading.Event()
    sleep_calls = [0]

    def sleep(seconds: float) -> None:
        sleep_calls[0] += 1
        if sleep_calls[0] == 1:
            # Foreground is sleeping for its token. Give background enough
            # credit (4 tokens) and try to admit it now.
            fake.now += 2.0
            try:
                limiter.wait(background_cancel)
            except RequestCancelledError:
                pass
            else:
                background_blocked.append(-1.0)  # admitted ahead of foreground
            return
        # Background's own sleep: it was correctly held back.
        background_blocked.append(seconds)
        background_cancel.set()

    limiter._sleep = sleep
    limiter.wait()  # the real foreground caller

    assert background_blocked and background_blocked[0] >= 0.0
    assert limiter._foreground_waiting == 0


def test_saturating_foreground_traffic_never_exceeds_the_minute_bound() -> None:
    fake = _FakeTime()
    limiter = _limiter(fake)  # rate 2/s, capacity 6
    admissions: list[float] = []
    while fake.now < 1000.0 + 300.0:
        limiter.wait()
        admissions.append(fake.now)
        if len(admissions) == 200:
            fake.now += 100.0  # idle: the bucket refills to capacity
    bound = 6 + 60 * 2  # capacity + one minute of refill
    peak = max(
        sum(1 for t in admissions[index:] if t <= start + 60.0)
        for index, start in enumerate(admissions)
    )

    assert peak <= bound
    assert peak >= bound - 1  # the trace really is saturating
