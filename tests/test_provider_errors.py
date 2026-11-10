"""Providers translate their libraries' failures into neutral errors.

Each failure is produced by the real library inside the real provider method
(Alpaca over a real client against the local server; Yahoo through the real
yfinance history path with a real curl_cffi request at its transport).
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import requests  # type: ignore[import-untyped]
from alpaca.common.exceptions import APIError
from curl_cffi.requests import exceptions as curl_exceptions
from yfinance.exceptions import YFRateLimitError

from data.errors import UserFacingError, describe
from data.models import Timeframe
from data.provider import (
    ProviderConfigurationError,
    alpaca_provider as alpaca_provider_module,
    create_provider,
)
from data.provider.alpaca_provider import AlpacaProvider, _MarketDataClient
from data.provider.base import (
    HistoryUnavailableError,
    MarketDataEntitlementError,
    ProviderCredentialsMalformedError,
    ProviderCredentialsRejectedError,
    ProviderRateLimitedError,
    ProviderRequestRefusedError,
    ProviderUnreachableError,
)
from data.provider.config import (
    ConnectionEnvironment,
    MarketDataFeed,
    ProviderConnection,
)
from data.provider.credentials import (
    CredentialStoreUnavailableError,
    ProviderCredentials,
)
from data.provider.yfinance_provider import YFinanceProvider
from realistic_errors import (
    PROVIDER_SOURCES,
    SECRET,
    LocalErrorServer,
    _curl_get,
    alpaca_provider,
    alpaca_provider_failure,
    assert_secret_absent,
    invalid_header_values,
    yahoo_transport,
)


MALFORMED = (
    "The saved Alpaca Paper credentials are malformed. Re-enter them in Settings."
)
ALPACA_UNREACHABLE = "Couldn't reach Alpaca Paper. Check your connection."
ALPACA_REJECTED = "Alpaca Paper rejected the credentials."
ALPACA_RATE_LIMITED = "Alpaca Paper is rate limiting requests. Try again shortly."
ALPACA_ENTITLEMENT = "Your Alpaca Paper plan doesn't include this data."
SIP_ENTITLEMENT = (
    "Alpaca rejected real-time SIP data for this account. "
    "Select 15-minute delayed SIP or IEX, or add a real-time "
    "SIP subscription to the Alpaca account."
)
DELAYED_SIP_ENTITLEMENT = (
    "Alpaca rejected the delayed SIP request as too recent. "
    "Try the request again; if the problem persists, select IEX "
    "or check the market-data plan on the Alpaca account."
)
YAHOO_UNREACHABLE = "Couldn't reach Yahoo Finance. Check your connection."
YAHOO_REFUSED = "Yahoo Finance refused the request."
YAHOO_RATE_LIMITED = "Yahoo Finance is rate limiting requests. Try again shortly."

# label -> (raised type, its message (None: untranslated), cause type)
EXPECTED: dict[str, tuple[type[BaseException], str | None, type[BaseException] | None]] = {
    "alpaca_invalid_header": (
        ProviderCredentialsMalformedError,
        MALFORMED,
        requests.exceptions.InvalidHeader,
    ),
    "alpaca_invalid_header_bytes": (
        ProviderCredentialsMalformedError,
        MALFORMED,
        requests.exceptions.InvalidHeader,
    ),
    "alpaca_http_client_invalid_header": (ValueError, None, None),
    "alpaca_connection_error": (
        ProviderUnreachableError,
        ALPACA_UNREACHABLE,
        requests.exceptions.ConnectionError,
    ),
    "alpaca_timeout": (
        ProviderUnreachableError,
        ALPACA_UNREACHABLE,
        requests.exceptions.ReadTimeout,
    ),
    "alpaca_api_401": (ProviderCredentialsRejectedError, ALPACA_REJECTED, APIError),
    "alpaca_api_403": (ProviderCredentialsRejectedError, ALPACA_REJECTED, APIError),
    "alpaca_api_429": (ProviderRateLimitedError, ALPACA_RATE_LIMITED, APIError),
    "alpaca_api_500": (APIError, None, None),
    # A 403 with the entitlement code: entitlement wins over 403.
    "alpaca_api_entitlement": (
        MarketDataEntitlementError,
        ALPACA_ENTITLEMENT,
        APIError,
    ),
    "alpaca_entitlement": (MarketDataEntitlementError, SIP_ENTITLEMENT, APIError),
    "alpaca_delayed_sip_entitlement": (
        MarketDataEntitlementError,
        DELAYED_SIP_ENTITLEMENT,
        APIError,
    ),
    "yfinance_connection_error": (
        ProviderUnreachableError,
        YAHOO_UNREACHABLE,
        curl_exceptions.ConnectionError,
    ),
    "yfinance_timeout": (
        ProviderUnreachableError,
        YAHOO_UNREACHABLE,
        curl_exceptions.Timeout,
    ),
    # Yahoo takes no user credentials, so a refusal never blames them.
    "yfinance_http_401": (
        ProviderRequestRefusedError,
        YAHOO_REFUSED,
        curl_exceptions.HTTPError,
    ),
    "yfinance_http_403": (
        ProviderRequestRefusedError,
        YAHOO_REFUSED,
        curl_exceptions.HTTPError,
    ),
    "yfinance_http_429": (
        ProviderRateLimitedError,
        YAHOO_RATE_LIMITED,
        curl_exceptions.HTTPError,
    ),
    "yfinance_http_500": (curl_exceptions.HTTPError, None, None),
    "yfinance_rate_limit": (
        ProviderRateLimitedError,
        YAHOO_RATE_LIMITED,
        YFRateLimitError,
    ),
}


@pytest.fixture(scope="module")
def error_server() -> Iterator[LocalErrorServer]:
    with LocalErrorServer() as server:
        yield server


@pytest.fixture(scope="module")
def provider_errors(
    error_server: LocalErrorServer,
) -> dict[str, BaseException]:
    return {label: produce(error_server) for label, produce in PROVIDER_SOURCES.items()}


def _assert_translated(
    exc: BaseException,
    expected_type: type[BaseException],
    message: str,
    cause_type: type[BaseException],
) -> None:
    assert type(exc) is expected_type
    assert str(exc) == message
    # raise ... from the library exception: describe() still logs its type.
    cause = exc.__cause__
    assert cause is not None
    assert isinstance(cause, cause_type)
    assert exc.__suppress_context__
    assert_secret_absent(str(exc))
    assert str(cause) not in str(exc)


def test_every_provider_source_has_an_expected_row() -> None:
    assert set(EXPECTED) == set(PROVIDER_SOURCES)


@pytest.mark.parametrize("label", sorted(PROVIDER_SOURCES))
def test_real_provider_failure_surfaces_as_its_neutral_type(
    provider_errors: dict[str, BaseException],
    label: str,
) -> None:
    exc = provider_errors[label]
    expected_type, message, cause_type = EXPECTED[label]

    if message is None:
        # Untranslated: the library's own exception, unchanged.
        assert type(exc) is expected_type
        assert not isinstance(exc, UserFacingError)
        return
    assert cause_type is not None
    _assert_translated(exc, expected_type, message, cause_type)


@pytest.mark.parametrize("shape", sorted(invalid_header_values()))
def test_every_invalid_header_shape_is_malformed_credentials(
    error_server: LocalErrorServer,
    shape: str,
) -> None:
    exc = alpaca_provider_failure(
        error_server.status_url(200),
        api_key=invalid_header_values()[shape],
    )

    _assert_translated(
        exc,
        ProviderCredentialsMalformedError,
        MALFORMED,
        requests.exceptions.InvalidHeader,
    )


@pytest.mark.parametrize("method", ["fetch_level1", "fetch_snapshots"])
@pytest.mark.parametrize(
    ("url", "expected_type", "message", "cause_type"),
    [
        ("status_401", ProviderCredentialsRejectedError, ALPACA_REJECTED, APIError),
        (
            "refused",
            ProviderUnreachableError,
            ALPACA_UNREACHABLE,
            requests.exceptions.ConnectionError,
        ),
    ],
)
def test_alpaca_snapshot_paths_translate_the_same_way(
    error_server: LocalErrorServer,
    method: str,
    url: str,
    expected_type: type[BaseException],
    message: str,
    cause_type: type[BaseException],
) -> None:
    base_url = (
        error_server.status_url(401) if url == "status_401" else error_server.refused_url()
    )
    provider = alpaca_provider(base_url)
    calls: dict[str, Callable[[], object]] = {
        "fetch_level1": lambda: provider.fetch_level1("SPY"),
        "fetch_snapshots": lambda: provider.fetch_snapshots(["SPY"]),
    }

    with pytest.raises(expected_type) as raised:
        calls[method]()

    _assert_translated(raised.value, expected_type, message, cause_type)


# requests is untyped here, so its classes are Any as bases.
class _MalformedHeaderAndConnectionError(requests.exceptions.InvalidHeader, requests.exceptions.ConnectionError):  # type: ignore[misc]
    pass


class _RaisingClient:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def get_stock_bars(self, request_params: object) -> Any:
        raise self._exc

    def get_stock_snapshot(self, request_params: object) -> Any:
        raise self._exc


def _fetch_with_client(exc: BaseException) -> BaseException:
    provider = AlpacaProvider(
        "key",
        "secret",
        MarketDataFeed.IEX,
        cast(_MarketDataClient, _RaisingClient(exc)),
        display_name="Alpaca Paper",
    )
    with pytest.raises(BaseException) as raised:
        provider.fetch_bars(
            "SPY",
            Timeframe.DAILY,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 5, tzinfo=UTC),
        )
    return raised.value


def test_malformed_header_wins_over_connection_error() -> None:
    exc = _fetch_with_client(_MalformedHeaderAndConnectionError(f"bad {SECRET}"))

    assert type(exc) is ProviderCredentialsMalformedError


def test_rejection_with_a_non_json_body_is_not_entitlement(
    error_server: LocalErrorServer,
) -> None:
    rejected = alpaca_provider_failure(error_server.status_url(403)).__cause__
    assert isinstance(rejected, APIError)
    html = APIError(  # type: ignore[no-untyped-call]
        f"<html>forbidden {SECRET}</html>",
        rejected._http_error,
    )

    exc = _fetch_with_client(html)

    _assert_translated(exc, ProviderCredentialsRejectedError, ALPACA_REJECTED, APIError)


def test_status_less_api_error_is_left_untranslated() -> None:
    error = APIError('{"code":40010001,"message":"bad request"}')  # type: ignore[no-untyped-call]

    assert _fetch_with_client(error) is error


class _Store:
    def get(self, connection_id: str) -> ProviderCredentials | None:
        return ProviderCredentials(f"AK{SECRET}", f"SK{SECRET}")

    def put(self, connection_id: str, credentials: ProviderCredentials) -> None:
        pass

    def delete(self, connection_id: str) -> None:
        pass


def test_factory_gives_the_alpaca_provider_its_connection_name(
    monkeypatch: pytest.MonkeyPatch,
    error_server: LocalErrorServer,
) -> None:
    # The production factory path, with its client pointed at the local server.
    def local_client(api_key_id: str, api_secret: str) -> _MarketDataClient:
        return alpaca_provider(error_server.status_url(401))._client()

    monkeypatch.setattr(
        alpaca_provider_module,
        "_create_stock_historical_data_client",
        local_client,
    )
    connection = ProviderConnection(
        connection_id="alpaca-custom",
        display_name="My Alpaca",
        provider_name="alpaca",
        environment=ConnectionEnvironment.PAPER,
        feed=MarketDataFeed.IEX,
    )
    provider = create_provider(connection, _Store())

    with pytest.raises(ProviderCredentialsRejectedError) as raised:
        provider.fetch_bars(
            "SPY",
            Timeframe.DAILY,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 5, tzinfo=UTC),
        )

    assert str(raised.value) == "My Alpaca rejected the credentials."


def _raise_rate_limit() -> None:
    raise YFRateLimitError()


def test_factory_gives_the_yahoo_provider_its_connection_name() -> None:
    connection = ProviderConnection(
        connection_id="yahoo-custom",
        display_name="My Yahoo",
        provider_name="yfinance",
    )
    provider = create_provider(connection, _Store())

    with yahoo_transport(_raise_rate_limit):
        with pytest.raises(ProviderRateLimitedError) as raised:
            provider.fetch_bars(
                "SPY",
                Timeframe.DAILY,
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 5, tzinfo=UTC),
            )

    assert str(raised.value) == "My Yahoo is rate limiting requests. Try again shortly."


def test_yahoo_paths_that_swallow_failures_are_unchanged(
    error_server: LocalErrorServer,
) -> None:
    def refuse() -> None:
        _curl_get(f"{error_server.refused_url()}/v8/finance/chart/SPY")

    provider = YFinanceProvider(display_name="Yahoo Finance")
    with yahoo_transport(refuse):
        assert provider.fetch_level1("SPY") is None
        assert provider.fetch_snapshots(["SPY"]) == {}


def test_yahoo_missing_prices_is_still_history_unavailable() -> None:
    def no_prices() -> object:
        class _Response:
            text = "{}"

            def json(self) -> dict[str, object]:
                return {"chart": {"result": None, "error": {"description": SECRET}}}

        return _Response()

    with yahoo_transport(no_prices):
        with pytest.raises(HistoryUnavailableError) as raised:
            YFinanceProvider(display_name="Yahoo Finance").fetch_bars(
                "TRAX",
                Timeframe.DAILY,
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 5, tzinfo=UTC),
            )

    assert str(raised.value) == "History is currently unavailable for TRAX (daily)."
    assert_secret_absent(str(raised.value))


@pytest.mark.parametrize(
    "error_type",
    [
        ProviderCredentialsRejectedError,
        ProviderRateLimitedError,
        ProviderRequestRefusedError,
        ProviderUnreachableError,
        ProviderCredentialsMalformedError,
        MarketDataEntitlementError,
        HistoryUnavailableError,
        ProviderConfigurationError,
        CredentialStoreUnavailableError,
    ],
)
def test_neutral_types_are_user_facing_runtime_errors(
    error_type: type[BaseException],
) -> None:
    assert issubclass(error_type, UserFacingError)
    assert issubclass(error_type, RuntimeError)


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
@pytest.mark.parametrize("label", sorted(PROVIDER_SOURCES))
def test_provider_source_ignores_environment_proxies(
    error_server: LocalErrorServer,
    label: str,
) -> None:
    exc = PROVIDER_SOURCES[label](error_server)

    assert type(exc) is EXPECTED[label][0]


@pytest.mark.parametrize(
    "label",
    sorted(label for label, row in EXPECTED.items() if row[1] is not None),
)
def test_describe_logs_the_library_cause_type_never_its_text(
    provider_errors: dict[str, BaseException],
    label: str,
) -> None:
    exc = provider_errors[label]
    cause = exc.__cause__
    assert cause is not None

    output = describe(exc)

    cause_type = f"{type(cause).__module__}.{type(cause).__qualname__}"
    assert f"caused by {cause_type}" in output
    if str(cause):
        assert str(cause) not in output
    assert_secret_absent(output)
