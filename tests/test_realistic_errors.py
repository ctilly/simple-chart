import threading
from collections.abc import Callable, Iterator

import keyring.errors
import pytest
import requests  # type: ignore[import-untyped]
from alpaca.common.exceptions import APIError
from curl_cffi import requests as curl_requests
from curl_cffi.requests import exceptions as curl_exceptions
from yfinance.exceptions import YFRateLimitError

from data.provider.base import MarketDataEntitlementError
from data.provider.credentials import CredentialStoreUnavailableError
from realistic_errors import (
    SECRET,
    SOURCES,
    LocalErrorServer,
    assert_secret_absent,
    invalid_header_values,
    requests_invalid_header,
    secret_forms,
)


_EXPECTED_TYPES: dict[str, type[BaseException]] = {
    "requests_invalid_header": requests.exceptions.InvalidHeader,
    "requests_invalid_header_bytes": requests.exceptions.InvalidHeader,
    "http_client_invalid_header": ValueError,
    "requests_connection_error": requests.exceptions.ConnectionError,
    "requests_timeout": requests.exceptions.ReadTimeout,
    "requests_http_401": requests.exceptions.HTTPError,
    "requests_http_403": requests.exceptions.HTTPError,
    "requests_http_429": requests.exceptions.HTTPError,
    "requests_http_500": requests.exceptions.HTTPError,
    "curl_connection_error": curl_exceptions.ConnectionError,
    "curl_timeout": curl_exceptions.Timeout,
    "curl_http_401": curl_exceptions.HTTPError,
    "curl_http_403": curl_exceptions.HTTPError,
    "curl_http_429": curl_exceptions.HTTPError,
    "curl_http_500": curl_exceptions.HTTPError,
    "alpaca_api_401": APIError,
    "alpaca_api_403": APIError,
    "alpaca_api_429": APIError,
    "alpaca_api_entitlement": APIError,
    "alpaca_entitlement": MarketDataEntitlementError,
    "yfinance_rate_limit": YFRateLimitError,
    "keyring_read": CredentialStoreUnavailableError,
    "keyring_write": CredentialStoreUnavailableError,
    "keyring_unavailable": CredentialStoreUnavailableError,
    "unknown": RuntimeError,
}

# Where the real library (or the realistic fake backend) carries the secret.
# Sources absent here never carry it: the library message omits the URL.
_SECRET_IN_MESSAGE = {
    "requests_invalid_header",
    "requests_invalid_header_bytes",
    "http_client_invalid_header",
    "requests_connection_error",
    "requests_http_401",
    "requests_http_403",
    "requests_http_429",
    "requests_http_500",
    "alpaca_api_401",
    "alpaca_api_403",
    "alpaca_api_429",
    "alpaca_api_entitlement",
    "unknown",
}
_SECRET_IN_CAUSE_MESSAGE = {"alpaca_entitlement", "keyring_read", "keyring_write"}
_SECRET_IN_RESPONSE_URL = {
    "requests_timeout",
    "curl_connection_error",
    "curl_timeout",
    "curl_http_401",
    "curl_http_403",
    "curl_http_429",
    "curl_http_500",
}
_SECRET_NOT_CARRIED = {"yfinance_rate_limit", "keyring_unavailable"}

_EXPECTED_STATUS = {
    "requests_http_401": 401,
    "requests_http_403": 403,
    "requests_http_429": 429,
    "requests_http_500": 500,
    "curl_http_401": 401,
    "curl_http_403": 403,
    "curl_http_429": 429,
    "curl_http_500": 500,
    "alpaca_api_401": 401,
    "alpaca_api_403": 403,
    "alpaca_api_429": 429,
    "alpaca_api_entitlement": 403,
}


@pytest.fixture(scope="module")
def error_server() -> Iterator[LocalErrorServer]:
    with LocalErrorServer() as server:
        yield server


@pytest.fixture
def hostile_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
    error_server: LocalErrorServer,
) -> None:
    proxy = error_server.proxy_trap_url()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, proxy)
        monkeypatch.setenv(name.lower(), proxy)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")


@pytest.mark.usefixtures("hostile_proxy_environment")
def test_hostile_proxy_environment_diverts_default_clients(
    error_server: LocalErrorServer,
) -> None:
    assert requests.get(error_server.status_url(200), timeout=0.2).status_code == 407
    assert curl_requests.get(error_server.status_url(200), timeout=0.2).status_code == 407


@pytest.mark.usefixtures("hostile_proxy_environment")
@pytest.mark.parametrize("label", sorted(SOURCES))
def test_factory_ignores_environment_proxies(
    error_server: LocalErrorServer,
    label: str,
) -> None:
    exc = SOURCES[label](error_server)

    assert type(exc) is _EXPECTED_TYPES[label]
    if label in _EXPECTED_STATUS:
        assert getattr(exc, "response").status_code == _EXPECTED_STATUS[label]


def test_every_source_has_an_expected_type_and_secret_location() -> None:
    locations = (
        _SECRET_IN_MESSAGE
        | _SECRET_IN_CAUSE_MESSAGE
        | _SECRET_IN_RESPONSE_URL
        | _SECRET_NOT_CARRIED
    )

    assert set(SOURCES) == set(_EXPECTED_TYPES) == locations


