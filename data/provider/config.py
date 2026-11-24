from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderConnection:
    connection_id: str
    display_name: str
    provider_name: str
    options: tuple[tuple[str, str], ...]
    cache_namespace: str
