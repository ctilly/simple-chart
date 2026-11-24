from pathlib import Path
from typing import TYPE_CHECKING

from simplechart.output_safety import install
from simplechart.plugins import _USER_PLUGIN_DIR

if TYPE_CHECKING:
    from data.provider.registry import ProviderRegistry


def bootstrap(
    *,
    provider_package: str = "providers",
    user_plugin_dir: Path | None = _USER_PLUGIN_DIR,
) -> "ProviderRegistry":
    install()

    from data.provider.registry import discover_providers
    from simplechart.extensions._guard import queue_notice

    registry = discover_providers(provider_package, notify=queue_notice)

    from simplechart.plugins import load_plugins

    load_plugins(user_plugin_dir=user_plugin_dir)
    return registry
