import ast
import logging
import re
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import requests  # type: ignore[import-untyped]

import app.errors as app_errors
from app.errors import ErrorContext, report_error, user_message
from chart.window import ChartWidget
from data.cache import Cache
from data.errors import UserFacingError, describe
from data.models import BarCorrection, Timeframe
from data.provider import create_provider
from data.provider.base import HistoryUnavailableError
from data.provider.config import (
    ConnectionEnvironment,
    ProviderConnection,
    alpaca_paper_connection,
)
from data.provider.credentials import (
    ProviderCredentials,
    UnavailableCredentialStore,
    initialize_keyring_credential_store,
)
from realistic_errors import (
    PROVIDER_SOURCES,
    SECRET,
    SOURCES,
    LocalErrorServer,
    SecretLeakingKeyring,
    assert_secret_absent,
)


ACTION = "Loading TRAX 5m history"
CONTEXT = ErrorContext(ACTION)
GENERIC = "Loading TRAX 5m history failed. Details were logged."
SIP_ENTITLEMENT = (
    "Alpaca rejected real-time SIP data for this account. "
    "Select 15-minute delayed SIP or IEX, or add a real-time "
    "SIP subscription to the Alpaca account."
)

# Raw library failures: only SimpleChart's own errors show their text.
EXPECTED: dict[str, str] = {
    "requests_invalid_header": GENERIC,
    "requests_invalid_header_bytes": GENERIC,
    "http_client_invalid_header": GENERIC,
    "requests_connection_error": GENERIC,
    "requests_timeout": GENERIC,
    "requests_http_401": GENERIC,
    "requests_http_403": GENERIC,
    "requests_http_429": GENERIC,
    "requests_http_500": GENERIC,
    "curl_connection_error": GENERIC,
    "curl_timeout": GENERIC,
    "curl_http_401": GENERIC,
    "curl_http_403": GENERIC,
    "curl_http_429": GENERIC,
    "curl_http_500": GENERIC,
    "alpaca_api_401": GENERIC,
    "alpaca_api_403": GENERIC,
    "alpaca_api_429": GENERIC,
    "alpaca_api_entitlement": GENERIC,
    "alpaca_entitlement": SIP_ENTITLEMENT,
    "yfinance_rate_limit": GENERIC,
    "keyring_read": "Unable to read provider credentials from the OS keyring.",
    "keyring_write": "Unable to save provider credentials in the OS keyring.",
    "keyring_unavailable": "The operating-system credential store is unavailable.",
    "unknown": GENERIC,
}


@pytest.fixture(scope="module")
def error_server() -> Iterator[LocalErrorServer]:
    with LocalErrorServer() as server:
        yield server


@pytest.fixture(scope="module")
def real_errors(error_server: LocalErrorServer) -> dict[str, BaseException]:
    return {label: produce(error_server) for label, produce in SOURCES.items()}


@pytest.fixture(scope="module")
def provider_errors(error_server: LocalErrorServer) -> dict[str, BaseException]:
    return {
        label: produce(error_server) for label, produce in PROVIDER_SOURCES.items()
    }


def _foreign_messages(exc: BaseException) -> list[str]:
    # Text of every exception in the chain that SimpleChart did not compose.
    messages: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        if not isinstance(current, UserFacingError):
            messages.append(str(current))
        current = current.__cause__ or current.__context__
    return [message for message in messages if message]


def _assert_shielded(message: str, exc: BaseException) -> None:
    assert_secret_absent(message)
    for foreign in _foreign_messages(exc):
        assert foreign not in message


def test_every_harness_source_has_an_expected_row() -> None:
    assert set(EXPECTED) == set(SOURCES)


@pytest.mark.parametrize("label", sorted(SOURCES))
def test_raw_real_error_shows_only_app_composed_text(
    real_errors: dict[str, BaseException],
    label: str,
) -> None:
    exc = real_errors[label]

    message = user_message(exc, CONTEXT)

    assert message == EXPECTED[label]
    _assert_shielded(message, exc)


@pytest.mark.parametrize("label", sorted(PROVIDER_SOURCES))
def test_provider_error_shows_its_neutral_text(
    provider_errors: dict[str, BaseException],
    label: str,
) -> None:
    exc = provider_errors[label]

    message = user_message(exc, CONTEXT)

    # The exact neutral texts are pinned in test_provider_errors.
    if isinstance(exc, UserFacingError):
        assert message == str(exc)
        assert exc.__cause__ is not None
    else:
        assert message == GENERIC
    _assert_shielded(message, exc)


