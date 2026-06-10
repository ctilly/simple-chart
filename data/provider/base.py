"""
data/provider/base.py

Abstract base class for data providers.

Any data source (yfinance, Alpaca, or a third-party addition) implements
this interface. The rest of the app only ever talks to a DataProvider —
it has no knowledge of which provider is active or how it works internally.

A provider is responsible for:
  - Fetching OHLCV bars for a symbol, timeframe, and time range
  - Returning bars as a list[Bar] sorted oldest-first
  - Fetching current market snapshots for one or more symbols
  - Raising UnsupportedTimeframeError for timeframes it cannot supply natively

Non-native timeframes (39m, 65m) are NOT the provider's problem. The
aggregator layer handles those by requesting a smaller native timeframe
and resampling. The provider only needs to handle what it can fetch directly.
"""

from abc import ABC, abstractmethod
from datetime import datetime

from data.models import Bar, Level1Quote, MarketSnapshot, Timeframe


class UnsupportedTimeframeError(Exception):
    """
    Raised when a provider is asked for a timeframe it cannot supply.

    The aggregator decides whether to synthesize by pre-checking the
    provider's native_timeframes(), not by catching this error. It raises
    this itself when a requested timeframe is neither native nor synthesizable
    from an available base timeframe.
    """


class DataProvider(ABC):
    """
    Interface for OHLCV data sources.

    Implementations must be stateless with respect to symbols and timeframes
    — all context is passed per call. Connection setup (API keys, sessions)
    belongs in __init__.
    """

    @abstractmethod
    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        """
        Fetch OHLCV bars for symbol between start and end (both UTC-aware).

        Returns bars sorted oldest-first. May return an empty list if the
        symbol has no data in the requested range (e.g. a newly listed stock,
        or a range that falls entirely on non-trading days).

        Raises:
            UnsupportedTimeframeError: if this provider cannot supply the
                requested timeframe natively.
        """

    @abstractmethod
    def fetch_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        """
        Fetch current market snapshots for symbols.

        Returns a mapping keyed by normalized symbol. Missing symbols may be
        omitted when the provider has no current market data for them. A
        provider that cannot determine the current session's data for a
        symbol must omit it rather than return prior-session data as current.
        """

    @abstractmethod
    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        """
        Fetch Level 1 data (company name, last/change, bid/ask with sizes,
        session OHLV, previous close) for one symbol.

        Returns None when the provider has no quote data for the symbol.
        Individual fields the provider cannot supply are None on the quote.
        """

    @abstractmethod
    def native_timeframes(self) -> list[Timeframe]:
        """
        Return the timeframes this provider can fetch directly.

        Used by the aggregator to determine whether to fetch directly or
        to request a base timeframe and resample. For example, a provider
        that returns [MIN5, MIN15, MIN30, DAILY, WEEKLY] cannot supply
        MIN39 or MIN65 natively — those must be synthesized from MIN5 bars.
        """
