from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
import logging
import math
import threading
import time
from typing import Never, Protocol, cast

from alpaca.common.constants import DEFAULT_RETRY_EXCEPTION_CODES
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame as AlpacaTimeFrame
from alpaca.data.timeframe import TimeFrameUnit
from requests import PreparedRequest, Response, Session  # type: ignore[import-untyped]
from requests.adapters import HTTPAdapter  # type: ignore[import-untyped]
from requests.exceptions import (  # type: ignore[import-untyped]
    ConnectionError as RequestsConnectionError,
    InvalidHeader,
    Timeout,
)

from data.errors import http_status
from data.models import Bar, Level1Quote, MarketSnapshot, Timeframe
from data.provider.base import (
    DataProvider,
    MarketDataEntitlementError,
    ProviderCredentialsMalformedError,
    ProviderCredentialsRejectedError,
    ProviderRateLimitedError,
    ProviderUnreachableError,
    RequestCancelledError,
    UnsupportedTimeframeError,
    background_request_cancellation,
)
from data.provider.config import MarketDataFeed
from data.session import is_regular_session_timestamp as _is_regular_session


_DELAYED_SIP_SAFE_LAG = timedelta(minutes=16)
_SIP_ENTITLEMENT_ERROR_CODE = 40310000
_ALPACA_REQUEST_TIMEOUT = (5.0, 15.0)
# Every Alpaca HTTP attempt from this process (bars, pages, snapshots, Level 1,
# SDK retries) takes a token: 2 per second on average (120/min) with bursts of up
# to 10, so the 3-5 requests of a new chart (quote, daily, history chunks) go out
# together, even for two quick clicks. Any 60 s window admits at most
# 10 + 120 = 130 requests, well under Alpaca's 200/minute account limit.
# Background history keeps 7 tokens in reserve for the next user action(s).
_REQUEST_RATE_PER_SECOND = 2.0
_REQUEST_BURST = 10
_BACKGROUND_RESERVE = 7
# One history request returns about 40 calendar days of intraday bars; 30-day
# chunks keep each chunk to a single page.
_INTRADAY_HISTORY_PAGE_SPAN = timedelta(days=30)
_RETRY_STATUS_CODES = frozenset(DEFAULT_RETRY_EXCEPTION_CODES)
# Longest a background caller sleeps before re-checking cancellation and
# waiting foreground callers.
_BACKGROUND_POLL_SECONDS = 0.05

_LOGGER = logging.getLogger(__name__)

_TIMEFRAME_MAP: dict[Timeframe, AlpacaTimeFrame] = {
    Timeframe.MIN1: AlpacaTimeFrame(1, TimeFrameUnit.Minute),
    Timeframe.MIN5: AlpacaTimeFrame(5, TimeFrameUnit.Minute),
    Timeframe.MIN15: AlpacaTimeFrame(15, TimeFrameUnit.Minute),
    Timeframe.MIN30: AlpacaTimeFrame(30, TimeFrameUnit.Minute),
    Timeframe.DAILY: AlpacaTimeFrame(1, TimeFrameUnit.Day),
    Timeframe.WEEKLY: AlpacaTimeFrame(1, TimeFrameUnit.Week),
}

_BAR_FEED_MAP: dict[MarketDataFeed, DataFeed] = {
    MarketDataFeed.IEX: DataFeed.IEX,
    MarketDataFeed.DELAYED_SIP: DataFeed.SIP,
    MarketDataFeed.SIP: DataFeed.SIP,
}

_SNAPSHOT_FEED_MAP: dict[MarketDataFeed, DataFeed] = {
    MarketDataFeed.IEX: DataFeed.IEX,
    MarketDataFeed.DELAYED_SIP: DataFeed.DELAYED_SIP,
    MarketDataFeed.SIP: DataFeed.SIP,
}

# What a client call raises for a failed request: alpaca-py wraps HTTP error
# statuses in APIError and lets requests' transport errors through.
_REQUEST_ERRORS = (APIError, RequestsConnectionError, Timeout, InvalidHeader)


class _AlpacaBar(Protocol):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None


class _AlpacaTrade(Protocol):
    timestamp: datetime
    price: float


class _AlpacaQuote(Protocol):
    bid_price: float
    bid_size: float
    ask_price: float
    ask_size: float


class _AlpacaSnapshot(Protocol):
    @property
    def latest_trade(self) -> _AlpacaTrade | None: ...

    @property
    def latest_quote(self) -> _AlpacaQuote | None: ...

    @property
    def daily_bar(self) -> _AlpacaBar | None: ...

    @property
    def previous_daily_bar(self) -> _AlpacaBar | None: ...


