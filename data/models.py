"""
data/models.py

Core data contracts for SimpleChart. Every other module imports from here.
These are plain dataclasses with no I/O, no business logic, and no external
dependencies beyond the standard library.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


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
    """

    symbol: str
    timeframe: Timeframe
    bars: list[Bar] = field(default_factory=list)
    def __len__(self) -> int:
        return len(self.bars)

    def __bool__(self) -> bool:
        return len(self.bars) > 0


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    last_price: float | None
    change: float | None
    change_percent: float | None
    previous_close: float | None
    timestamp: datetime | None


@dataclass(frozen=True)
class ChartExtensionStoreRecord:
    record_id: int
    store_key: str
    symbol: str
    sort_key: int
    payload: dict[str, Any]
