"""
data/calendar.py

Trading calendar utilities for SimpleChart.

Two responsibilities:
  1. Convert a day-count to a bar-count for a given timeframe. This is what
     makes day-based moving averages work — a "50-day SMA" means the same
     price value on every timeframe because it always covers the same number
     of trading days, just expressed as a different bar count.

  2. Resolve an AVWAP anchor timestamp to a bar index in the current loaded
     series. Anchors are stored as UTC timestamps; the indicator kernel needs
     an integer index into the bar array.

No external dependencies — only the standard library.
"""

import bisect
import math

from data.models import Timeframe


# NYSE regular session: 9:30 AM – 4:00 PM ET = 390 minutes.
# Bar count for one complete trading day at each intraday timeframe.
# DAILY = 1 bar per day, WEEKLY = 1 bar per week (handled separately below).
_BARS_PER_TRADING_DAY: dict[Timeframe, int] = {
    Timeframe.MIN1:   390 // 1,    # 390 — internal use, base for MIN39 aggregation
    Timeframe.MIN5:   390 // 5,    # 78
    Timeframe.MIN15:  390 // 15,   # 26
    Timeframe.MIN30:  390 // 30,   # 13
    Timeframe.MIN39:  390 // 39,   # 10
    Timeframe.MIN65:  390 // 65,   # 6
    Timeframe.DAILY:  1,
}

_TRADING_DAYS_PER_WEEK = 5


def bars_for_n_days(n_days: int, timeframe: Timeframe) -> int:
    """
    Return the number of bars that span n_days trading days at a given
    timeframe.

    For intraday and daily timeframes this is a straight multiplication:
        bars = n_days * bars_per_trading_day

    For weekly, one bar represents a full trading week (~5 days), so we
    divide and round up to include any partial week at the boundary:
        bars = ceil(n_days / 5)

    Examples:
        bars_for_n_days(50, Timeframe.DAILY)   ->   50
        bars_for_n_days(50, Timeframe.MIN5)    -> 3900
        bars_for_n_days(50, Timeframe.MIN65)   ->  300
        bars_for_n_days(50, Timeframe.WEEKLY)  ->   10
    """
    if n_days <= 0:
        raise ValueError(f"n_days must be positive, got {n_days}")

    if timeframe == Timeframe.WEEKLY:
        return math.ceil(n_days / _TRADING_DAYS_PER_WEEK)

    return n_days * _BARS_PER_TRADING_DAY[timeframe]


def timestamp_ms_to_bar_index(
    timestamp_ms: int,
    bar_timestamps_ms: list[int],
) -> int:
    """
    Return the bar index corresponding to a UTC millisecond timestamp.

    bar_timestamps_ms is the list of bar open timestamps for the currently
    loaded series, in UTC milliseconds, sorted oldest-first.

    Uses bisect_right: returns the index of the last bar whose timestamp is
    <= anchor_ts_ms. If the anchor lands inside a coarser bar (for example a
    5m anchor viewed on the daily chart), that containing bar is the AVWAP
    start index.

    Edge cases:
      - Anchor predates all loaded bars: returns 0. The controller is
        responsible for fetching far enough back, but 0 is a safe fallback.
      - Anchor is far beyond the newest loaded bar, when the previous interval
        gives enough information to identify that, returns len(bar_timestamps_ms).
        The chart layer treats this anchor as not yet drawable.
    """
    if not bar_timestamps_ms:
        return 0
    idx = bisect.bisect_right(bar_timestamps_ms, timestamp_ms) - 1
    if idx < 0:
        return 0
    if idx == len(bar_timestamps_ms) - 1 and timestamp_ms > bar_timestamps_ms[-1]:
        if len(bar_timestamps_ms) == 1:
            return idx
        last_step = bar_timestamps_ms[-1] - bar_timestamps_ms[-2]
        if last_step > 0 and timestamp_ms >= bar_timestamps_ms[-1] + last_step:
            return len(bar_timestamps_ms)
    return idx