class _BarSet(Protocol):
    @property
    def data(self) -> Mapping[str, Sequence[_AlpacaBar]]: ...


class _MarketDataClient(Protocol):
    def get_stock_bars(self, request_params: StockBarsRequest) -> _BarSet: ...

    def get_stock_snapshot(
        self,
        request_params: StockSnapshotRequest,
    ) -> Mapping[str, _AlpacaSnapshot]: ...


class _RequestRateLimiter:
    """
    Process-wide token bucket admitting each Alpaca HTTP attempt.

    Foreground callers need one token. A background caller (one passing its
    cancellation event) is admitted only while no foreground caller is waiting
    and more than background_reserve tokens remain, so history backfill never
    spends the burst a user action needs; it raises RequestCancelledError
    instead of being admitted once cancelled. Refill, eligibility, and
    deduction happen under one lock at the actual admission time.
    """

    def __init__(
        self,
        rate_per_second: float,
        capacity: int,
        background_reserve: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rate_per_second = rate_per_second
        self._capacity = float(capacity)
        self._background_reserve = background_reserve
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens = float(capacity)
        self._refilled_at = clock()
        self._foreground_waiting = 0

    def wait(self, cancellation: threading.Event | None = None) -> None:
        background = cancellation is not None
        needed = 1.0 + (self._background_reserve if background else 0)
        if not background:
            with self._lock:
                self._foreground_waiting += 1
        try:
            while True:
                with self._lock:
                    if cancellation is not None and cancellation.is_set():
                        raise RequestCancelledError
                    now = self._clock()
                    self._tokens = min(
                        self._capacity,
                        self._tokens
                        + (now - self._refilled_at) * self._rate_per_second,
                    )
                    self._refilled_at = now
                    if self._tokens >= needed and (
                        not background or self._foreground_waiting == 0
                    ):
                        self._tokens -= 1.0
                        return
                    delay = max(
                        (needed - self._tokens) / self._rate_per_second,
                        0.0,
                    )
                if background:
                    delay = min(delay, _BACKGROUND_POLL_SECONDS) or (
                        _BACKGROUND_POLL_SECONDS
                    )
                self._sleep(delay)
        finally:
            if not background:
                with self._lock:
                    self._foreground_waiting -= 1


class _RetryStatusCounter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def record(self) -> int:
        with self._lock:
            self._count += 1
            return self._count


_REQUEST_LIMITER = _RequestRateLimiter(
    _REQUEST_RATE_PER_SECOND,
    _REQUEST_BURST,
    _BACKGROUND_RESERVE,
)
_RETRY_STATUS_COUNTER = _RetryStatusCounter()


class _DefaultTimeoutHTTPAdapter(HTTPAdapter):  # type: ignore[misc]
    def send(
        self,
        request: PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float, float] | None = None,
        verify: bool | str = True,
        cert: str | tuple[str, str] | None = None,
        proxies: Mapping[str, str] | None = None,
    ) -> Response:
        _REQUEST_LIMITER.wait(background_request_cancellation())
        response = cast(
            Response,
            super().send(
                request,
                stream=stream,
                timeout=_ALPACA_REQUEST_TIMEOUT if timeout is None else timeout,
                verify=verify,
                cert=cert,
                proxies=proxies,
            ),
        )
        if response.status_code in _RETRY_STATUS_CODES:
            count = _RETRY_STATUS_COUNTER.record()
            _LOGGER.warning(
                "Alpaca returned HTTP %d; alpaca-py waits and retries unless its "
                "retry limit is exhausted (%d retryable responses this session).",
                response.status_code,
                count,
            )
        return response


def _create_stock_historical_data_client(
    api_key_id: str,
    api_secret: str,
) -> _MarketDataClient:
    client = StockHistoricalDataClient(api_key_id, api_secret)
    session = cast(Session, client._session)
    session.mount("https://", _DefaultTimeoutHTTPAdapter())
    session.mount("http://", _DefaultTimeoutHTTPAdapter())
    return cast(_MarketDataClient, client)


