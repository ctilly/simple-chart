from enum import StrEnum

from data.provider.base import DataProvider, ProviderConfigurationError
from data.provider.config import ProviderConnection
from data.provider.credentials import ProviderCredentials
from data.provider.spec import (
    Capability,
    ConnectionTemplate,
    CredentialField,
    NamespaceSpec,
    OptionField,
    ProviderSpec,
)


ALPACA_PAPER_CONNECTION_ID = "alpaca-paper"
ALPACA_LIVE_CONNECTION_ID = "alpaca-live"


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


def _create_provider(
    connection: ProviderConnection, credentials: ProviderCredentials | None,
) -> DataProvider:
    from providers.alpaca.implementation import AlpacaProvider

    options = dict(connection.options)
    feed = options.get("feed")
    if credentials is None or feed is None or "environment" not in options:
        raise ProviderConfigurationError(f"{connection.display_name} is not fully configured.")
    return AlpacaProvider(
        credentials.get("api_key_id"),
        credentials.get("api_secret"),
        MarketDataFeed(feed),
        display_name=connection.display_name,
    )


def _serves_symbol(symbol: str) -> bool:
    return not symbol.strip().upper().endswith("=F")


PROVIDER: ProviderSpec = ProviderSpec(
    name="alpaca",
    display_name="Alpaca",
    connections=(
        ConnectionTemplate(
            ALPACA_PAPER_CONNECTION_ID, "Alpaca Paper",
            (("environment", ConnectionEnvironment.PAPER.value),),
        ),
        ConnectionTemplate(
            ALPACA_LIVE_CONNECTION_ID, "Alpaca Live",
            (("environment", ConnectionEnvironment.LIVE.value),),
        ),
    ),
    capabilities=frozenset((Capability.BARS, Capability.SNAPSHOTS, Capability.LEVEL1)),
    namespaces=(
        NamespaceSpec("alpaca:iex", "Alpaca / IEX", (("feed", "iex"),), True),
        NamespaceSpec(
            "alpaca:delayed_sip", "Alpaca / SIP (15-minute delayed)",
            (("feed", "delayed_sip"),), True,
        ),
        NamespaceSpec("alpaca:sip", "Alpaca / SIP (real-time)", (("feed", "sip"),), False),
    ),
    factory=_create_provider,
    credential_fields=(
        CredentialField("api_key_id", "API key ID"),
        CredentialField("api_secret", "API secret"),
    ),
    option_fields=(
        OptionField(
            "environment", "Environment",
            tuple((environment.value, environment.value.title()) for environment in ConnectionEnvironment),
            ConnectionEnvironment.PAPER.value,
            user_editable=False,
        ),
        OptionField(
            "feed", "Market data feed",
            tuple((feed.value, feed.display_name) for feed in MarketDataFeed),
            MarketDataFeed.IEX.value,
        ),
    ),
    required_modules=(("alpaca", "alpaca-py"),),
    serves_symbol=_serves_symbol,
)
