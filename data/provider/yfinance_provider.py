"""
data/provider/yfinance_provider.py

yfinance implementation of DataProvider. Used during development and testing
in place of a paid Alpaca subscription.

yfinance is an unofficial Yahoo Finance client. It is well-maintained and
widely used, but being unofficial means it can break if Yahoo changes its
API. For development purposes this is acceptable.

Intraday history limits (Yahoo Finance):
  - 5m, 15m, 30m: ~60 days back
  - These limits are fine for development; production will use Alpaca.

Note: the file is named yfinance_provider.py (not yfinance.py) to avoid
shadowing the yfinance package on import.
"""

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, cast

import pandas as pd
import yfinance as yf

from data.models import Bar, Level1Quote, MarketSnapshot, Timeframe
from data.provider.base import DataProvider, UnsupportedTimeframeError


# Mapping from our Timeframe enum to yfinance interval strings.
# MIN39 and MIN65 are intentionally absent — they are not native to Yahoo
# Finance and must be synthesized by the aggregator from MIN5 bars.
_TIMEFRAME_MAP: dict[Timeframe, str] = {
    Timeframe.MIN1:   "1m",    # ~7-day history limit on Yahoo Finance
    Timeframe.MIN5:   "5m",
    Timeframe.MIN15:  "15m",
    Timeframe.MIN30:  "30m",
    Timeframe.DAILY:  "1d",
    Timeframe.WEEKLY: "1wk",
}


class YFinanceProvider(DataProvider):
    """
    Fetches OHLCV data from Yahoo Finance via the yfinance library.

    No authentication required — yfinance needs no API key.
    """

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        """
        Fetch bars from Yahoo Finance for the given symbol and range.

        start and end must be UTC-aware datetimes. yfinance accepts
        datetime objects directly and handles timezone conversion internally.

        Returns bars sorted oldest-first with UTC-aware timestamps.
        An empty list is returned if Yahoo has no data for the range
        (e.g. the symbol didn't exist yet, or the range is all holidays).
        """
        interval = _TIMEFRAME_MAP.get(timeframe)
        if interval is None:
            raise UnsupportedTimeframeError(
                f"YFinanceProvider does not support {timeframe!r} natively. "
                f"Native timeframes: {self.native_timeframes()}"
            )

        ticker = yf.Ticker(symbol)
        df = ticker.history(
            interval=interval,
            start=start,
            end=end,
            auto_adjust=True,   # adjusts OHLC for splits and dividends
            prepost=False,      # regular session only, no pre/post market bars
        )

        if df.empty:
            return []

        return _rows_to_bars(df)

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        requested = _unique_symbols(symbols)
        if not requested:
            return {}

        snapshots = _fetch_batch_daily_snapshots(requested)
        missing = [symbol for symbol in requested if symbol not in snapshots]
        snapshots.update(_fetch_fast_info_snapshots(missing))
        return snapshots

    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        normalized = symbol.strip().upper()
        if not normalized:
            return None
        try:
            info = yf.Ticker(normalized).info
        except Exception:
            return None
        return _level1_from_info(normalized, info)

    def native_timeframes(self) -> list[Timeframe]:
        return [
            Timeframe.MIN1,
            Timeframe.MIN5,
            Timeframe.MIN15,
            Timeframe.MIN30,
            Timeframe.DAILY,
            Timeframe.WEEKLY,
        ]


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------