class AlpacaProvider(DataProvider):
    def __init__(
        self,
        api_key_id: str,
        api_secret: str,
        feed: MarketDataFeed,
        client: _MarketDataClient | None = None,
        *,
        display_name: str = "Alpaca",
    ) -> None:
        # The connection's name, for the neutral errors this provider raises.
        self._display_name = display_name
        self._configured_feed = feed
        self._bar_feed = _BAR_FEED_MAP[feed]
        self._snapshot_feed = _SNAPSHOT_FEED_MAP[feed]
        # requests.Session is not thread-safe, so each thread that uses this
        # provider gets its own SDK client. The factory closure keeps the
        # credentials out of instance attributes.
        self._create_client: Callable[[], _MarketDataClient] = (
            (lambda: client)
            if client is not None
            else (
                lambda: _create_stock_historical_data_client(
                    api_key_id,
                    api_secret,
                )
            )
        )
        self._thread_clients = threading.local()
        self._client()

    def _client(self) -> _MarketDataClient:
        client = cast(
            _MarketDataClient | None,
            getattr(self._thread_clients, "client", None),
        )
        if client is None:
            client = self._create_client()
            self._thread_clients.client = client
        return client

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        alpaca_timeframe = _TIMEFRAME_MAP.get(timeframe)
        if alpaca_timeframe is None:
            raise UnsupportedTimeframeError(
                f"AlpacaProvider does not support {timeframe!r} natively. "
                f"Native timeframes: {self.native_timeframes()}"
            )

        normalized = symbol.strip().upper()
        request = StockBarsRequest(
            symbol_or_symbols=normalized,
            timeframe=alpaca_timeframe,
            start=start,
            end=end,
            adjustment=Adjustment.ALL,
            feed=self._bar_feed,
        )
        try:
            response = self._client().get_stock_bars(request)
        except _REQUEST_ERRORS as exc:
            _raise_request_error(exc, self._configured_feed, self._display_name)
        alpaca_bars = response.data.get(normalized, [])
        if timeframe.is_intraday:
            alpaca_bars = [
                bar for bar in alpaca_bars if _is_regular_session(bar.timestamp)
            ]
        return sorted((_to_bar(bar) for bar in alpaca_bars), key=lambda bar: bar.timestamp)

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        normalized = _unique_symbols(symbols)
        if not normalized:
            return {}
        snapshots = self._fetch_alpaca_snapshots(normalized)
        result: dict[str, MarketSnapshot] = {}
        for symbol in normalized:
            snapshot = snapshots.get(symbol)
            if snapshot is None:
                continue
            converted = _to_market_snapshot(symbol, snapshot)
            if converted is not None:
                result[symbol] = converted
        return result

    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        normalized = symbol.strip().upper()
        if not normalized:
            return None
        snapshot = self._fetch_alpaca_snapshots([normalized]).get(normalized)
        if snapshot is None:
            return None
        return _to_level1_quote(normalized, snapshot)

    def latest_history_end(
        self,
        timeframe: Timeframe,
        end: datetime,
    ) -> datetime:
        if self._configured_feed == MarketDataFeed.DELAYED_SIP:
            return end - _DELAYED_SIP_SAFE_LAG
        return end

    def history_page_span(self, timeframe: Timeframe) -> timedelta | None:
        return _INTRADAY_HISTORY_PAGE_SPAN if timeframe.is_intraday else None

    def native_timeframes(self) -> list[Timeframe]:
        return [
            Timeframe.MIN1,
            Timeframe.MIN5,
            Timeframe.MIN15,
            Timeframe.MIN30,
            Timeframe.DAILY,
            Timeframe.WEEKLY,
        ]

    def _fetch_alpaca_snapshots(
        self,
        symbols: list[str],
    ) -> Mapping[str, _AlpacaSnapshot]:
        request = StockSnapshotRequest(
            symbol_or_symbols=symbols,
            feed=self._snapshot_feed,
        )
        try:
            return self._client().get_stock_snapshot(request)
        except _REQUEST_ERRORS as exc:
            _raise_request_error(exc, self._configured_feed, self._display_name)


def _raise_request_error(
    error: Exception,
    feed: MarketDataFeed,
    display_name: str,
) -> Never:
    # Translates the library's failure into SimpleChart's neutral errors;
    # anything else is re-raised unchanged.
    if isinstance(error, InvalidHeader):
        raise ProviderCredentialsMalformedError(display_name) from error
    if isinstance(error, RequestsConnectionError | Timeout):
        raise ProviderUnreachableError(display_name) from error
    if isinstance(error, APIError):
        if _subscription_rejected(error):
            raise _entitlement_error(feed, display_name) from error
        status = http_status(error)
        if status in (401, 403):
            raise ProviderCredentialsRejectedError(display_name) from error
        if status == 429:
            raise ProviderRateLimitedError(display_name) from error
    raise error


