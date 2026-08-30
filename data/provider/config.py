from dataclasses import dataclass
from enum import StrEnum


YFINANCE_CONNECTION_ID = "yfinance"
ALPACA_PAPER_CONNECTION_ID = "alpaca-paper"
ALPACA_LIVE_CONNECTION_ID = "alpaca-live"
FIXED_CONNECTION_IDS = (
    YFINANCE_CONNECTION_ID,
    ALPACA_PAPER_CONNECTION_ID,
    ALPACA_LIVE_CONNECTION_ID,
)


class ConnectionEnvironment(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class MarketDataFeed(StrEnum):
    IEX = "iex"
    DELAYED_SIP = "delayed_sip"
    SIP = "sip"

    @property
    def display_name(self) -> str:
        if self == MarketDataFeed.IEX:
            return "IEX (real-time)"
        if self == MarketDataFeed.DELAYED_SIP:
            return "SIP (15-minute delayed)"
        return "SIP (real-time, subscription required)"


@dataclass(frozen=True)
class ProviderConnection:
    connection_id: str
    display_name: str
    provider_name: str
    environment: ConnectionEnvironment | None = None
    feed: MarketDataFeed | None = None

    @property
    def cache_namespace(self) -> str:
        if self.provider_name == "yfinance":
            return "yfinance"
        if self.provider_name == "alpaca" and self.feed is not None:
            return f"alpaca:{self.feed.value}"
        raise ValueError(
            f"Connection {self.connection_id!r} has no market-data cache namespace."
        )


def yfinance_connection() -> ProviderConnection:
    return ProviderConnection(
        connection_id=YFINANCE_CONNECTION_ID,
        display_name="Yahoo Finance",
        provider_name="yfinance",
    )


def alpaca_paper_connection(
    feed: MarketDataFeed = MarketDataFeed.IEX,
) -> ProviderConnection:
    return ProviderConnection(
        connection_id=ALPACA_PAPER_CONNECTION_ID,
        display_name="Alpaca Paper",
        provider_name="alpaca",
        environment=ConnectionEnvironment.PAPER,
        feed=feed,
    )


def alpaca_live_connection(
    feed: MarketDataFeed = MarketDataFeed.IEX,
) -> ProviderConnection:
    return ProviderConnection(
        connection_id=ALPACA_LIVE_CONNECTION_ID,
        display_name="Alpaca Live",
        provider_name="alpaca",
        environment=ConnectionEnvironment.LIVE,
        feed=feed,
    )


def fixed_connections() -> tuple[ProviderConnection, ...]:
    return (
        yfinance_connection(),
        alpaca_paper_connection(),
        alpaca_live_connection(),
    )
