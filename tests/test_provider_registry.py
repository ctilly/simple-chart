import importlib
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from types import ModuleType

import pytest

from data.provider.spec import CredentialField
from data.provider.registry import ProviderRegistry
from data.provider import ProviderConfigurationError
from data.provider.config import ProviderConnection
from data.provider.credentials import (
    CredentialStoreUnavailableError,
    KeyringCredentialStore,
    ProviderCredentials,
)
from test_provider_config import _FakeKeyring, _MemoryCredentialStore


def test_declaration_modules_can_be_imported_without_provider_packages() -> None:
    script = """
import importlib.abc
import sys

class BlockSDKs(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition('.')[0] in {'alpaca', 'yfinance'}:
            raise AssertionError(f'Unexpected dependency import: {fullname}')
        return None

sys.meta_path.insert(0, BlockSDKs())
from providers.alpaca import PROVIDER as alpaca
from providers.yahoo import PROVIDER as yahoo
assert alpaca.requires_credentials
assert not yahoo.requires_credentials
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_discovery_seals_registry_and_discards_failed_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from data.provider.registry import discover_providers
    from providers.yahoo import PROVIDER

    def broken_import() -> ModuleType:
        module = ModuleType("broken")
        module.PROVIDER = replace(PROVIDER, name="broken")
        raise RuntimeError("unlabelled-failure-sentinel")

    original_import = importlib.import_module

    def importer(name: str) -> ModuleType:
        return broken_import() if name == "broken" else original_import(name)

    monkeypatch.setattr("data.provider.registry.import_module", importer)
    notices: list[str] = []
    registry = discover_providers(
        ("broken", "providers.yahoo"), notify=notices.append,
    )

    assert tuple(registry.providers) == ("yfinance",)
    assert notices == ["Plugin 'broken' could not be loaded and was skipped."]
    with pytest.raises(TypeError):
        registry.providers["broken"] = PROVIDER  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        setattr(registry.providers["yfinance"], "display_name", "Changed")
    assert not hasattr(importlib.import_module("data.provider"), "register_provider")


@pytest.mark.parametrize(
    "violation",
    [
        "free_text", "mutable_choices", "duplicate_id", "foreign_id", "probe_id",
        "duplicate_namespace", "foreign_namespace", "missing_namespace",
        "ambiguous_namespace", "credential_option_overlap",
    ],
)
def test_discovery_skips_invalid_declarations(violation: str) -> None:
    from providers.alpaca import PROVIDER
    from data.provider.registry import discover_providers
    from data.provider.spec import OptionField

    invalid = ModuleType("invalid")
    paper, live = PROVIDER.connections
    iex, delayed, sip = PROVIDER.namespaces
    environment, feed = PROVIDER.option_fields
    changes = {
        "free_text": {"option_fields": (environment, replace(feed, choices=()))},
        "mutable_choices": {
            "option_fields": (environment, replace(feed, choices=list(feed.choices))),
        },
        "duplicate_id": {"connections": (paper, replace(live, connection_id=paper.connection_id))},
        "foreign_id": {"connections": (replace(paper, connection_id="yfinance"), live)},
        "probe_id": {
            "name": "availability",
            "connections": (replace(paper, connection_id="availability-probe-reserved"),),
        },
        "duplicate_namespace": {"namespaces": (iex, replace(delayed, namespace=iex.namespace), sip)},
        "foreign_namespace": {"namespaces": (replace(iex, namespace="yfinance"), delayed, sip)},
        "missing_namespace": {"namespaces": (iex, delayed)},
        "ambiguous_namespace": {"namespaces": (iex, delayed, replace(sip, options=()))},
        "credential_option_overlap": {
            "option_fields": (
                environment, feed, OptionField("api_secret", "Secret", (("a", "A"),), "a"),
            ),
        },
    }
    invalid.PROVIDER = replace(PROVIDER, **changes[violation])
    notices: list[str] = []

    registry = discover_providers(
        ("providers.yahoo", invalid), notify=notices.append,
    )

    assert tuple(registry.providers) == ("yfinance",)
    assert notices == ["Plugin 'invalid' could not be loaded and was skipped."]


def test_invalid_fallback_fails_with_fixed_error() -> None:
    from data.provider.registry import discover_providers
    from providers.yahoo import PROVIDER

    invalid = ModuleType("invalid")
    invalid.PROVIDER = replace(PROVIDER, capabilities=frozenset())
    with pytest.raises(ProviderConfigurationError) as raised:
        discover_providers((invalid,), notify=lambda _notice: None)
    assert str(raised.value) == "The fallback data provider is unavailable."


@pytest.mark.parametrize(
    ("connection_id", "provider_name"),
    [
        ("alpaca-unregistered", "alpaca"),
        ("yfinance", "alpaca"),
        ("alpaca-paper", "yfinance"),
        ("availability-probe-reserved", "alpaca"),
    ],
)
def test_create_provider_rejects_unowned_credentials(
    provider_registry: ProviderRegistry,
    connection_id: str, provider_name: str,
) -> None:
    from provider_support import alpaca_paper_connection

    class StoreMustNotBeRead(_MemoryCredentialStore):
        def get(self, connection_id: str) -> ProviderCredentials | None:
            pytest.fail("Read credentials before verifying connection ownership")

        def read(
            self,
            connection_id: str,
            fields: tuple[CredentialField, ...],
            display_name: str,
        ) -> ProviderCredentials | None:
            return self.get(connection_id)

    connection = replace(
        alpaca_paper_connection(),
        connection_id=connection_id,
        provider_name=provider_name,
    )
    with pytest.raises(ProviderConfigurationError):
        provider_registry.create_provider(connection, StoreMustNotBeRead())


def test_discovery_scopes_matching_credential_fields_to_each_provider() -> None:
    from providers.alpaca import PROVIDER
    from data.provider.base import DataProvider
    from data.provider.registry import discover_providers
    from data.provider.spec import ConnectionTemplate, CredentialField, NamespaceSpec
    from providers.yahoo.implementation import YFinanceProvider

    received: dict[str, ProviderCredentials | None] = {}

    def factory(
        connection: ProviderConnection, credentials: ProviderCredentials | None,
    ) -> DataProvider:
        received[connection.connection_id] = credentials
        return YFinanceProvider()

    modules = []
    for name in ("first", "second"):
        module = ModuleType(name)
        module.PROVIDER = replace(
            PROVIDER,
            name=name,
            display_name=name.title(),
            connections=(ConnectionTemplate(name, name.title()),),
            namespaces=(NamespaceSpec(name, name.title(), (), False),),
            credential_fields=(CredentialField("token", "Token"),),
            option_fields=(),
            required_modules=(),
            factory=factory,
        )
        modules.append(module)
    registry = discover_providers(("providers.yahoo", *modules))
    backend = _FakeKeyring()
    store = KeyringCredentialStore(backend, registry=registry)
    backend.set_password("simplechart.provider-credentials", "first", '{"token":"first-value"}')
    backend.set_password("simplechart.provider-credentials", "second", '{"token":"second-value"}')

    for name in ("first", "second"):
        registry.create_provider(registry.build_connection(name), store)
        credentials = received[name]
        assert credentials is not None
        assert credentials.get("token") == f"{name}-value"
        assert f"{name}-value" not in repr(credentials)


@pytest.mark.parametrize(
    "payload",
    [
        '{"api_key_id":"unlabelled-sentinel","api_secret":',
        '["unlabelled-sentinel"]',
        '{"api_key_id":"unlabelled-sentinel"}',
        '{"api_key_id":"unlabelled-sentinel","api_secret":"abc","extra":"abc"}',
        '{"api_key_id":"unlabelled-sentinel","api_secret":"abc","api_secret":"abc"}',
        '{"api_key_id":"unlabelled-sentinel","api_secret":12}',
        '{"api_key_id":"unlabelled-sentinel","api_secret":"  "}',
    ],
    ids=["malformed", "array", "missing", "extra", "duplicate", "non-string", "blank"],
)
def test_keyring_rejects_invalid_payloads_without_echoing_text(payload: str, provider_registry: ProviderRegistry) -> None:
    backend = _FakeKeyring()
    backend.set_password("simplechart.provider-credentials", "alpaca-paper", payload)
    with pytest.raises(CredentialStoreUnavailableError) as raised:
        KeyringCredentialStore(backend, registry=provider_registry).get("alpaca-paper")
    assert str(raised.value) == "Stored credentials for Alpaca Paper are invalid."
    assert "unlabelled-sentinel" not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_credential_wrapper_is_frozen_redacted_and_keyed() -> None:
    credentials = ProviderCredentials({"token": "unlabelled-sentinel"})
    assert credentials.get("token") == "unlabelled-sentinel"
    assert "unlabelled-sentinel" not in str(credentials)
    assert "unlabelled-sentinel" not in repr(credentials)
    assert credentials != ProviderCredentials({"token": "unlabelled-sentinel"})
    assert not hasattr(credentials, "items")
    with pytest.raises(TypeError):
        iter(credentials)  # type: ignore[call-overload]
    with pytest.raises(FrozenInstanceError):
        setattr(credentials, "token", "changed")
    with pytest.raises(KeyError) as raised:
        credentials.get("unlabelled-sentinel")
    assert "unlabelled-sentinel" not in str(raised.value)
