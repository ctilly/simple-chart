"""
data/provider/__init__.py

Provider factory. Resolves persisted connection metadata and credentials into
the configured DataProvider used by the application.

Usage:
    from data.provider import create_provider
    provider = create_provider(connection, credential_store)

Provider implementations register a factory that accepts the selected
connection and its credentials. This keeps credential lookup in one place.
"""

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module

from data.provider.base import DataProvider
from data.provider.config import ProviderConnection
from data.provider.credentials import (
    CredentialStore,
    CredentialStoreAccess,
    ProviderCredentials,
)


class ProviderConfigurationError(RuntimeError):
    pass


ProviderFactory = Callable[
    [ProviderConnection, ProviderCredentials | None],
    DataProvider,
]


@dataclass(frozen=True)
class ProviderRequirements:
    required_modules: tuple[tuple[str, str], ...] = ()
    requires_credentials: bool = False


@dataclass(frozen=True)
class ProviderAvailability:
    available: bool
    reason: str | None


def _create_yfinance_provider(
    connection: ProviderConnection,
    credentials: ProviderCredentials | None,
) -> DataProvider:
    from data.provider.yfinance_provider import YFinanceProvider

    return YFinanceProvider()


def _create_alpaca_provider(
    connection: ProviderConnection,
    credentials: ProviderCredentials | None,
) -> DataProvider:
    from data.provider.alpaca_provider import AlpacaProvider

    if (
        credentials is None
        or connection.feed is None
        or connection.environment is None
    ):
        raise ProviderConfigurationError(
            f"{connection.display_name} is not fully configured."
        )
    return AlpacaProvider(
        credentials.api_key_id,
        credentials.api_secret,
        connection.feed,
    )


_registry: dict[str, ProviderFactory] = {
    "yfinance": _create_yfinance_provider,
    "alpaca": _create_alpaca_provider,
}

_requirements: dict[str, ProviderRequirements] = {
    "yfinance": ProviderRequirements((("yfinance", "yfinance"),)),
    "alpaca": ProviderRequirements(
        (("alpaca", "alpaca-py"),),
        requires_credentials=True,
    ),
}


def register_provider(
    name: str,
    factory: ProviderFactory,
    *,
    required_modules: tuple[tuple[str, str], ...] = (),
    requires_credentials: bool = False,
) -> None:
    """Register a configured DataProvider factory under the given name."""
    _registry[name] = factory
    _requirements[name] = ProviderRequirements(
        required_modules,
        requires_credentials,
    )


def provider_requirements(name: str) -> ProviderRequirements:
    return _requirements.get(name, ProviderRequirements())


def provider_dependency_status(
    name: str,
    importer: Callable[[str], object] = import_module,
) -> ProviderAvailability:
    for module_name, package_name in provider_requirements(name).required_modules:
        try:
            importer(module_name)
        except (ImportError, ModuleNotFoundError):
            return ProviderAvailability(
                False,
                f"Required package {package_name!r} is not installed.",
            )
    return ProviderAvailability(True, None)


def provider_availability(
    name: str,
    credential_access: CredentialStoreAccess,
    importer: Callable[[str], object] = import_module,
) -> ProviderAvailability:
    dependency_status = provider_dependency_status(name, importer)
    if not dependency_status.available:
        return ProviderAvailability(False, dependency_status.reason)
    if (
        provider_requirements(name).requires_credentials
        and not credential_access.available
    ):
        return ProviderAvailability(False, credential_access.reason)
    return ProviderAvailability(True, None)


def create_provider(
    connection: ProviderConnection,
    credential_store: CredentialStore,
) -> DataProvider:
    factory = _registry.get(connection.provider_name)
    if factory is None:
        available = ", ".join(sorted(_registry))
        raise ProviderConfigurationError(
            f"Provider {connection.provider_name!r} is not available. "
            f"Available: {available}"
        )

    dependency_status = provider_dependency_status(connection.provider_name)
    if not dependency_status.available:
        raise ProviderConfigurationError(dependency_status.reason)

    credentials: ProviderCredentials | None = None
    if provider_requirements(connection.provider_name).requires_credentials:
        credentials = credential_store.get(connection.connection_id)
        if credentials is None:
            raise ProviderConfigurationError(
                f"{connection.display_name} credentials are not configured."
            )
    return factory(connection, credentials)