@pytest.mark.parametrize("label", sorted(SOURCES))
def test_factory_produces_the_claimed_real_exception(
    error_server: LocalErrorServer,
    label: str,
) -> None:
    exc = SOURCES[label](error_server)

    assert type(exc) is _EXPECTED_TYPES[label]
    assert exc.__traceback__ is not None
    if label in _SECRET_IN_MESSAGE:
        assert SECRET in str(exc)
    elif label in _SECRET_IN_CAUSE_MESSAGE:
        assert SECRET not in str(exc)
        assert SECRET in str(exc.__cause__)
    elif label in _SECRET_IN_RESPONSE_URL:
        assert SECRET not in str(exc)
        response = getattr(exc, "response", None)
        carrier = response if response is not None else getattr(exc, "request")
        assert SECRET in carrier.url
    else:
        assert SECRET not in str(exc)


@pytest.mark.parametrize("label", sorted(_EXPECTED_STATUS))
def test_http_failures_carry_a_real_response_status(
    error_server: LocalErrorServer,
    label: str,
) -> None:
    exc = SOURCES[label](error_server)

    response = getattr(exc, "response")
    assert response is not None
    assert response.status_code == _EXPECTED_STATUS[label]


def test_alpaca_entitlement_error_comes_from_the_provider_classification(
    error_server: LocalErrorServer,
) -> None:
    exc = SOURCES["alpaca_entitlement"](error_server)

    assert isinstance(exc.__cause__, APIError)
    assert exc.__cause__.code == 40310000


def test_keyring_failures_wrap_the_backend_error(
    error_server: LocalErrorServer,
) -> None:
    read = SOURCES["keyring_read"](error_server)
    write = SOURCES["keyring_write"](error_server)

    assert type(read.__cause__) is keyring.errors.KeyringError
    assert type(write.__cause__) is keyring.errors.PasswordSetError


def test_invalid_header_values_cover_each_shape_with_an_interior_break() -> None:
    values = invalid_header_values()

    shapes = {label.rsplit("_", 1)[0] for label in values}
    assert shapes == {
        "ascii_lf",
        "ascii_cr",
        "unicode_newline",
        "backslash",
        "single_quote",
        "double_quote",
        "both_quotes",
        "control_bytes",
        "long_600",
        "embedded",
    }
    for shape in shapes:
        assert isinstance(values[f"{shape}_str"], str)
        assert isinstance(values[f"{shape}_bytes"], bytes)
    for value in values.values():
        text = value.decode("utf-8") if isinstance(value, bytes) else value
        interior = text[1:-1]
        assert "\r" in interior or "\n" in interior
        assert SECRET in text
    assert len(values["long_600_str"]) == 600
    embedded = values["embedded_str"]
    assert isinstance(embedded, str)
    assert not embedded.startswith(SECRET)


@pytest.mark.parametrize("label", sorted(invalid_header_values()))
def test_every_invalid_header_value_fails_preparation_with_the_secret(
    label: str,
) -> None:
    exc = requests_invalid_header(invalid_header_values()[label])

    assert type(exc) is requests.exceptions.InvalidHeader
    assert SECRET in str(exc)


def test_secret_forms_are_distinct_for_escaped_characters() -> None:
    secret = "sé\"cr\\et'\n"

    forms = secret_forms(secret)

    assert len(forms) >= 4
    assert secret in forms
    assert "s\\xc3\\xa9" in "".join(forms)
    assert "\\u00e9" in "".join(forms)


@pytest.mark.parametrize("form", secret_forms("sé\"cr\\et'\n"))
def test_assert_secret_absent_detects_each_form(form: str) -> None:
    with pytest.raises(AssertionError) as raised:
        assert_secret_absent(f"prefix {form} suffix", "sé\"cr\\et'\n")

    assert "sé" not in str(raised.value)


def test_assert_secret_absent_accepts_clean_output() -> None:
    assert_secret_absent("requests.exceptions.HTTPError (HTTP 401)")


def _no_action() -> None:
    pass


def _assert_server_joins_hanging_handler_threads(
    during: Callable[[], None] = _no_action,
) -> None:
    with LocalErrorServer() as server, requests.Session() as session:
        session.trust_env = False
        with pytest.raises(requests.exceptions.ReadTimeout):
            session.get(server.hang_url(), timeout=0.2)
        during()

    # Only the server's own threads: unrelated threads may start or end.
    threads = server.threads()
    assert len(threads) >= 2
    assert not any(thread.is_alive() for thread in threads)


def test_server_joins_hanging_handler_threads_on_exit() -> None:
    _assert_server_joins_hanging_handler_threads()


def test_server_thread_check_ignores_unrelated_thread_ending() -> None:
    release = threading.Event()
    unrelated = threading.Thread(target=release.wait, name="unrelated")
    unrelated.start()

    def end_unrelated_thread() -> None:
        release.set()
        unrelated.join()

    _assert_server_joins_hanging_handler_threads(end_unrelated_thread)


@pytest.mark.usefixtures("hostile_proxy_environment")
def test_server_joins_hanging_handler_threads_despite_environment_proxies() -> None:
    _assert_server_joins_hanging_handler_threads()
