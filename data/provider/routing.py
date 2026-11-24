from dataclasses import dataclass

from data.aggregator import Aggregator
from data.provider.config import ProviderConnection
from data.provider.registry import ProviderRegistry
from data.provider.spec import Capability, ProviderSpec


@dataclass(frozen=True, eq=False)
class DataRoute:
    aggregator: Aggregator
    connection: ProviderConnection
    registry: ProviderRegistry

    @property
    def spec(self) -> ProviderSpec:
        return self.registry.providers[self.connection.provider_name]

    @property
    def cache_namespace(self) -> str:
        return self.connection.cache_namespace

    @property
    def display_name(self) -> str:
        values = dict(self.connection.options)
        labels = (
            dict(field.choices)[values[field.key]]
            for field in self.registry.editable_options(self.connection.connection_id)
        )
        return " / ".join((self.connection.display_name, *labels))


@dataclass
class CapabilityRouter:
    selected_route: DataRoute
    fallback_route: DataRoute

    def route(self, capability: Capability, symbol: str) -> DataRoute:
        selected = self.selected_route
        if capability in selected.spec.capabilities and selected.spec.serves_symbol(symbol):
            return selected
        return self.fallback_route
