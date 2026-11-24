"""OS-keyring-only provider credential storage.

Production code must not add a file, database, environment-variable, command-
line, or in-memory fallback. See docs/credential-security.md.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from secrets import token_urlsafe
from types import MappingProxyType
from typing import Protocol, cast

from data.provider.base import ProviderConfigurationError
from data.provider.spec import ConnectionTemplate, CredentialField, ProviderSpec


_KEYRING_SERVICE = "simplechart.provider-credentials"
AVAILABILITY_PROBE_PREFIX = "availability-probe-"

# keyring's operating-system backends, by fully qualified class name. Any
# other backend (keyrings.alt's file stores, null, fail, or one named in a
# keyringrc.cfg or PYTHON_KEYRING_BACKEND) is rejected before it is written to.
_OS_KEYRING_BACKENDS = frozenset(
    {
        "keyring.backends.SecretService.Keyring",
        "keyring.backends.libsecret.Keyring",
        "keyring.backends.kwallet.DBusKeyring",
        "keyring.backends.kwallet.DBusKeyringKWallet4",
        "keyring.backends.macOS.Keyring",
        "keyring.backends.Windows.WinVaultKeyring",
    }
)
_KEYRING_CHAINER = "keyring.backends.chainer.ChainerBackend"


@dataclass(frozen=True, repr=False, eq=False, init=False)
class ProviderCredentials:
    __slots__ = ("_values",)
    _values: Mapping[str, str]

    def __init__(self, values: Mapping[str, str], /) -> None:
        object.__setattr__(self, "_values", MappingProxyType(dict(values)))

    def __repr__(self) -> str:
        return "ProviderCredentials(<redacted>)"

    def get(self, key: str) -> str:
        value = self._values.get(key)
        if value is None:
            raise KeyError("Unknown credential field.")
        return value


class CredentialStore(Protocol):
    def get(self, connection_id: str) -> ProviderCredentials | None: ...

    def read(
        self,
        connection_id: str,
        fields: tuple[CredentialField, ...],
        display_name: str,
    ) -> ProviderCredentials | None:
        """Read with the registered schema, using decode_credentials for serialized entries."""
        ...

    def put(
        self,
        connection_id: str,
        credentials: ProviderCredentials,
    ) -> None: ...

    def delete(self, connection_id: str) -> None: ...


class _PasswordBackend(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(
        self,
        service_name: str,
        username: str,
        password: str,
    ) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class _KeyringModule(Protocol):
    def get_keyring(self) -> object: ...


class _CredentialRegistry(Protocol):
    def connection_template(self, connection_id: str) -> tuple[ProviderSpec, ConnectionTemplate]: ...


class CredentialStoreUnavailableError(ProviderConfigurationError):
    pass


class UnavailableCredentialStore:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def get(self, connection_id: str) -> ProviderCredentials | None:
        raise CredentialStoreUnavailableError(self._reason)

    def read(
        self,
        connection_id: str,
        fields: tuple[CredentialField, ...],
        display_name: str,
    ) -> ProviderCredentials | None:
        raise CredentialStoreUnavailableError(self._reason)

    def put(
        self,
        connection_id: str,
        credentials: ProviderCredentials,
    ) -> None:
        raise CredentialStoreUnavailableError(self._reason)

    def delete(self, connection_id: str) -> None:
        raise CredentialStoreUnavailableError(self._reason)


@dataclass(frozen=True)
class CredentialStoreAccess:
    store: CredentialStore
    available: bool
    reason: str | None


class KeyringCredentialStore:
    def __init__(self, backend: _PasswordBackend, *, registry: _CredentialRegistry) -> None:
        self._backend = backend
        self._registry = registry

    def get(self, connection_id: str) -> ProviderCredentials | None:
        spec, template = self._registry.connection_template(connection_id)
        if not spec.requires_credentials:
            return None
        return self._load(connection_id, spec.credential_fields, template.display_name)

    def read(
        self,
        connection_id: str,
        fields: tuple[CredentialField, ...],
        display_name: str,
    ) -> ProviderCredentials | None:
        spec, template = self._registry.connection_template(connection_id)
        if not spec.requires_credentials:
            return None
        if fields != spec.credential_fields or display_name != template.display_name:
            raise CredentialStoreUnavailableError("The credential fields do not match the data connection.")
        return self._load(connection_id, fields, display_name)

    def _load(
        self,
        connection_id: str,
        fields: tuple[CredentialField, ...],
        display_name: str,
    ) -> ProviderCredentials | None:
        try:
            payload = self._backend.get_password(_KEYRING_SERVICE, connection_id)
        except Exception as exc:
            raise CredentialStoreUnavailableError(
                "Unable to read provider credentials from the OS keyring."
            ) from exc
        if payload is None:
            return None
        return decode_credentials(payload, fields, display_name)

    def put(
        self,
        connection_id: str,
        credentials: ProviderCredentials,
    ) -> None:
        spec, template = self._registry.connection_template(connection_id)
        if not spec.requires_credentials or not _matches_fields(
            credentials._values, spec.credential_fields,
        ):
            raise CredentialStoreUnavailableError(
                f"Credentials for {template.display_name} are invalid."
            )
        payload = json.dumps(dict(credentials._values))
        try:
            self._backend.set_password(_KEYRING_SERVICE, connection_id, payload)
        except Exception as exc:
            raise CredentialStoreUnavailableError(
                "Unable to save provider credentials in the OS keyring."
            ) from exc

    def delete(self, connection_id: str) -> None:
        spec, _ = self._registry.connection_template(connection_id)
        if not spec.requires_credentials:
            return
        try:
            existing = self._backend.get_password(_KEYRING_SERVICE, connection_id)
            if existing is not None:
                self._backend.delete_password(_KEYRING_SERVICE, connection_id)
        except Exception as exc:
            raise CredentialStoreUnavailableError(
                "Unable to delete provider credentials from the OS keyring."
            ) from exc

    def verify_available(self) -> None:
        probe_id = f"{AVAILABILITY_PROBE_PREFIX}{token_urlsafe(12)}"
        probe_value = token_urlsafe(24)
        stored = False
        failed = False
        try:
            self._backend.set_password(_KEYRING_SERVICE, probe_id, probe_value)
            stored = True
            failed = (
                self._backend.get_password(_KEYRING_SERVICE, probe_id)
                != probe_value
            )
        except Exception:
            failed = True
        finally:
            if stored:
                try:
                    self._backend.delete_password(_KEYRING_SERVICE, probe_id)
                except Exception:
                    failed = True
        if failed:
            raise CredentialStoreUnavailableError(
                "The operating-system credential store is unavailable."
            )


def decode_credentials(
    payload: str,
    fields: tuple[CredentialField, ...],
    display_name: str,
) -> ProviderCredentials:
    try:
        decoded: object = json.loads(payload, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        decoded = None
    if not isinstance(decoded, dict) or not _matches_fields(decoded, fields):
        raise _invalid_stored_credentials(display_name)
    return ProviderCredentials(decoded)


def validate_credentials(
    credentials: ProviderCredentials,
    fields: tuple[CredentialField, ...],
    display_name: str,
) -> None:
    if not _matches_fields(credentials._values, fields):
        raise _invalid_stored_credentials(display_name)


def _invalid_stored_credentials(display_name: str) -> CredentialStoreUnavailableError:
    return CredentialStoreUnavailableError(f"Stored credentials for {display_name} are invalid.")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate credential fields.")
    return result


def _matches_fields(
    values: Mapping[str, object], fields: tuple[CredentialField, ...],
) -> bool:
    return values.keys() == {field.key for field in fields} and all(
        isinstance(value, str) and bool(value.strip()) for value in values.values()
    )


def _backend_class_name(backend: object) -> str:
    cls = type(backend)
    return f"{cls.__module__}.{cls.__qualname__}"


def _is_os_keyring(backend: object) -> bool:
    if _backend_class_name(backend) == _KEYRING_CHAINER:
        # The chainer writes to the first backend that accepts the write, so
        # every chained backend must be an OS store.
        chained: list[object] = list(getattr(backend, "backends"))
        return bool(chained) and all(
            _backend_class_name(item) in _OS_KEYRING_BACKENDS for item in chained
        )
    return _backend_class_name(backend) in _OS_KEYRING_BACKENDS


def initialize_keyring_credential_store(
    backend: _PasswordBackend | None = None,
    importer: Callable[[str], object] = import_module,
    *,
    registry: _CredentialRegistry,
) -> CredentialStoreAccess:
    if backend is None:
        try:
            keyring_module = cast(_KeyringModule, importer("keyring"))
        except (ImportError, ModuleNotFoundError):
            reason = "Required package 'keyring' is not installed."
            return CredentialStoreAccess(
                UnavailableCredentialStore(reason),
                False,
                reason,
            )
        try:
            # Raises when PYTHON_KEYRING_BACKEND or keyringrc.cfg names a
            # backend that cannot load.
            active = keyring_module.get_keyring()
            is_os_keyring = _is_os_keyring(active)
        except Exception:
            reason = "The operating-system credential store is unavailable."
            return CredentialStoreAccess(
                UnavailableCredentialStore(reason),
                False,
                reason,
            )
        if not is_os_keyring:
            reason = (
                "The configured keyring backend is not an operating-system "
                "credential store."
            )
            return CredentialStoreAccess(
                UnavailableCredentialStore(reason),
                False,
                reason,
            )
        # Write through the backend that was checked; keyring's module-level
        # functions re-resolve the active backend on every call.
        backend = cast(_PasswordBackend, active)

    store = KeyringCredentialStore(backend, registry=registry)
    try:
        store.verify_available()
    except CredentialStoreUnavailableError:
        reason = "The operating-system credential store is unavailable."
        return CredentialStoreAccess(
            UnavailableCredentialStore(reason),
            False,
            reason,
        )
    return CredentialStoreAccess(store, True, None)
