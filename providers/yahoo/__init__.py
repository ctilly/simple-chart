from data.provider.base import DataProvider
from data.provider.config import ProviderConnection
from data.provider.credentials import ProviderCredentials
from data.provider.spec import Capability, ConnectionTemplate, NamespaceSpec, ProviderSpec


_PROVIDER_NAME = "yfinance"


def _create_provider(
    connection: ProviderConnection, credentials: ProviderCredentials | None,
) -> DataProvider:
    from providers.yahoo.implementation import YFinanceProvider

    return YFinanceProvider(display_name=connection.display_name)


PROVIDER: ProviderSpec = ProviderSpec(
    name=_PROVIDER_NAME,
    display_name="Yahoo Finance",
    connections=(ConnectionTemplate(_PROVIDER_NAME, "Yahoo Finance"),),
    capabilities=frozenset((
        Capability.BARS,
        Capability.SNAPSHOTS,
        Capability.LEVEL1,
        Capability.COMPANY_NAME,
        Capability.PROFILE,
        Capability.NEWS,
        Capability.SEARCH,
        Capability.UPCOMING_EARNINGS,
        Capability.PAST_EARNINGS,
        Capability.DIVIDENDS,
    )),
    namespaces=(NamespaceSpec(_PROVIDER_NAME, "Yahoo Finance", (), True),),
    factory=_create_provider,
    required_modules=(("yfinance", "yfinance"),),
)
