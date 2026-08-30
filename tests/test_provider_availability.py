import subprocess
import sys
from types import ModuleType

import pytest

from data.provider import (
    ProviderAvailability,
    provider_availability,
    provider_dependency_status,
)
from data.provider.credentials import (
    CredentialStoreAccess,
    CredentialStoreUnavailableError,
    ProviderCredentials,
    UnavailableCredentialStore,
    initialize_keyring_credential_store,
)


class _ProbeBackend:
    def __init__(self, fail_operation: str | None = None) -> None:
        self._fail_operation = fail_operation
        self.passwords: dict[tuple[str, str], str] = {}
        self.operations: list[str] = []

    def get_password(self, service_name: str, username: str) -> str | None:
        self.operations.append("get")
        if self._fail_operation == "get":
            raise RuntimeError("backend read failed")
        return self.passwords.get((service_name, username))

    def set_password(
        self,
        service_name: str,
        username: str,
        password: str,
    ) -> None:
        self.operations.append("set")
        if self._fail_operation == "set":
            raise RuntimeError("backend write failed")
        self.passwords[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.operations.append("delete")
        if self._fail_operation == "delete":
            raise RuntimeError("backend delete failed")
        self.passwords.pop((service_name, username), None)


def test_provider_registry_import_does_not_require_alpaca_package() -> None:
    script = """
import importlib.abc
import sys

class BlockAlpaca(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'alpaca' or fullname.startswith('alpaca.'):
            raise ModuleNotFoundError("alpaca blocked for test")
        return None

sys.meta_path.insert(0, BlockAlpaca())
import data.provider
print('provider registry imported')
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "provider registry imported"


def test_provider_dependency_status_identifies_missing_module() -> None:
    def importer(name: str) -> ModuleType:
        raise ModuleNotFoundError(name)

    status = provider_dependency_status("alpaca", importer)

    assert isinstance(status, ProviderAvailability)
    assert not status.available
    assert status.reason == "Required package 'alpaca-py' is not installed."


def test_provider_dependency_status_accepts_available_modules() -> None:
    imported: list[str] = []

    def importer(name: str) -> ModuleType:
        imported.append(name)
        return ModuleType(name)

    status = provider_dependency_status("alpaca", importer)

    assert status.available
    assert status.reason is None
    assert imported == ["alpaca"]


def test_provider_availability_requires_secure_storage_for_alpaca() -> None:
    reason = "The operating-system credential store is unavailable."
    access = CredentialStoreAccess(
        UnavailableCredentialStore(reason),
        False,
        reason,
    )

    status = provider_availability(
        "alpaca",
        access,
        lambda name: ModuleType(name),
    )

    assert not status.available
    assert status.reason == reason


def test_provider_availability_keeps_yahoo_without_secure_storage() -> None:
    reason = "The operating-system credential store is unavailable."
    access = CredentialStoreAccess(
        UnavailableCredentialStore(reason),
        False,
        reason,
    )

    status = provider_availability(
        "yfinance",
        access,
        lambda name: ModuleType(name),
    )

    assert status.available
    assert status.reason is None


def test_provider_availability_reports_missing_package_before_keyring() -> None:
    keyring_reason = "The operating-system credential store is unavailable."
    access = CredentialStoreAccess(
        UnavailableCredentialStore(keyring_reason),
        False,
        keyring_reason,
    )

    def importer(name: str) -> ModuleType:
        raise ModuleNotFoundError(name)

    status = provider_availability("alpaca", access, importer)

    assert not status.available
    assert status.reason == "Required package 'alpaca-py' is not installed."


def test_keyring_preflight_writes_reads_and_removes_probe() -> None:
    backend = _ProbeBackend()

    access = initialize_keyring_credential_store(backend)

    assert access.available
    assert access.reason is None
    assert backend.operations == ["set", "get", "delete"]
    assert backend.passwords == {}


@pytest.mark.parametrize("operation", ["set", "get", "delete"])
def test_keyring_preflight_failure_returns_non_storing_store(
    operation: str,
) -> None:
    backend = _ProbeBackend(operation)

    access = initialize_keyring_credential_store(backend)

    assert not access.available
    assert access.reason == "The operating-system credential store is unavailable."
    if operation == "get":
        assert backend.passwords == {}
    with pytest.raises(CredentialStoreUnavailableError, match="unavailable"):
        access.store.put(
            "alpaca-paper",
            ProviderCredentials("must-not-persist", "must-not-persist"),
        )
    assert "must-not-persist" not in repr(backend.passwords)


def test_missing_keyring_package_returns_non_storing_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def importer(name: str) -> ModuleType:
        raise ModuleNotFoundError(name)

    access = initialize_keyring_credential_store(importer=importer)

    assert not access.available
    assert access.reason == "Required package 'keyring' is not installed."
    with pytest.raises(CredentialStoreUnavailableError, match="keyring"):
        access.store.get("alpaca-paper")


def test_credential_secret_is_excluded_from_repr() -> None:
    credentials = ProviderCredentials("visible-key-id", "hidden-api-secret")

    assert "visible-key-id" not in repr(credentials)
    assert "hidden-api-secret" not in repr(credentials)
