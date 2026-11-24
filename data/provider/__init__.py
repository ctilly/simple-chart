"""Compatibility imports for application code using provider types."""

from data.provider.base import (
    DataProvider,
    ProviderConfigurationError as ProviderConfigurationError,
)
from data.provider.config import ProviderConnection
from data.provider.credentials import CredentialStore
from data.provider.registry import (
    ProviderAvailability as ProviderAvailability,
    ProviderRegistry,
)


def create_provider(
    connection: ProviderConnection,
    credential_store: CredentialStore,
    *,
    registry: ProviderRegistry | None = None,
) -> DataProvider:
    # Compare Bars still has this default until P5. MainWindow supplies its registry's factory.
    if registry is None:
        raise ProviderConfigurationError("A provider registry is required.")
    return registry.create_provider(connection, credential_store)
