import logging
import pkgutil
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from importlib import import_module
from itertools import chain, product
from types import MappingProxyType, ModuleType
from typing import TypeIs

from data.models import Timeframe
from data.provider.base import DataProvider, ProviderConfigurationError
from data.provider.config import ProviderConnection
from data.provider.credentials import (
    AVAILABILITY_PROBE_PREFIX,
    CredentialStore,
    CredentialStoreAccess,
    ProviderCredentials,
    validate_credentials,
)
from data.provider.spec import (
    Capability,
    ConnectionTemplate,
    CredentialField,
    NamespaceSpec,
    OptionField,
    ProviderSpec,
)


FALLBACK_PROVIDER = "yfinance"
_FIELD_KEY = re.compile(r"[a-z][a-z0-9_]*")
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderAvailability:
    available: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class ProviderRegistry:
    providers: Mapping[str, ProviderSpec]
    _connections: Mapping[str, tuple[ProviderSpec, ConnectionTemplate]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "providers", MappingProxyType(dict(self.providers)))
        object.__setattr__(self, "_connections", MappingProxyType(dict(self._connections)))

    def connection_template(self, connection_id: str) -> tuple[ProviderSpec, ConnectionTemplate]:
        registered = self._connections.get(connection_id)
        if registered is None:
            raise ProviderConfigurationError("The data connection is not registered.")
        return registered

    @property
    def connection_ids(self) -> tuple[str, ...]:
        return tuple(self._connections)

    @property
    def fallback_provider(self) -> ProviderSpec:
        return self.providers[FALLBACK_PROVIDER]

    @property
    def fallback_connection_id(self) -> str:
        return self.fallback_provider.connections[0].connection_id

    def namespace_label(self, namespace: str) -> str:
        return next(
            (
                entry.label for spec in self.providers.values() for entry in spec.namespaces
                if entry.namespace == namespace
            ),
            namespace,
        )

    @property
    def legacy_dividend_adjusted_namespaces(self) -> dict[str, tuple[Timeframe, ...] | None]:
        return {
            namespace.namespace: spec.legacy_dividend_adjusted_timeframes
            for spec in self.providers.values()
            if spec.legacy_dividend_adjusted_timeframes != ()
            for namespace in spec.namespaces
        }

    def editable_options(self, connection_id: str) -> tuple[OptionField, ...]:
        spec, template = self.connection_template(connection_id)
        fixed = dict(template.fixed_options)
        return tuple(
            field for field in spec.option_fields if field.user_editable and field.key not in fixed
        )

    def build_connection(
        self, connection_id: str, options: Mapping[str, str] | None = None,
    ) -> ProviderConnection:
        spec, template = self.connection_template(connection_id)
        fields = {field.key: field for field in spec.option_fields}
        fixed = dict(template.fixed_options)
        values = {field.key: field.default for field in spec.option_fields} | fixed
        if options is not None:
            for key, value in options.items():
                if (
                    key not in fields or value not in dict(fields[key].choices)
                    or (key in fixed and value != fixed[key])
                ):
                    raise ValueError("Invalid option for the data connection.")
                values[key] = value
        namespace = self.cache_namespace(connection_id, values)
        assert namespace is not None
        return ProviderConnection(
            connection_id, template.display_name, spec.name, tuple(values.items()), namespace,
        )

    def connection_variant(
        self, connection: ProviderConnection, namespace: NamespaceSpec,
    ) -> ProviderConnection | None:
        spec, template = self.connection_template(connection.connection_id)
        if namespace not in spec.namespaces:
            return None
        fixed = dict(template.fixed_options)
        if any(key in fixed and fixed[key] != value for key, value in namespace.options):
            return None
        return self.build_connection(
            connection.connection_id, dict(connection.options) | dict(namespace.options),
        )

    def credentials_for(
        self, connection_id: str, store: CredentialStore,
    ) -> ProviderCredentials | None:
        spec, template = self.connection_template(connection_id)
        if not spec.requires_credentials:
            return None
        credentials = store.read(connection_id, spec.credential_fields, template.display_name)
        if credentials is not None:
            validate_credentials(credentials, spec.credential_fields, template.display_name)
        return credentials

    def cache_namespace(
        self, connection_id: str, options: Mapping[str, str],
    ) -> str | None:
        registered = self._connections.get(connection_id)
        if registered is None:
            return None
        spec, _ = registered
        return next(
            (
                namespace.namespace for namespace in spec.namespaces
                if _matches_options(namespace, options)
            ),
            None,
        )

    def dependency_status(
        self, name: str, importer: Callable[[str], object] = import_module,
    ) -> ProviderAvailability:
        spec = self.providers.get(name)
        if spec is None:
            return ProviderAvailability(False, "The data provider is not registered.")
        for module_name, package_name in spec.required_modules:
            try:
                importer(module_name)
            except ImportError:
                return ProviderAvailability(
                    False,
                    f"Required package {package_name!r} is not installed.",
                )
        return ProviderAvailability(True, None)

    def provider_availability(
        self,
        name: str,
        credential_access: CredentialStoreAccess,
        importer: Callable[[str], object] = import_module,
    ) -> ProviderAvailability:
        status = self.dependency_status(name, importer)
        if (
            status.available
            and self.providers[name].requires_credentials
            and not credential_access.available
        ):
            return ProviderAvailability(False, credential_access.reason)
        return status

    def create_provider(
        self, connection: ProviderConnection, store: CredentialStore,
    ) -> DataProvider:
        spec = self.providers.get(connection.provider_name)
        if spec is None:
            available = ", ".join(sorted(self.providers))
            raise ProviderConfigurationError(
                f"Provider {connection.provider_name!r} is not available. "
                f"Available: {available}"
            )
        owner, _ = self.connection_template(connection.connection_id)
        if owner is not spec:
            raise ProviderConfigurationError("The data connection has a different provider.")

        status = self.dependency_status(spec.name)
        if not status.available:
            raise ProviderConfigurationError(status.reason)

        credentials = self.credentials_for(connection.connection_id, store)
        if spec.requires_credentials and credentials is None:
            raise ProviderConfigurationError(
                f"{connection.display_name} credentials are not configured."
            )
        return spec.factory(connection, credentials)