def _unique_symbols(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for symbol in symbols:
        normalized = symbol.strip().upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


def _fetch_batch_daily_snapshots(symbols: list[str]) -> dict[str, MarketSnapshot]:
    try:
        df = yf.download(
            symbols,
            period="5d",
            interval="1d",
            auto_adjust=True,
            group_by="ticker",
            threads=True,
            progress=False,
            timeout=5,
        )
    except Exception:
        return {}
    if df is None or df.empty:
        return {}
    return _snapshots_from_downloaded_history(symbols, df)


def _snapshots_from_downloaded_history(
    symbols: list[str],
    df: pd.DataFrame,
) -> dict[str, MarketSnapshot]:
    snapshots: dict[str, MarketSnapshot] = {}
    if isinstance(df.columns, pd.MultiIndex):
        available_symbols = set(str(value).upper() for value in df.columns.get_level_values(0))
        for symbol in symbols:
            if symbol not in available_symbols:
                continue
            snapshot = _snapshot_from_daily_history(symbol, df[symbol])
            if snapshot is not None:
                snapshots[symbol] = snapshot
        return snapshots

    if len(symbols) == 1:
        snapshot = _snapshot_from_daily_history(symbols[0], df)
        if snapshot is not None:
            snapshots[symbols[0]] = snapshot
    return snapshots


def _fetch_fast_info_snapshots(symbols: list[str]) -> dict[str, MarketSnapshot]:
    if not symbols:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as executor:
        results = list(executor.map(_fetch_fast_info_snapshot, symbols))
    return {
        symbol: snapshot
        for symbol, snapshot in zip(symbols, results)
        if snapshot is not None
    }


def _fetch_fast_info_snapshot(symbol: str) -> MarketSnapshot | None:
    ticker = yf.Ticker(symbol)
    try:
        fast_info = ticker.get_fast_info()
        snapshot = _snapshot_from_fast_info(symbol, fast_info)
    except Exception:
        snapshot = None
    if snapshot is None or snapshot.change_percent is None:
        try:
            history_snapshot = _snapshot_from_daily_history(
                symbol,
                ticker.history(period="5d", interval="1d", auto_adjust=True),
            )
        except Exception:
            history_snapshot = None
        if history_snapshot is not None:
            snapshot = history_snapshot
    return snapshot


def _snapshot_from_fast_info(symbol: str, fast_info: Any) -> MarketSnapshot | None:
    last_price = _optional_finite_float(fast_info.get("lastPrice"))
    previous_close = _optional_finite_float(fast_info.get("regularMarketPreviousClose"))
    if previous_close is None:
        previous_close = _optional_finite_float(fast_info.get("previousClose"))
    return _snapshot_from_price_fields(symbol, last_price, previous_close, None)


def _snapshot_from_price_fields(
    symbol: str,
    last_price: float | None,
    previous_close: float | None,
    timestamp: datetime | None,
) -> MarketSnapshot | None:
    if last_price is None and previous_close is None:
        return None
    change: float | None = None
    change_percent: float | None = None
    if last_price is not None and previous_close is not None:
        change = last_price - previous_close
        if previous_close != 0:
            change_percent = change / previous_close * 100
    return MarketSnapshot(
        symbol=symbol.upper(),
        last_price=last_price,
        change=change,
        change_percent=change_percent,
        previous_close=previous_close,
        timestamp=timestamp,
    )


def _snapshot_from_daily_history(symbol: str, df: pd.DataFrame) -> MarketSnapshot | None:
    if df.empty or "Close" not in df.columns:
        return None

    # Yahoo sometimes returns the newest session as a row of NaNs. Computing
    # the change from the prior two sessions would silently report yesterday's
    # move as today's, so omit the snapshot and let the quote-endpoint
    # fallback supply it.
    if _optional_finite_float(df.iloc[-1]["Close"]) is None:
        return None

    close_rows: list[tuple[object, float]] = []
    for timestamp, row in df.iterrows():
        close = _optional_finite_float(row["Close"])
        if close is not None:
            close_rows.append((timestamp, close))
    if len(close_rows) < 2:
        return None

    latest_timestamp, latest_close = close_rows[-1]
    _, previous_close = close_rows[-2]
    return _snapshot_from_price_fields(
        symbol,
        latest_close,
        previous_close,
        _normalize_timestamp(latest_timestamp),
    )


def _snapshot_from_pricing_data(data: dict[str, Any]) -> MarketSnapshot | None:
    raw_symbol = data.get("id")
    if not isinstance(raw_symbol, str) or not raw_symbol:
        return None
    return MarketSnapshot(
        symbol=raw_symbol.upper(),
        last_price=_optional_finite_float(data.get("price")),
        change=_optional_finite_float(data.get("change")),
        change_percent=_optional_finite_float(data.get("change_percent")),
        previous_close=_optional_finite_float(data.get("previous_close")),
        timestamp=_optional_timestamp(data.get("time")),
    )


def _level1_from_info(symbol: str, info: dict[str, Any]) -> Level1Quote | None:
    def number(*keys: str) -> float | None:
        for key in keys:
            value = _optional_finite_float(info.get(key))
            if value is not None:
                return value
        return None

    def whole(*keys: str) -> int | None:
        value = number(*keys)
        return int(value) if value is not None else None

    last_price = number("regularMarketPrice", "currentPrice")
    previous_close = number("regularMarketPreviousClose", "previousClose")
    if last_price is None and previous_close is None:
        return None

    change = number("regularMarketChange")
    change_percent = number("regularMarketChangePercent")
    if change is None and last_price is not None and previous_close is not None:
        change = last_price - previous_close
        if previous_close != 0:
            change_percent = change / previous_close * 100

    name = info.get("longName") or info.get("shortName")
    return Level1Quote(
        symbol=symbol,
        company_name=name if isinstance(name, str) else None,
        last_price=last_price,
        change=change,
        change_percent=change_percent,
        bid=number("bid"),
        bid_size=whole("bidSize"),
        ask=number("ask"),
        ask_size=whole("askSize"),
        open=number("regularMarketOpen", "open"),
        high=number("regularMarketDayHigh", "dayHigh"),
        low=number("regularMarketDayLow", "dayLow"),
        volume=whole("regularMarketVolume", "volume"),
        previous_close=previous_close,
    )


def _optional_finite_float(value: object) -> float | None:
    if not isinstance(value, str | int | float):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _optional_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str | int | float):
        return None
    try:
        timestamp = float(value)
    except ValueError:
        return None
    if not math.isfinite(timestamp):
        return None
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def _rows_to_bars(df: pd.DataFrame) -> list[Bar]:
    """
    Convert a yfinance DataFrame into Bars with validation.

    Providers sometimes return a trailing partial row with missing OHLCV
    values for the current in-progress period. We drop invalid rows only
    from the tail of the result. Invalid rows in the middle indicate a
    corrupted fetch and must fail loudly rather than silently distorting
    the series.
    """
    rows_with_validity = [
        (_normalize_timestamp(ts), row, _row_is_valid(row))
        for ts, row in df.iterrows()
    ]

    last_valid_index = -1
    for index, (_, _, is_valid) in enumerate(rows_with_validity):
        if is_valid:
            last_valid_index = index

    if last_valid_index == -1:
        return []

    first_invalid_in_kept: tuple[int, datetime] | None = next(
        (
            (index, timestamp)
            for index, (timestamp, _, is_valid) in enumerate(
                rows_with_validity[: last_valid_index + 1]
            )
            if not is_valid
        ),
        None,
    )
    if first_invalid_in_kept is not None:
        _, timestamp = first_invalid_in_kept
        raise ValueError(
            "Provider returned an invalid interior bar for "
            f"{timestamp.isoformat()}."
        )

    return [
        _row_to_bar(timestamp, row)
        for timestamp, row, _ in rows_with_validity[: last_valid_index + 1]
    ]