def test_untranslated_library_exception_gets_the_generic_message() -> None:
    response = requests.Response()
    response.status_code = 401
    exc = requests.exceptions.HTTPError(f"token {SECRET}", response=response)

    assert user_message(exc, CONTEXT) == GENERIC


def test_user_facing_error_from_cache_shows_its_own_text(tmp_path: Path) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        with pytest.raises(UserFacingError) as raised:
            cache.put_bar_correction(
                BarCorrection(
                    cache_namespace="alpaca:delayed_sip",
                    symbol="SPY",
                    timeframe=Timeframe.DAILY,
                    timestamp=datetime(2026, 2, 2, 5, 0, tzinfo=UTC),
                    low=685.77,
                )
            )

    assert (
        user_message(raised.value, CONTEXT)
        == "The provider bar to correct does not exist."
    )


def test_panel_limit_shows_its_own_text() -> None:
    # Widget-free, like the S1 panel-limit test.
    occupied = SimpleNamespace(
        _extension_slots=[
            SimpleNamespace(name=name) for name in ("rsi", "macd", "volume_profile")
        ]
    )
    with pytest.raises(RuntimeError) as raised:
        ChartWidget.ensure_extension_panel(cast(ChartWidget, occupied), "stochastic")

    assert user_message(raised.value, CONTEXT) == (
        "All 3 extension panel slots are in use. "
        "Remove an existing panel extension before adding another."
    )


# requests is untyped here, so its classes are Any as bases.
class _UserFacingConnectionError(UserFacingError, requests.exceptions.ConnectionError):  # type: ignore[misc]
    pass


def test_user_facing_error_shows_its_text_whatever_else_it_is() -> None:
    exc = _UserFacingConnectionError("Pick a different symbol.")

    assert user_message(exc, CONTEXT) == "Pick a different symbol."


# ------------------------------------------------------------------
# Restored app-composed guidance
# ------------------------------------------------------------------


class _MemoryCredentialStore:
    def __init__(self, credentials: ProviderCredentials | None = None) -> None:
        self._credentials = credentials

    def get(self, connection_id: str) -> ProviderCredentials | None:
        return self._credentials

    def put(self, connection_id: str, credentials: ProviderCredentials) -> None:
        pass

    def delete(self, connection_id: str) -> None:
        pass


def _create_provider_message(
    connection: ProviderConnection,
    credentials: ProviderCredentials | None = None,
) -> str:
    with pytest.raises(RuntimeError) as raised:
        create_provider(connection, _MemoryCredentialStore(credentials))
    return user_message(raised.value, CONTEXT)


def test_missing_credentials_guidance_is_shown() -> None:
    assert (
        _create_provider_message(alpaca_paper_connection())
        == "Alpaca Paper credentials are not configured."
    )


def test_missing_package_guidance_is_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    # A None entry is how Python marks a module whose import fails.
    monkeypatch.setitem(sys.modules, "alpaca", None)

    assert (
        _create_provider_message(alpaca_paper_connection())
        == "Required package 'alpaca-py' is not installed."
    )


def test_incomplete_connection_guidance_is_shown() -> None:
    connection = ProviderConnection(
        connection_id="alpaca-custom",
        display_name="My Alpaca",
        provider_name="alpaca",
        environment=ConnectionEnvironment.PAPER,
    )

    assert (
        _create_provider_message(connection, ProviderCredentials("AK", "SK"))
        == "My Alpaca is not fully configured."
    )


def test_unregistered_provider_guidance_is_shown() -> None:
    connection = ProviderConnection(
        connection_id="custom",
        display_name="Custom",
        provider_name="not-registered",
    )

    assert _create_provider_message(connection) == (
        "Provider 'not-registered' is not available. Available: alpaca, yfinance"
    )


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("get", "Unable to read provider credentials from the OS keyring."),
        ("put", "Unable to save provider credentials in the OS keyring."),
        ("delete", "Unable to delete provider credentials from the OS keyring."),
    ],
)
def test_keyring_failure_guidance_is_shown(operation: str, expected: str) -> None:
    store = initialize_keyring_credential_store(SecretLeakingKeyring(SECRET)).store
    credentials = ProviderCredentials(f"AK{SECRET}", SECRET)
    calls: dict[str, Callable[[], object]] = {
        "get": lambda: store.get("alpaca-paper"),
        "put": lambda: store.put("alpaca-paper", credentials),
        "delete": lambda: store.delete("alpaca-paper"),
    }
    with pytest.raises(RuntimeError) as raised:
        calls[operation]()

    message = user_message(raised.value, CONTEXT)

    assert message == expected
    _assert_shielded(message, raised.value)


