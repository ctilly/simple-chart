from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data.provider.base import DataProvider
    from data.provider.config import ProviderConnection
    from data.provider.credentials import ProviderCredentials


@dataclass(frozen=True, slots=True)
class CredentialField:
    key: str
    label: str


@dataclass(frozen=True, slots=True)
class OptionField:
    key: str
    label: str
    choices: tuple[tuple[str, str], ...]
    default: str
    user_editable: bool = True


@dataclass(frozen=True, slots=True)
class ConnectionTemplate:
    connection_id: str
    display_name: str
    fixed_options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class NamespaceSpec:
    namespace: str
    label: str
    options: tuple[tuple[str, str], ...]
    comparable: bool


class Capability(StrEnum):
    BARS = auto()
    SNAPSHOTS = auto()
    LEVEL1 = auto()
    COMPANY_NAME = auto()
    PROFILE = auto()
    NEWS = auto()
    SEARCH = auto()
    UPCOMING_EARNINGS = auto()
    PAST_EARNINGS = auto()
    DIVIDENDS = auto()


type ProviderFactory = Callable[[ProviderConnection, ProviderCredentials | None], DataProvider]


def _all_symbols(symbol: str) -> bool:
    return True


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    name: str
    display_name: str
    connections: tuple[ConnectionTemplate, ...]
    capabilities: frozenset[Capability]
    namespaces: tuple[NamespaceSpec, ...]
    factory: ProviderFactory
    credential_fields: tuple[CredentialField, ...] = ()
    option_fields: tuple[OptionField, ...] = ()
    required_modules: tuple[tuple[str, str], ...] = ()
    serves_symbol: Callable[[str], bool] = _all_symbols

    @property
    def requires_credentials(self) -> bool:
        return bool(self.credential_fields)
