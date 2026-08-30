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


_KEYRING_SERVICE = "simplechart.provider-credentials"


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


class CredentialStoreUnavailableError(RuntimeError):
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
            raise RuntimeError("Unable to read provider credentials from the OS keyring.") from exc
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
            raise RuntimeError("Unable to save provider credentials in the OS keyring.") from exc

    def delete(self, connection_id: str) -> None:
        try:
            existing = self._backend.get_password(_KEYRING_SERVICE, connection_id)
            if existing is not None:
                self._backend.delete_password(_KEYRING_SERVICE, connection_id)
        except Exception as exc:
            raise RuntimeError("Unable to delete provider credentials from the OS keyring.") from exc

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


def initialize_keyring_credential_store(
    backend: _PasswordBackend | None = None,
    importer: Callable[[str], object] = import_module,
) -> CredentialStoreAccess:
    if backend is None:
        try:
            backend = cast(_PasswordBackend, importer("keyring"))
        except (ImportError, ModuleNotFoundError):
            reason = "Required package 'keyring' is not installed."
            return CredentialStoreAccess(
                UnavailableCredentialStore(reason),
                False,
                reason,
            )

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