def _normalize_timestamp(ts: object) -> datetime:
    """
    Normalize a yfinance row index value to a UTC-aware datetime.

    yfinance returns timestamps as pandas Timestamps. For intraday data
    they are timezone-aware (America/New_York); for daily/weekly they may
    be timezone-naive dates. Both are normalized to UTC-aware datetimes.
    """
    timestamp = pd.Timestamp(ts)
    if timestamp.tzinfo is None:
        # Daily/weekly bars from yfinance are sometimes timezone-naive.
        # Treat them as midnight UTC.
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return cast(datetime, timestamp.to_pydatetime())


def _row_to_bar(ts: object, row: Any) -> Bar:
    """Convert one validated yfinance DataFrame row to a Bar."""
    return Bar(
        timestamp=_normalize_timestamp(ts),
        open=_coerce_float(row["Open"]),
        high=_coerce_float(row["High"]),
        low=_coerce_float(row["Low"]),
        close=_coerce_float(row["Close"]),
        volume=_coerce_volume(row["Volume"]),
        vwap=None,  # Yahoo Finance does not provide VWAP
    )


def _row_is_valid(row: Any) -> bool:
    required = ("Open", "High", "Low", "Close", "Volume")
    return all(_is_finite_number(row[column]) for column in required)


def _is_finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _coerce_float(value: Any) -> float:
    return float(value)


def _coerce_volume(value: Any) -> int:
    return int(float(value))
