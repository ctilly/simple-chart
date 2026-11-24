from data.provider.base import DataProvider
from data.provider.config import ProviderConnection
from data.provider.credentials import ProviderCredentials
from data.provider.spec import (
    Capability,
    ConnectionTemplate,
    CredentialField,
    NamespaceSpec,
    OptionField,
    ProviderSpec,
)


def _create_provider(
    connection: ProviderConnection, credentials: ProviderCredentials | None,
) -> DataProvider:
    from .implementation import FakeSource

    assert credentials is not None
    return FakeSource(credentials, dict(connection.options)["tier"])


PROVIDER: ProviderSpec = ProviderSpec(
    name="fakesource",
    display_name="Fake Source",
    connections=(ConnectionTemplate("fakesource-work", "Fake Source Work"),),
    capabilities=frozenset({Capability.BARS, Capability.PROFILE}),
    namespaces=(NamespaceSpec("fakesource", "Fake Source", (), True),),
    factory=_create_provider,
    credential_fields=(CredentialField("token", "Access token"),),
    option_fields=(OptionField("tier", "Access tier", (("basic", "Basic"), ("pro", "Pro")), "basic"),),
)
