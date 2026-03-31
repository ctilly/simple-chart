"""
data/models.py

Core data contracts for SimpleChart. Every other module imports from here.
These are plain dataclasses with no I/O, no business logic, and no external
dependencies beyond the standard library.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Timeframe(str, Enum):
    """
    Supported chart timeframes.

    Inherits from str so a Timeframe can be used anywhere a plain string is
    expected (e.g. as a SQLite column value or dict key) without explicit
    conversion.

    MIN5, MIN15, MIN30, MIN39, MIN65 are intraday bars.
    DAILY and WEEKLY are end-of-day bars.

    MIN39 and MIN65 are non-standard — no data provider supplies them natively.
    They are synthesized in data/aggregator.py by resampling smaller bars.
    """

    MIN1 = "1m"    # internal use only — base for MIN39 aggregation, not user-chartable
    MIN5 = "5m"
    MIN15 = "15m"
    MIN30 = "30m"
    MIN39 = "39m"
    MIN65 = "65m"
    DAILY = "daily"
    WEEKLY = "weekly"

    @property
    def is_intraday(self) -> bool:
        """True for any timeframe shorter than a full trading day."""
        return self in (
            Timeframe.MIN1,
            Timeframe.MIN5,
            Timeframe.MIN15,
            Timeframe.MIN30,
            Timeframe.MIN39,
            Timeframe.MIN65,
        )

    @property
    def minutes(self) -> int | None:
        """
        Bar duration in minutes. Returns None for DAILY and WEEKLY since those
        don't have a fixed minute count (trading day length varies slightly).
        """
        mapping: dict[Timeframe, int] = {
            Timeframe.MIN5: 5,
            Timeframe.MIN15: 15,
            Timeframe.MIN30: 30,
            Timeframe.MIN39: 39,
            Timeframe.MIN65: 65,
        }
        return mapping.get(self)


@dataclass(frozen=True)
class Bar:
    """
    A single OHLCV bar.

    frozen=True makes Bar immutable and hashable — safe to use as a dict key
    or in a set, and prevents accidental mutation after the data layer returns it.

    timestamp is always UTC. The chart layer is responsible for converting to
    the user's local time for display if needed.

    vwap is optional — not all providers supply it, and it is not the same as
    Anchored VWAP (which is computed by the indicator engine from OHLCV data).
    """

    timestamp: datetime   # UTC, timezone-aware
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float | None = None


@dataclass
class OHLCVSeries:
    """
    An ordered sequence of bars for a single symbol and timeframe.

    bars is sorted oldest-first. This matches the natural order for indicator
    computation (indicators read left to right) and for SQLite queries
    (ORDER BY timestamp ASC).

    loaded_range_start and loaded_range_end describe the calendar window that
    was requested from the data layer, NOT necessarily the timestamps of the
    first and last bar. There may be gaps (weekends, holidays, halted trading).

    The controller uses loaded_range_start to determine whether it needs to
    fetch older bars when the user pans left past the current window.
    """

    symbol: str
    timeframe: Timeframe
    bars: list[Bar] = field(default_factory=list)
    loaded_range_start: datetime | None = None   # UTC
    loaded_range_end: datetime | None = None     # UTC

    def __len__(self) -> int:
        return len(self.bars)

    def __bool__(self) -> bool:
        return len(self.bars) > 0

    @property
    def newest_bar(self) -> Bar | None:
        """The most recent bar, or None if the series is empty."""
        return self.bars[-1] if self.bars else None

    @property
    def oldest_bar(self) -> Bar | None:
        """The oldest bar, or None if the series is empty."""
        return self.bars[0] if self.bars else None


@dataclass
class AnchorRecord:
    """
    A persisted AVWAP anchor point.

    anchor_ts is a UTC Unix timestamp in milliseconds. Storing milliseconds
    (rather than seconds or a datetime string) keeps the SQLite column a plain
    integer, which is fast to index and compare, and avoids any timezone
    serialization ambiguity.

    Why UTC timestamps and not bar indexes?
    Bar indexes change every time the timeframe changes — a bar that is index
    500 on a 5m chart is a completely different index on a 65m chart. UTC
    timestamps are stable across all timeframes. When the chart needs to draw
    the AVWAP, it converts the timestamp to a bar index via bisect_left on the
    current bar array (see data/calendar.py).

    label and color are display properties. label defaults to the ISO date of
    the anchor so the legend is readable without configuration. color is stored
    as a hex string (e.g. "#00FF88").

    anchor_id is None for anchors that have not yet been written to SQLite.
    After INSERT, the cache layer sets anchor_id to the SQLite rowid.
    """

    symbol: str
    anchor_ts: int            # UTC milliseconds
    label: str
    color: str                # hex, e.g. "#00FF88"
    anchor_id: int | None = None   # None until persisted
