"""OS-keyring-only provider credential storage.

Production code must not add a file, database, environment-variable, command-
line, or in-memory fallback. See docs/credential-security.md.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from secrets import token_urlsafe
from typing import Protocol, cast

from data.errors import UserFacingError


_KEYRING_SERVICE = "simplechart.provider-credentials"

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


@dataclass(frozen=True, repr=False)
class ProviderCredentials:
    api_key_id: str
    api_secret: str


class CredentialStore(Protocol):
    def get(self, connection_id: str) -> ProviderCredentials | None: ...

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


class CredentialStoreUnavailableError(UserFacingError, RuntimeError):
    pass


class UnavailableCredentialStore:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def get(self, connection_id: str) -> ProviderCredentials | None:
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
    def __init__(self, backend: _PasswordBackend | None = None) -> None:
        if backend is None:
            import keyring

            backend = cast(_PasswordBackend, keyring)
        self._backend = backend

    def get(self, connection_id: str) -> ProviderCredentials | None:
        try:
            payload = self._backend.get_password(_KEYRING_SERVICE, connection_id)
        except Exception as exc:
            raise CredentialStoreUnavailableError(
                "Unable to read provider credentials from the OS keyring."
            ) from exc
        if payload is None:
            return None
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("Stored provider credentials are invalid.")
        api_key_id = decoded.get("api_key_id")
        api_secret = decoded.get("api_secret")
        if not isinstance(api_key_id, str) or not isinstance(api_secret, str):
            raise ValueError("Stored provider credentials are invalid.")
        return ProviderCredentials(api_key_id=api_key_id, api_secret=api_secret)

    def put(
        self,
        connection_id: str,
        credentials: ProviderCredentials,
    ) -> None:
        payload = json.dumps(
            {
                "api_key_id": credentials.api_key_id,
                "api_secret": credentials.api_secret,
            }
        )
        try:
            self._backend.set_password(_KEYRING_SERVICE, connection_id, payload)
        except Exception as exc:
            raise CredentialStoreUnavailableError(
                "Unable to save provider credentials in the OS keyring."
            ) from exc

    def delete(self, connection_id: str) -> None:
        try:
            existing = self._backend.get_password(_KEYRING_SERVICE, connection_id)
            if existing is not None:
                self._backend.delete_password(_KEYRING_SERVICE, connection_id)
        except Exception as exc:
            raise CredentialStoreUnavailableError(
                "Unable to delete provider credentials from the OS keyring."
            ) from exc

    def verify_available(self) -> None:
        probe_id = f"availability-probe-{token_urlsafe(12)}"
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

    store = KeyringCredentialStore(backend)
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
