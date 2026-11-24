import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from data.provider.credentials import CredentialStoreUnavailableError, ProviderCredentials
from data.provider.registry import discover_providers
from data.provider.spec import CredentialField


_ROOT = Path(__file__).resolve().parents[1]


def test_main_seals_providers_before_loading_plugins(tmp_path: Path) -> None:
    plugin_dir = tmp_path / ".simplechart" / "plugins"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "verify_startup.py").write_text(
        """
import __main__
import logging
from pathlib import Path
from PyQt6.QtGui import QIcon
from data.provider.registry import ProviderRegistry

assert logging.Logger.callHandlers.__module__ == "simplechart.output_safety"
assert not Path(__main__.db_path).parent.exists()
registry = __main__.discovered_registry
assert set(registry.providers) == {"alpaca", "yfinance"}
try:
    registry.providers["plugin"] = registry.providers["yfinance"]
except TypeError:
    pass
else:
    raise AssertionError("Plugins received an unsealed registry")

import app.controller

class VerifyWindow:
    def __init__(
        self, *, db_path: str, provider_connection_id: str | None,
        provider_registry: ProviderRegistry,
    ) -> None:
        assert provider_registry is registry
        assert db_path == __main__.db_path
        print("window received sealed registry", flush=True)

    def setWindowIcon(self, icon: QIcon) -> None:
        pass

    def show(self) -> None:
        raise SystemExit(0)

app.controller.MainWindow = VerifyWindow
""",
        encoding="utf-8",
    )
    script = """
import logging
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from types import ModuleType

import data.provider.registry as providers

db_path = sys.argv[-1]
original_discover = providers.discover_providers

def discover(
    sources: str | Iterable[str | ModuleType],
    *,
    notify: Callable[[str], None] | None = None,
) -> providers.ProviderRegistry:
    global discovered_registry
    assert logging.Logger.callHandlers.__module__ == "simplechart.output_safety"
    assert not Path(db_path).parent.exists()
    discovered_registry = original_discover(sources, notify=notify)
    return discovered_registry

providers.discover_providers = discover

from main import main

main()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, "--db", str(tmp_path / "db" / "chart.db")],
        cwd=_ROOT,
        env={**os.environ, "HOME": str(tmp_path), "QT_QPA_PLATFORM": "offscreen"},
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "window received sealed registry" in result.stdout, result.stderr
    assert result.returncode == 0, result.stderr


def test_discovery_finds_provider_declarations_in_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert set(discover_providers("providers").providers) == {"alpaca", "yfinance"}

    package = tmp_path / "fixture_sources"
    package.mkdir()
    (package / "__init__.py").touch()
    for name, source in (("fallback", "yahoo"), ("another_source", "alpaca")):
        child = package / name
        child.mkdir()
        (child / "__init__.py").write_text(
            f"from providers.{source} import PROVIDER\n", encoding="utf-8",
        )
    monkeypatch.syspath_prepend(str(tmp_path))

    assert set(discover_providers("fixture_sources").providers) == {"alpaca", "yfinance"}


@pytest.mark.parametrize(
    "payload",
    [
        '{"api_key_id":"synthetic","api_secret":',
        '{"api_key_id":"synthetic","api_secret":"first","api_secret":"second"}',
        '{"api_key_id":"synthetic","api_secret":" "}',
        '{"api_key_id":"synthetic","api_secret":"valid","extra":"synthetic"}',
    ],
    ids=["malformed", "duplicate", "blank", "extra"],
)
def test_registry_reads_non_keyring_store_with_strict_decoding(payload: str) -> None:
    class SerializedStore:
        def get(self, connection_id: str) -> ProviderCredentials:
            return ProviderCredentials({"api_key_id": "synthetic", "api_secret": "legacy-read-bypassed-decoding"})

        def read(
            self,
            connection_id: str,
            fields: tuple[CredentialField, ...],
            display_name: str,
        ) -> ProviderCredentials:
            from data.provider.credentials import decode_credentials

            assert connection_id == "alpaca-paper"
            assert tuple(field.key for field in fields) == ("api_key_id", "api_secret")
            return decode_credentials(payload, fields, display_name)

        def put(self, connection_id: str, credentials: ProviderCredentials) -> None:
            pytest.fail("Unexpected write")

        def delete(self, connection_id: str) -> None:
            pytest.fail("Unexpected delete")

    providers = discover_providers("providers")

    with pytest.raises(CredentialStoreUnavailableError) as raised:
        providers.credentials_for("alpaca-paper", SerializedStore())
    assert str(raised.value) == "Stored credentials for Alpaca Paper are invalid."
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_registry_rejects_invalid_decoded_fields_from_non_keyring_store() -> None:
    class DecodedStore:
        def get(self, connection_id: str) -> ProviderCredentials:
            return ProviderCredentials({"api_key_id": "synthetic", "unexpected": "synthetic"})

        def read(
            self,
            connection_id: str,
            fields: tuple[CredentialField, ...],
            display_name: str,
        ) -> ProviderCredentials:
            return self.get(connection_id)

        def put(self, connection_id: str, credentials: ProviderCredentials) -> None:
            pytest.fail("Unexpected write")

        def delete(self, connection_id: str) -> None:
            pytest.fail("Unexpected delete")

    with pytest.raises(CredentialStoreUnavailableError, match="Stored credentials for Alpaca Paper are invalid"):
        discover_providers("providers").credentials_for("alpaca-paper", DecodedStore())


def test_data_layer_has_no_provider_implementation_imports() -> None:
    forbidden = ("providers", "alpaca", "yfinance")
    violations: list[str] = []
    for path in sorted((_ROOT / "data").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            if any(module.split(".")[0] in forbidden for module in modules):
                violations.append(f"{path.relative_to(_ROOT)}:{node.lineno} imports {modules}")
    assert not violations, "\n".join(violations)
