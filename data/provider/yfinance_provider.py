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
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from data.models import Bar, Timeframe
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
    return timestamp.to_pydatetime()


def _row_to_bar(ts: object, row: object) -> Bar:
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


def _row_is_valid(row: object) -> bool:
    required = ("Open", "High", "Low", "Close", "Volume")
    return all(_is_finite_number(row[column]) for column in required)


def _is_finite_number(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _coerce_float(value: object) -> float:
    return float(value)


def _coerce_volume(value: object) -> int:
    return int(float(value))
