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

        return [_row_to_bar(ts, row) for ts, row in df.iterrows()]

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

def _row_to_bar(ts: object, row: object) -> Bar:
    """
    Convert a single yfinance DataFrame row to a Bar.

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

    return Bar(
        timestamp=timestamp.to_pydatetime(),
        open=float(row["Open"]),
        high=float(row["High"]),
        low=float(row["Low"]),
        close=float(row["Close"]),
        volume=int(row["Volume"]),
        vwap=None,  # Yahoo Finance does not provide VWAP
    )
