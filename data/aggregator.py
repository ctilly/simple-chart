"""
data/aggregator.py

Synthesizes non-native timeframes by resampling smaller bars.

The two timeframes that no provider supplies natively are MIN39 and MIN65:
  - MIN65 is synthesized from MIN5  (65 / 5  = 13 bars per period, exact)
  - MIN39 is synthesized from MIN1  (39 / 1  = 39 bars per period, exact)

All other timeframes are passed through to the provider unchanged.

The Aggregator wraps a DataProvider and exposes the same fetch_bars()
interface, so the cache/controller layer never needs to know whether a
timeframe is native or synthesized.

Grouping strategy:
  Bars are grouped into periods anchored to the NYSE session open (9:30 AM ET).
  Period 0 starts at 9:30, period 1 starts at 9:30 + period_minutes, etc.
  The grouping key is (date, period_index) so periods reset each trading day.
  OHLCV is then reduced across each group in the standard way:
    open   = first bar's open
    high   = max of all highs
    low    = min of all lows
    close  = last bar's close
    volume = sum of all volumes
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from data.models import Bar, Timeframe
from data.provider.base import DataProvider, UnsupportedTimeframeError


_ET = ZoneInfo("America/New_York")

# Ordered list of base timeframes to try for each non-native timeframe.
# The aggregator picks the first one the active provider supports.
# The base must divide evenly into the target period — only mathematically
# valid options are listed.
#
# MIN65: prefer MIN5 (13 bars/period) over MIN1 (65 bars/period). Fewer
#        base bars to fetch and process for the same output.
# MIN39: 39 is not evenly divisible by 5, so MIN5-based synthesis produces
#        bars of approximately 35–40 minutes (7–8 base bars per group) rather
#        than exactly 39 minutes. In practice the grouping key
#        (minutes_elapsed // 39) still yields exactly 10 bars per trading day,
#        which is correct. MIN5 is preferred because yfinance supplies 60 days
#        of 5m history vs only 7 days of 1m history — vastly more useful.
#        MIN1 is kept as a fallback in case the provider doesn't support MIN5.
_SYNTHESIS_BASES: dict[Timeframe, list[Timeframe]] = {
    Timeframe.MIN39: [Timeframe.MIN5, Timeframe.MIN1],
    Timeframe.MIN65: [Timeframe.MIN5, Timeframe.MIN1],
}


class Aggregator:
    """
    Wraps a DataProvider and transparently synthesizes non-native timeframes.

    For native timeframes, calls the provider directly.
    For MIN39 and MIN65, fetches a smaller base timeframe and resamples.
    """

    def __init__(self, provider: DataProvider) -> None:
        self._provider = provider

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        """
        Fetch bars for the given timeframe, synthesizing if necessary.

        For native timeframes the provider is called directly.
        For MIN39/MIN65 the provider is asked for base-timeframe bars and
        the result is resampled into the target timeframe.
        """
        if timeframe in self._provider.native_timeframes():
            return self._provider.fetch_bars(symbol, timeframe, start, end)

        if timeframe not in _SYNTHESIS_BASES:
            raise UnsupportedTimeframeError(
                f"No synthesis strategy defined for {timeframe!r}."
            )

        base_tf = _select_base(timeframe, self._provider.native_timeframes())
        base_bars = self._provider.fetch_bars(symbol, base_tf, start, end)
        return _resample(base_bars, timeframe)


# ------------------------------------------------------------------
# Base timeframe selection
# ------------------------------------------------------------------

def _select_base(target: Timeframe, available: list[Timeframe]) -> Timeframe:
    """
    Pick the best available base timeframe for synthesizing target.

    Iterates the priority list for target and returns the first entry that
    the provider supports. Raises UnsupportedTimeframeError if none match.
    """
    for candidate in _SYNTHESIS_BASES[target]:
        if candidate in available:
            return candidate
    raise UnsupportedTimeframeError(
        f"Cannot synthesize {target!r}. Required base timeframes "
        f"{_SYNTHESIS_BASES[target]} are not supported by the active provider."
    )


# ------------------------------------------------------------------
# Resampling
# ------------------------------------------------------------------

def _resample(bars: list[Bar], target: Timeframe) -> list[Bar]:
    """
    Resample a list of bars into a larger timeframe.

    bars must be sorted oldest-first and all belong to the same symbol.
    Returns a new list of bars sorted oldest-first.
    """
    period_minutes = target.minutes
    if period_minutes is None:
        raise ValueError(f"Cannot resample into non-intraday timeframe {target!r}.")

    # Group bars by (date, period_index). Using a list of (key, bar) pairs
    # rather than a dict preserves insertion order (Python 3.7+ guarantee)
    # and makes the reduce step straightforward.
    groups: dict[tuple[datetime, int], list[Bar]] = {}
    for bar in bars:
        key = _group_key(bar.timestamp, period_minutes)
        if key not in groups:
            groups[key] = []
        groups[key].append(bar)

    return [_reduce_group(group_bars) for group_bars in groups.values()]


def _group_key(ts: datetime, period_minutes: int) -> tuple[datetime, int]:
    """
    Return a (date, period_index) key for a bar timestamp.

    Converts the UTC timestamp to ET, then computes how many minutes have
    elapsed since the session open (9:30 AM ET). Integer division by
    period_minutes gives the period index for that day.

    Using ET for the calculation ensures period boundaries align with the
    NYSE session regardless of DST shifts.
    """
    et = ts.astimezone(_ET)
    session_open = et.replace(hour=9, minute=30, second=0, microsecond=0)
    minutes_elapsed = int((et - session_open).total_seconds() / 60)
    period_index = minutes_elapsed // period_minutes
    return (et.date(), period_index)


def _reduce_group(bars: list[Bar]) -> Bar:
    """
    Combine a group of bars into a single bar using standard OHLCV rules:
      open   = first bar's open
      high   = max of all highs
      low    = min of all lows
      close  = last bar's close
      volume = sum of all volumes
      timestamp = first bar's timestamp (the open of the period)
    """
    return Bar(
        timestamp=bars[0].timestamp,
        open=bars[0].open,
        high=max(b.high for b in bars),
        low=min(b.low for b in bars),
        close=bars[-1].close,
        volume=sum(b.volume for b in bars),
        vwap=None,  # VWAP cannot be meaningfully reconstructed from OHLCV
    )
