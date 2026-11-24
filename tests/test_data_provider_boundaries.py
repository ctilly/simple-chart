import ast
import os
import pkgutil
import re
import subprocess
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path

import pytest
from fixtures.providers import fakesource
from providers import __path__ as builtin_provider_path

from data.errors import UserFacingError
from data.provider import base
from data.provider.credentials import ProviderCredentials
from data.provider.registry import discover_providers
from data.provider.spec import Capability
from fixtures.providers import __path__ as fixture_provider_path


_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_NAME = re.compile(r"\b(?:alpaca|yfinance)\b", re.IGNORECASE)
_CAPABILITY_METHODS = {
    Capability.BARS: ("fetch_bars", "native_timeframes"),
    Capability.SNAPSHOTS: ("fetch_snapshots",),
    Capability.LEVEL1: ("fetch_level1",),
    Capability.COMPANY_NAME: ("fetch_company_name",),
    Capability.PROFILE: ("fetch_profile",),
    Capability.NEWS: ("fetch_news",),
    Capability.SEARCH: ("search_symbols",),
    Capability.UPCOMING_EARNINGS: ("fetch_upcoming_earnings",),
    Capability.PAST_EARNINGS: ("fetch_past_earnings",),
    Capability.DIVIDENDS: ("fetch_dividend_dates",),
}


def _provider_packages(paths: Iterable[str], prefix: str) -> tuple[str, ...]:
    return tuple(
        info.name for info in pkgutil.iter_modules(paths, prefix)
        if info.ispkg and not info.name.removeprefix(prefix).startswith("_")
    )


@pytest.mark.parametrize(
    "package",
    _provider_packages(builtin_provider_path, "providers.")
    + _provider_packages(fixture_provider_path, "fixtures.providers."),
)
def test_discovered_provider_conformance(package: str, monkeypatch: pytest.MonkeyPatch) -> None:
    notices: list[str] = []
    registry = discover_providers("providers", "fixtures.providers", notify=notices.append)
    assert not notices
    spec = import_module(package).PROVIDER
    assert registry.providers[spec.name] is spec
    fields = spec.credential_fields
    credentials = ProviderCredentials({field.key: "synthetic" for field in fields}) if fields else None
    for template in spec.connections:
        provider = spec.factory(registry.build_connection(template.connection_id), credentials)
        cls = type(provider)
        assert cls.check_connection is not base.DataProvider.check_connection
        assert set(_CAPABILITY_METHODS) == set(Capability)
        for capability in spec.capabilities:
            for method in _CAPABILITY_METHODS[capability]:
                assert getattr(cls, method) is not getattr(base.DataProvider, method), method

        if Capability.NEWS in spec.capabilities:
            # A new news provider must supply a raw SDK response fixture here.
            if package == "providers.yahoo":
                from providers.yahoo import implementation

                def get_news(_ticker: object, *, count: int) -> list[dict[str, object]]:
                    return [
                        {"content": {"title": "Offset", "pubDate": "2026-01-05T10:00:00-05:00"}},
                        {"content": {"title": "Naive", "pubDate": "2026-01-05T10:00:00"}},
                    ]

                monkeypatch.setattr(implementation.yf.Ticker, "get_news", get_news)
            else:
                pytest.fail(f"Missing offline news fixture for {package}")
            articles = provider.fetch_news("ACME")
            assert articles is not None
            assert len(articles) == 2
            assert articles[0].published == datetime(2026, 1, 5, 15, tzinfo=UTC)
            assert all(article.published is None or article.published.tzinfo is UTC for article in articles)
            assert articles[1].published is None