def _package_modules(package_name: str) -> Iterable[str]:
    package = import_module(package_name)
    return (
        info.name for info in pkgutil.iter_modules(package.__path__, f"{package_name}.")
        if info.ispkg and not info.name.rsplit(".", 1)[-1].startswith("_")
    )


def discover_providers(
    sources: str | Iterable[str | ModuleType],
    *additional_packages: str,
    notify: Callable[[str], None] | None = None,
) -> ProviderRegistry:
    modules = _package_modules(sources) if isinstance(sources, str) else sources
    all_modules = chain(modules, chain.from_iterable(map(_package_modules, additional_packages)))

    providers: dict[str, ProviderSpec] = {}
    connections: dict[str, tuple[ProviderSpec, ConnectionTemplate]] = {}
    namespaces: set[str] = set()
    for source in all_modules:
        name = source if isinstance(source, str) else source.__name__
        try:
            module = import_module(source) if isinstance(source, str) else source
            spec = _validate_spec(getattr(module, "PROVIDER", None))
            if (
                spec.name in providers
                or any(template.connection_id in connections for template in spec.connections)
                or any(namespace.namespace in namespaces for namespace in spec.namespaces)
            ):
                raise ValueError("Duplicate provider declaration.")
        except Exception:
            notice = f"Plugin '{name}' could not be loaded and was skipped."
            _LOGGER.warning("%s", notice)
            if notify is not None:
                notify(notice)
            continue

        providers[spec.name] = spec
        for template in spec.connections:
            connections[template.connection_id] = (spec, template)
        namespaces.update(namespace.namespace for namespace in spec.namespaces)

    fallback = providers.get(FALLBACK_PROVIDER)
    if (
        fallback is None
        or fallback.requires_credentials
        or fallback.capabilities != frozenset(Capability)
    ):
        raise ProviderConfigurationError("The fallback data provider is unavailable.")
    return ProviderRegistry(providers, connections)