@pytest.mark.parametrize(
    "reason",
    [
        "The operating-system credential store is unavailable.",
        "Required package 'keyring' is not installed.",
    ],
)
def test_keychain_unavailable_reason_is_shown(reason: str) -> None:
    with pytest.raises(RuntimeError) as raised:
        UnavailableCredentialStore(reason).get("alpaca-paper")

    assert user_message(raised.value, CONTEXT) == reason


def test_history_unavailable_guidance_is_shown() -> None:
    exc = HistoryUnavailableError("TRAX", Timeframe.MIN5)

    assert (
        user_message(exc, CONTEXT) == "History is currently unavailable for TRAX (5m)."
    )


# ------------------------------------------------------------------
# report_error
# ------------------------------------------------------------------


def test_report_error_logs_user_facing_error_at_warning(
    provider_errors: dict[str, BaseException],
    caplog: pytest.LogCaptureFixture,
) -> None:
    exc = provider_errors["alpaca_api_401"]
    logger = logging.getLogger("app.test_errors")
    caplog.set_level(logging.WARNING, logger=logger.name)

    message = report_error(logger, exc, CONTEXT)

    assert message == "Alpaca Paper rejected the credentials."
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.WARNING, f"{ACTION} failed: {describe(exc)}"),
    ]
    assert message in caplog.text
    assert "alpaca.common.exceptions.APIError (HTTP 401)" in caplog.text
    _assert_shielded(caplog.text, exc)


def test_report_error_logs_other_errors_at_error(
    real_errors: dict[str, BaseException],
    caplog: pytest.LogCaptureFixture,
) -> None:
    exc = real_errors["alpaca_api_401"]
    logger = logging.getLogger("app.test_errors")
    caplog.set_level(logging.DEBUG, logger=logger.name)

    message = report_error(logger, exc, CONTEXT)

    assert message == GENERIC
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.ERROR, f"{ACTION} failed: {describe(exc)}"),
    ]
    _assert_shielded(caplog.text, exc)


# ------------------------------------------------------------------
# Provider-agnostic guard
# ------------------------------------------------------------------


_PROVIDER_LIBRARIES = frozenset(
    {"alpaca", "requests", "curl_cffi", "yfinance", "http", "keyring"}
)
_PROVIDER_TEXT = re.compile(
    r"\b(?:alpaca|requests|curl_cffi|yfinance|http\.client|keyring)\b"
    r"|_provider\b|APIError|YFRateLimit"
)
_DYNAMIC_LOOKUPS = frozenset({"modules", "import_module", "__import__", "importlib"})


def _is_provider_module(module: str) -> bool:
    parts = module.split(".")
    return parts[0] in _PROVIDER_LIBRARIES or any(
        part.endswith("_provider") for part in parts
    )


def provider_references(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if _is_provider_module(a.name)]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _is_provider_module(module):
                found.append(module)
            if module.startswith("data.provider"):
                found += [a.name for a in node.names if a.name.startswith("_")]
        elif isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in _PROVIDER_LIBRARIES or name in _DYNAMIC_LOOKUPS:
                found.append(name)
            elif name.startswith("_") and name.isupper():
                # A private constant, such as a provider's error code.
                found.append(name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _PROVIDER_TEXT.search(node.value):
                found.append(node.value)
    return found


def test_app_errors_references_no_provider_library_or_private_name() -> None:
    source = Path(app_errors.__file__).read_text(encoding="utf-8")

    assert provider_references(source) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "from alpaca.common.exceptions import APIError",
        "import requests",
        "import curl_cffi.requests.exceptions",
        "from yfinance.exceptions import YFRateLimitError",
        "import http.client",
        "import keyring.errors",
        "from data.provider.alpaca_provider import AlpacaProvider",
        "from data.provider.base import _BACKGROUND_CANCELLATION",
        "x = sys.modules.get('somewhere')",
        "x = importlib.import_module('somewhere')",
        "X = ('requests.exceptions.HTTPError',)",
        "x = exc.code == _SIP_ENTITLEMENT_ERROR_CODE",
    ],
)
def test_guard_catches_each_provider_reference(snippet: str) -> None:
    assert provider_references(snippet) != []