def _subscription_rejected(error: APIError) -> bool:
    # APIError.code parses the response body, which need not be JSON.
    try:
        return bool(error.code == _SIP_ENTITLEMENT_ERROR_CODE)
    except (ValueError, KeyError, TypeError):
        return False


def _entitlement_error(
    feed: MarketDataFeed,
    display_name: str,
) -> MarketDataEntitlementError:
    if feed == MarketDataFeed.DELAYED_SIP:
        return MarketDataEntitlementError(
            "Alpaca rejected the delayed SIP request as too recent. "
            "Try the request again; if the problem persists, select IEX "
            "or check the market-data plan on the Alpaca account."
        )
    if feed == MarketDataFeed.SIP:
        return MarketDataEntitlementError(
            "Alpaca rejected real-time SIP data for this account. "
            "Select 15-minute delayed SIP or IEX, or add a real-time "
            "SIP subscription to the Alpaca account."
        )
    return MarketDataEntitlementError(
        f"Your {display_name} plan doesn't include this data."
    )


def _unique_symbols(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for symbol in symbols:
        value = symbol.strip().upper()
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized


def _to_bar(bar: _AlpacaBar) -> Bar:
    values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Alpaca returned a bar with non-finite OHLCV data.")
    if bar.vwap is not None and not math.isfinite(bar.vwap):
        raise ValueError("Alpaca returned a bar with non-finite VWAP data.")
    if bar.timestamp.tzinfo is None:
        raise ValueError("Alpaca returned a bar with a timezone-naive timestamp.")
    return Bar(
        timestamp=bar.timestamp.astimezone(UTC),
        open=float(bar.open),
        high=float(bar.high),
        low=float(bar.low),
        close=float(bar.close),
        volume=int(bar.volume),
        vwap=None if bar.vwap is None else float(bar.vwap),
    )


def _to_market_snapshot(
    symbol: str,
    snapshot: _AlpacaSnapshot,
) -> MarketSnapshot | None:
    last_price, timestamp = _last_price_and_timestamp(snapshot)
    previous_close = _bar_close(snapshot.previous_daily_bar)
    if last_price is None and previous_close is None:
        return None
    change, change_percent = _change_fields(last_price, previous_close)
    return MarketSnapshot(
        symbol=symbol,
        last_price=last_price,
        change=change,
        change_percent=change_percent,
        previous_close=previous_close,
        timestamp=timestamp,
    )


def _to_level1_quote(
    symbol: str,
    snapshot: _AlpacaSnapshot,
) -> Level1Quote | None:
    if (
        snapshot.latest_trade is None
        and snapshot.latest_quote is None
        and snapshot.daily_bar is None
        and snapshot.previous_daily_bar is None
    ):
        return None
    last_price, _timestamp = _last_price_and_timestamp(snapshot)
    previous_close = _bar_close(snapshot.previous_daily_bar)
    change, change_percent = _change_fields(last_price, previous_close)
    latest_quote = snapshot.latest_quote
    daily_bar = snapshot.daily_bar
    return Level1Quote(
        symbol=symbol,
        company_name=None,
        last_price=last_price,
        change=change,
        change_percent=change_percent,
        bid=None if latest_quote is None else float(latest_quote.bid_price),
        bid_size=None if latest_quote is None else int(latest_quote.bid_size),
        ask=None if latest_quote is None else float(latest_quote.ask_price),
        ask_size=None if latest_quote is None else int(latest_quote.ask_size),
        open=None if daily_bar is None else float(daily_bar.open),
        high=None if daily_bar is None else float(daily_bar.high),
        low=None if daily_bar is None else float(daily_bar.low),
        volume=None if daily_bar is None else int(daily_bar.volume),
        previous_close=previous_close,
    )


def _last_price_and_timestamp(
    snapshot: _AlpacaSnapshot,
) -> tuple[float | None, datetime | None]:
    if snapshot.latest_trade is not None:
        return (
            float(snapshot.latest_trade.price),
            snapshot.latest_trade.timestamp.astimezone(UTC),
        )
    if snapshot.daily_bar is not None:
        return (
            float(snapshot.daily_bar.close),
            snapshot.daily_bar.timestamp.astimezone(UTC),
        )
    return None, None


def _bar_close(bar: _AlpacaBar | None) -> float | None:
    return None if bar is None else float(bar.close)


def _change_fields(
    last_price: float | None,
    previous_close: float | None,
) -> tuple[float | None, float | None]:
    if last_price is None or previous_close is None:
        return None, None
    change = last_price - previous_close
    change_percent = None
    if previous_close != 0.0:
        change_percent = change / previous_close * 100.0
    return change, change_percent