def _text(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _string_pairs(value: object) -> TypeIs[tuple[tuple[str, str], ...]]:
    return type(value) is tuple and all(
        type(pair) is tuple and len(pair) == 2 and all(_text(item) for item in pair)
        for pair in value
    )


def _qualified(value: str, name: str, separator: str) -> bool:
    return _text(value) and (
        value == name or (value.startswith(name + separator) and len(value) > len(name) + 1)
    )


def _matches_options(namespace: NamespaceSpec, options: Mapping[str, str]) -> bool:
    return all(options.get(key) == value for key, value in namespace.options)


def _validate_spec(value: object) -> ProviderSpec:
    if (
        type(value) is not ProviderSpec
        or not _text(value.name)
        or not _text(value.display_name)
        or not callable(value.factory)
        or not callable(value.serves_symbol)
        or type(value.capabilities) is not frozenset
        or any(type(capability) is not Capability for capability in value.capabilities)
        or not _string_pairs(value.required_modules)
        or type(value.credential_fields) is not tuple
        or type(value.option_fields) is not tuple
        or type(value.connections) is not tuple
        or not value.connections
        or type(value.namespaces) is not tuple
        or not value.namespaces
    ):
        raise ValueError("Invalid provider declaration.")

    timeframes = value.legacy_dividend_adjusted_timeframes
    if (
        (timeframes != () and Capability.BARS not in value.capabilities)
        or (
            timeframes is not None
            and (type(timeframes) is not tuple or any(type(item) is not Timeframe for item in timeframes))
        )
    ):
        raise ValueError("Invalid legacy dividend-adjusted timeframes.")

    fields: set[str] = set()
    for field in value.credential_fields:
        if (
            type(field) is not CredentialField
            or not _text(field.label)
            or not _text(field.key)
            or not _FIELD_KEY.fullmatch(field.key)
            or field.key in fields
        ):
            raise ValueError("Invalid credential field.")
        fields.add(field.key)

    choices: dict[str, dict[str, str]] = {}
    for option in value.option_fields:
        if (
            type(option) is not OptionField
            or not _text(option.label)
            or not _text(option.key)
            or option.key in fields
            or type(option.user_editable) is not bool
            or not _string_pairs(option.choices)
            or not _text(option.default)
        ):
            raise ValueError("Invalid option field.")
        option_choices = dict(option.choices)
        if len(option_choices) != len(option.choices) or option.default not in option_choices:
            raise ValueError("Invalid option choices.")
        fields.add(option.key)
        choices[option.key] = option_choices

    namespace_ids: set[str] = set()
    for namespace in value.namespaces:
        if (
            type(namespace) is not NamespaceSpec
            or not _qualified(namespace.namespace, value.name, ":")
            or namespace.namespace in namespace_ids
            or not _text(namespace.label)
            or type(namespace.comparable) is not bool
            or not _valid_options(namespace.options, choices)
            or (len(value.namespaces) == 1 and namespace.options)
        ):
            raise ValueError("Invalid cache namespace.")
        namespace_ids.add(namespace.namespace)

    connection_ids: set[str] = set()
    for template in value.connections:
        if (
            type(template) is not ConnectionTemplate
            or not _qualified(template.connection_id, value.name, "-")
            or template.connection_id.startswith(AVAILABILITY_PROBE_PREFIX)
            or template.connection_id in connection_ids
            or not _text(template.display_name)
            or not _valid_options(template.fixed_options, choices)
        ):
            raise ValueError("Invalid connection template.")
        connection_ids.add(template.connection_id)
        fixed = dict(template.fixed_options)
        values = ((fixed[key],) if key in fixed else choices[key] for key in choices)
        for combination in product(*values):
            options = dict(zip(choices, combination, strict=True))
            if sum(_matches_options(namespace, options) for namespace in value.namespaces) != 1:
                raise ValueError("Connection options must select exactly one cache namespace.")
    return value


def _valid_options(
    options: tuple[tuple[str, str], ...], choices: Mapping[str, Mapping[str, str]],
) -> bool:
    return (
        _string_pairs(options)
        and len(dict(options)) == len(options)
        and all(key in choices and value in choices[key] for key, value in options)
    )
