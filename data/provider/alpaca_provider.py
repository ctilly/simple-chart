from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
import math
from typing import Never, Protocol, cast
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame as AlpacaTimeFrame
from alpaca.data.timeframe import TimeFrameUnit

from data.models import Bar, Level1Quote, MarketSnapshot, Timeframe
from data.provider.base import (
    DataProvider,
    MarketDataEntitlementError,
    UnsupportedTimeframeError,
)
from data.provider.config import MarketDataFeed


_ET = ZoneInfo("America/New_York")
_SESSION_OPEN = time(9, 30)
_SESSION_CLOSE = time(16, 0)
_DELAYED_SIP_SAFE_LAG = timedelta(minutes=16)
_SIP_ENTITLEMENT_ERROR_CODE = 40310000

_TIMEFRAME_MAP: dict[Timeframe, AlpacaTimeFrame] = {
    Timeframe.MIN1: AlpacaTimeFrame.Minute,
    Timeframe.MIN5: AlpacaTimeFrame(5, TimeFrameUnit.Minute),
    Timeframe.MIN15: AlpacaTimeFrame(15, TimeFrameUnit.Minute),
    Timeframe.MIN30: AlpacaTimeFrame(30, TimeFrameUnit.Minute),
    Timeframe.DAILY: AlpacaTimeFrame.Day,
    Timeframe.WEEKLY: AlpacaTimeFrame.Week,
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


class AlpacaProvider(DataProvider):
    def __init__(
        self,
        api_key_id: str,
        api_secret: str,
        feed: MarketDataFeed,
        client: _MarketDataClient | None = None,
    ) -> None:
        self._configured_feed = feed
        self._bar_feed = _BAR_FEED_MAP[feed]
        self._snapshot_feed = _SNAPSHOT_FEED_MAP[feed]
        self._client = (
            client
            if client is not None
            else cast(
                _MarketDataClient,
                StockHistoricalDataClient(api_key_id, api_secret),
            )
        )

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
            response = self._client.get_stock_bars(request)
        except APIError as exc:
            _raise_market_data_error(exc, self._configured_feed)
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
            return self._client.get_stock_snapshot(request)
        except APIError as exc:
            _raise_market_data_error(exc, self._configured_feed)


def _raise_market_data_error(
    error: APIError,
    feed: MarketDataFeed,
) -> Never:
    subscription_rejected = error.code == _SIP_ENTITLEMENT_ERROR_CODE
    if feed == MarketDataFeed.DELAYED_SIP and subscription_rejected:
        raise MarketDataEntitlementError(
            "Alpaca rejected the delayed SIP request as too recent. "
            "Try the request again; if the problem persists, select IEX "
            "or check the market-data plan on the Alpaca account."
        ) from error
    if feed == MarketDataFeed.SIP and subscription_rejected:
        raise MarketDataEntitlementError(
            "Alpaca rejected real-time SIP data for this account. "
            "Select 15-minute delayed SIP or IEX, or add a real-time "
            "SIP subscription to the Alpaca account."
        ) from error
    raise error


def _unique_symbols(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for symbol in symbols:
        value = symbol.strip().upper()
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized


def _is_regular_session(timestamp: datetime) -> bool:
    local = timestamp.astimezone(_ET)
    return local.weekday() < 5 and _SESSION_OPEN <= local.time() < _SESSION_CLOSE


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