def test_user_plugin_declaration_cannot_add_a_provider(tmp_path: Path) -> None:
    plugin_dir = tmp_path / ".simplechart" / "plugins"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "provider_attempt.py").write_text(
        """
from dataclasses import replace

from data.provider.spec import ConnectionTemplate, NamespaceSpec
from providers.yahoo import PROVIDER as YAHOO

PROVIDER = replace(
    YAHOO,
    name="plugin-source",
    connections=(ConnectionTemplate("plugin-source", "Plugin Source"),),
    namespaces=(NamespaceSpec("plugin-source", "Plugin Source", (), True),),
)
""",
        encoding="utf-8",
    )
    script = """
import sys

from data.provider.registry import discover_providers
from simplechart.bootstrap import bootstrap

registry = bootstrap()
loaded = sys.modules["simplechart_user_plugins.provider_attempt"]
assert "plugin-source" in discover_providers(("providers.yahoo", loaded)).providers
assert "plugin-source" not in registry.providers
assert "plugin-source" not in registry.connection_ids
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_ROOT,
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_data_provider_skill_and_fixture_are_linked() -> None:
    skill = "docs/skills/data-provider.md"
    assert (_ROOT / skill).is_file()
    assert f"| `{skill}` |" in (_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert tuple(field.key for field in fakesource.PROVIDER.credential_fields) == ("token",)
    assert len(fakesource.PROVIDER.option_fields) == 1
    assert fakesource.PROVIDER.option_fields[0].user_editable
    assert fakesource.PROVIDER.capabilities == frozenset({Capability.BARS, Capability.PROFILE})
    assert len(fakesource.PROVIDER.namespaces) == 1
    assert fakesource.PROVIDER.namespaces[0].comparable


@pytest.mark.parametrize("agent", (".codex", ".claude", ".gemini", ".grok"))
def test_data_provider_skill_forwarder(agent: str) -> None:
    forwarder = _ROOT / agent / "skills" / "data-provider.md"
    assert forwarder.is_file()
    assert "`docs/skills/data-provider.md`" in forwarder.read_text(encoding="utf-8")


def test_application_has_no_provider_imports_or_literals() -> None:
    paths = [_ROOT / "main.py"]
    for directory in ("app", "data", "simplechart"):
        paths.extend((_ROOT / directory).rglob("*.py"))

    violations: list[str] = []
    for path in sorted(paths):
        relative = path.relative_to(_ROOT)
        if relative == Path("data/provider/registry.py"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            imports: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                imports = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports = (node.module,)
            if any(name.split(".")[0] in {"providers", "alpaca", "yfinance"} for name in imports):
                violations.append(f"{relative}:{node.lineno}: provider import")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and _SOURCE_NAME.search(node.value):
                violations.append(f"{relative}:{node.lineno}: provider name in string")

    assert not violations, "\n".join(violations)


def _expression_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_provider_message(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    return isinstance(node, ast.JoinedStr) and all(
        _expression_name(part.value) == "display_name"
        for part in ast.walk(node) if isinstance(part, ast.FormattedValue)
    )


def _provider_error_violations(source: str, filename: str) -> list[str]:
    error_types = {
        name: value for name, value in vars(base).items()
        if isinstance(value, type) and issubclass(value, UserFacingError)
    }
    message_types = {
        name for name, value in error_types.items() if "__init__" not in vars(value)
    }
    nodes = list(ast.walk(ast.parse(source, filename=filename)))
    aliases = {
        alias.asname: alias.name
        for node in nodes if isinstance(node, ast.ImportFrom)
        for alias in node.names if alias.asname and alias.name in error_types
    }
    violations: list[str] = []
    for node in nodes:
        if isinstance(node, ast.ClassDef):
            for parent in node.bases:
                name = _expression_name(parent)
                if name and aliases.get(name, name) in error_types:
                    violations.append(f"{filename}:{node.lineno}: provider-defined user-facing error")
        elif isinstance(node, ast.Call):
            name = _expression_name(node.func)
            if name and aliases.get(name, name) in message_types and any(
                not _is_provider_message(argument)
                for argument in (*node.args, *(keyword.value for keyword in node.keywords))
            ):
                violations.append(f"{filename}:{node.lineno}: invalid provider error message")
    return violations


def test_provider_packages_use_only_framework_user_facing_messages() -> None:
    violations = [
        violation
        for path in sorted((_ROOT / "providers").rglob("*.py"))
        for violation in _provider_error_violations(
            path.read_text(encoding="utf-8"), str(path.relative_to(_ROOT)),
        )
    ]
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "raise MarketDataEntitlementError(f'Request failed: {exc}')",
            "invalid provider error message", id="exception-text",
        ),
        pytest.param(
            "raise ProviderConfigurationError(f'Request failed: {response_body}')",
            "invalid provider error message", id="other-name",
        ),
        pytest.param(
            "raise ProviderConfigurationError(f'{display_name:{response_body}} failed')",
            "invalid provider error message", id="format-spec-interpolation",
        ),
        pytest.param(
            "from data.errors import UserFacingError as VisibleError\n"
            "class ProviderError(VisibleError): pass",
            "provider-defined user-facing error", id="user-facing-subclass",
        ),
    ],
)
def test_provider_error_rule_rejects_unsafe_source(source: str, expected: str) -> None:
    violations = _provider_error_violations(source, "provider.py")
    assert [violation.rsplit(": ", 1)[1] for violation in violations] == [expected]
