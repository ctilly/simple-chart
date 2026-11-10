"""Real-world error harness: failures produced by the real libraries.

Every exception comes from real library code against a local 127.0.0.1
server (or a closed local port, or a fake keyring backend driven through the
production credential store). Synthetic secrets are embedded wherever a real
failure would carry them. Nothing here contacts a real provider.
"""

import json
import re
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, Self, cast
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import keyring.errors
import requests  # type: ignore[import-untyped]
import yfinance
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from curl_cffi import CurlOpt
from curl_cffi import requests as curl_requests
from requests.adapters import HTTPAdapter  # type: ignore[import-untyped]
from yfinance.data import YfData
from yfinance.exceptions import YFRateLimitError

from data.models import Timeframe
from data.provider.alpaca_provider import AlpacaProvider, _MarketDataClient
from data.provider.config import MarketDataFeed
from data.provider.yfinance_provider import YFinanceProvider
from data.provider.credentials import (
    ProviderCredentials,
    initialize_keyring_credential_store,
)


SECRET = "SYNTHk3yQ7mZ2xP9vL4w"
# Display names the providers are constructed with, as their connections
# supply them in production.
ALPACA_DISPLAY_NAME = "Alpaca Paper"
YAHOO_DISPLAY_NAME = "Yahoo Finance"

_ALPACA_ENTITLEMENT_CODE = 40310000
_CLIENT_TIMEOUT_SECONDS = 0.2
# A hanging handler never outlives this, even if the server is not closed.
_HANG_LIMIT_SECONDS = 5.0
_MAX_HANDLER_THREADS = 8
_STATUS_PATH = re.compile(r"/status/(?P<status>\d{3})(?:/code/(?P<code>\d+))?")
_HISTORY_START = datetime(2026, 11, 2, tzinfo=UTC)
_HISTORY_END = datetime(2026, 11, 6, tzinfo=UTC)


# ------------------------------------------------------------------
# Secret assertions
# ------------------------------------------------------------------


def secret_forms(secret: str = SECRET) -> tuple[str, ...]:
    forms = (
        secret,
        repr(secret)[1:-1],
        repr(secret.encode("utf-8"))[2:-1],
        json.dumps(secret)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
    )
    return tuple(dict.fromkeys(forms))


def assert_secret_absent(output: str, secret: str = SECRET) -> None:
    for index, form in enumerate(secret_forms(secret)):
        if form in output:
            # The failure message names only the form index, never the value.
            raise AssertionError(f"synthetic secret leaked (form {index})")


# ------------------------------------------------------------------
# Local server
# ------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server: "_BoundedServer"

    def do_GET(self) -> None:
        if not self.path.startswith("/"):
            # Absolute-form request line: a client used this server as its
            # proxy. An origin server never receives one.
            self.send_response(407)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        url = urlsplit(self.path)
        if url.path.startswith("/hang"):
            # Accept and never respond; the client times out.
            self.server.release_hangs.wait(_HANG_LIMIT_SECONDS)
            self.close_connection = True
            return
        match = _STATUS_PATH.match(url.path)
        if match is None:
            self.send_error(404)
            return
        status = int(match["status"])
        code = int(match["code"]) if match["code"] else status * 100_000
        token = parse_qs(url.query).get("token", [""])[0]
        key = self.headers.get("APCA-API-KEY-ID", "")
        body = json.dumps(
            {
                "code": code,
                "message": f"request rejected for key {key} token {token}",
            }
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class _BoundedServer(ThreadingHTTPServer):
    # Handler threads are non-daemon and joined by server_close().
    daemon_threads = False
    block_on_close = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.release_hangs = threading.Event()
        self.handler_threads: list[threading.Thread] = []
        self._slots = threading.BoundedSemaphore(_MAX_HANDLER_THREADS)

    def process_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int] | str,
    ) -> None:
        if not self._slots.acquire(timeout=_HANG_LIMIT_SECONDS):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int] | str,
    ) -> None:
        self.handler_threads.append(threading.current_thread())
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class LocalErrorServer:
    def __init__(self) -> None:
        self._server = _BoundedServer()
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="local-error-server",
        )
        # Bound but never listening: connections are refused.
        self._refusing = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._refusing.bind(("127.0.0.1", 0))

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._server.release_hangs.set()
        self._server.shutdown()
        self._thread.join()
        self._server.server_close()
        self._refusing.close()

    def threads(self) -> tuple[threading.Thread, ...]:
        # The serving thread and every request-handler thread it started.
        return (self._thread, *self._server.handler_threads)

    @property
    def _base(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def status_url(self, status: int, *, code: int | None = None) -> str:
        suffix = "" if code is None else f"/code/{code}"
        return f"{self._base}/status/{status}{suffix}"

    def hang_url(self) -> str:
        return f"{self._base}/hang"

    def proxy_trap_url(self) -> str:
        return self._base

    def refused_url(self) -> str:
        port = self._refusing.getsockname()[1]
        return f"http://127.0.0.1:{port}"


# ------------------------------------------------------------------
# Factories: each returns the exception the real code actually raised
# ------------------------------------------------------------------


def _capture(action: Callable[[], object]) -> BaseException:
    try:
        action()
    except Exception as exc:
        return exc
    raise AssertionError("the failure was not produced")


def _requests_get(url: str, headers: dict[str, str] | None = None) -> object:
    # Local requests never go through environment proxies (HTTP_PROXY etc.).
    with requests.Session() as session:
        session.trust_env = False
        return session.get(url, headers=headers, timeout=_CLIENT_TIMEOUT_SECONDS)


def _curl_get(url: str, *, check_status: bool = False) -> None:
    # libcurl reads proxy variables itself; NOPROXY "*" bypasses them all.
    response = curl_requests.get(
        url,
        timeout=_CLIENT_TIMEOUT_SECONDS,
        curl_options={CurlOpt.NOPROXY: "*"},
    )
    if check_status:
        response.raise_for_status()


def invalid_header_values(secret: str = SECRET) -> dict[str, str | bytes]:
    # Every value has an interior CR or LF: requests accepts a trailing one.
    shapes = {
        "ascii_lf": f"{secret}\nX-Injected: 1",
        "ascii_cr": f"{secret}\rtail",
        "unicode_newline": f"{secret}é\nñ日",
        "backslash": f"{secret}\\path\nnext",
        "single_quote": f"{secret}'q\nnext",
        "double_quote": f'{secret}"q\nnext',
        "both_quotes": f"{secret}'\"\nnext",
        "control_bytes": f"{secret}\x00\x07\x1b\x7f\nnext",
        "long_600": f"{secret}\n".ljust(600, "A"),
        "embedded": f"Bearer prefix-{secret}-suffix\nX-Next: 1",
    }
    values: dict[str, str | bytes] = {}
    for shape, value in shapes.items():
        values[f"{shape}_str"] = value
        values[f"{shape}_bytes"] = value.encode("utf-8")
    return values


def requests_invalid_header(value: str | bytes) -> BaseException:
    return _capture(
        lambda: requests.Request(
            "GET",
            "http://127.0.0.1/v2/stocks/bars",
            headers={"APCA-API-KEY-ID": value},
        ).prepare()
    )


def http_client_invalid_header(
    server: LocalErrorServer,
    secret: str = SECRET,
) -> BaseException:
    # A trailing newline passes requests' check and fails in http.client.
    return _capture(
        lambda: _requests_get(
            server.status_url(200),
            headers={"APCA-API-KEY-ID": f"AK{secret}\n"},
        )
    )


def requests_connection_error(
    server: LocalErrorServer,
    secret: str = SECRET,
) -> BaseException:
    return _capture(
        lambda: _requests_get(f"{server.refused_url()}/v2/stocks/bars?token={secret}")
    )


def requests_timeout(server: LocalErrorServer, secret: str = SECRET) -> BaseException:
    return _capture(
        lambda: _requests_get(f"{server.hang_url()}?token={secret}")
    )


def requests_http_error(
    server: LocalErrorServer,
    status: int,
    secret: str = SECRET,
) -> BaseException:
    def failing_request() -> None:
        with requests.Session() as session:
            session.trust_env = False
            session.get(
                f"{server.status_url(status)}?token={secret}",
                timeout=_CLIENT_TIMEOUT_SECONDS,
            ).raise_for_status()

    return _capture(failing_request)


def curl_connection_error(
    server: LocalErrorServer,
    secret: str = SECRET,
) -> BaseException:
    return _capture(
        lambda: _curl_get(f"{server.refused_url()}/v2/stocks/bars?token={secret}")
    )


def curl_timeout(server: LocalErrorServer, secret: str = SECRET) -> BaseException:
    return _capture(
        lambda: _curl_get(f"{server.hang_url()}?token={secret}")
    )


def curl_http_error(
    server: LocalErrorServer,
    status: int,
    secret: str = SECRET,
) -> BaseException:
    return _capture(
        lambda: _curl_get(
            f"{server.status_url(status)}?token={secret}",
            check_status=True,
        )
    )


class _ShortTimeoutAdapter(HTTPAdapter):  # type: ignore[misc]
    def send(self, request: Any, **kwargs: Any) -> Any:
        kwargs["timeout"] = _CLIENT_TIMEOUT_SECONDS
        return super().send(request, **kwargs)


def _alpaca_client(
    base_url: str,
    api_key: str | bytes,
    secret: str,
) -> StockHistoricalDataClient:
    client = StockHistoricalDataClient(
        api_key=cast(str, api_key),
        secret_key=f"SK{secret}",
        url_override=base_url,
    )
    # Test-only: no 3 x 3 s alpaca-py waits on a 429, a short timeout, and
    # no environment proxies for the local server.
    client._retry = 0
    session = cast(requests.Session, client._session)
    session.trust_env = False
    session.mount("http://", _ShortTimeoutAdapter())
    return client


def alpaca_api_error(
    server: LocalErrorServer,
    status: int,
    secret: str = SECRET,
    *,
    code: int | None = None,
) -> BaseException:
    # The library's own error, straight from the real client.
    client = _alpaca_client(
        server.status_url(status, code=code), f"AK{secret}", secret
    )
    request = StockBarsRequest(
        symbol_or_symbols="AAPL",
        timeframe=TimeFrame(1, TimeFrameUnit.Day),
        start=_HISTORY_START,
        end=_HISTORY_END,
        feed=DataFeed.IEX,
    )
    return _capture(lambda: client.get_stock_bars(request))


def alpaca_provider(
    base_url: str,
    feed: MarketDataFeed = MarketDataFeed.IEX,
    secret: str = SECRET,
    *,
    api_key: str | bytes | None = None,
) -> AlpacaProvider:
    # The real provider over a real client pointed at the local server.
    key = f"AK{secret}" if api_key is None else api_key
    return AlpacaProvider(
        cast(str, key),
        f"SK{secret}",
        feed,
        client=cast(_MarketDataClient, _alpaca_client(base_url, key, secret)),
        display_name=ALPACA_DISPLAY_NAME,
    )


def alpaca_provider_failure(
    base_url: str,
    feed: MarketDataFeed = MarketDataFeed.IEX,
    secret: str = SECRET,
    *,
    api_key: str | bytes | None = None,
) -> BaseException:
    provider = alpaca_provider(base_url, feed, secret, api_key=api_key)
    return _capture(
        lambda: provider.fetch_bars(
            "AAPL", Timeframe.DAILY, _HISTORY_START, _HISTORY_END
        )
    )


def alpaca_entitlement_error(
    server: LocalErrorServer,
    secret: str = SECRET,
) -> BaseException:
    return alpaca_provider_failure(
        server.status_url(403, code=_ALPACA_ENTITLEMENT_CODE),
        MarketDataFeed.SIP,
        secret,
    )


def _raise_yfinance_rate_limit() -> None:
    raise YFRateLimitError()


def yfinance_rate_limit_error() -> BaseException:
    return _capture(_raise_yfinance_rate_limit)


class _TimezoneCache:
    def lookup(self, symbol: str) -> str:
        return "America/New_York"

    def store(self, symbol: str, timezone: str | None) -> None:
        pass


@contextmanager
def yahoo_transport(get: Callable[[], object]) -> Iterator[None]:
    # The real Ticker/history path runs; only yfinance's data I/O is
    # replaced, by `get`, and its timezone cache, so nothing reaches Yahoo
    # or the user's cache.
    def transport(
        data: YfData,
        url: str,
        params: object = None,
        timeout: float = 30,
    ) -> object:
        return get()

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(YfData, "get", transport))
        stack.enter_context(mock.patch.object(YfData, "cache_get", transport))
        stack.enter_context(
            mock.patch.object(yfinance.cache, "get_tz_cache", _TimezoneCache)
        )
        for name in ("_DFS", "_ERRORS", "_TRACEBACKS", "_ISINS"):
            stack.enter_context(mock.patch.object(yfinance.shared, name, {}))
        yield


def yahoo_provider_failure(get: Callable[[], object]) -> BaseException:
    provider = YFinanceProvider(display_name=YAHOO_DISPLAY_NAME)
    with yahoo_transport(get):
        return _capture(
            lambda: provider.fetch_bars(
                "AAPL", Timeframe.DAILY, _HISTORY_START, _HISTORY_END
            )
        )


def _yahoo_curl_failure(url: str) -> BaseException:
    # A real curl_cffi request, as yfinance makes, checked like yfinance's
    # get_raw_json.
    return yahoo_provider_failure(lambda: _curl_get(url, check_status=True))


class SecretLeakingKeyring:
    """Fake keyring backend: passes the availability probe, then fails
    real reads and writes with the secret in the backend message."""

    def __init__(self, secret: str, *, available: bool = True) -> None:
        self._secret = secret
        self._available = available
        self._probes: dict[str, str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        if self._available and username in self._probes:
            return self._probes[username]
        raise keyring.errors.KeyringError(
            f"Secret Service read failed for {service_name}/{username}: "
            f"cached secret {self._secret}"
        )

    def set_password(self, service_name: str, username: str, password: str) -> None:
        if self._available and username.startswith("availability-probe-"):
            self._probes[username] = password
            return
        raise keyring.errors.PasswordSetError(
            f"Secret Service rejected {service_name}/{username} value {password}"
        )

    def delete_password(self, service_name: str, username: str) -> None:
        self._probes.pop(username, None)


def keyring_read_error(secret: str = SECRET) -> BaseException:
    access = initialize_keyring_credential_store(SecretLeakingKeyring(secret))
    assert access.available
    return _capture(lambda: access.store.get("alpaca-paper"))


def keyring_write_error(secret: str = SECRET) -> BaseException:
    access = initialize_keyring_credential_store(SecretLeakingKeyring(secret))
    assert access.available
    credentials = ProviderCredentials(api_key_id=f"AK{secret}", api_secret=secret)
    return _capture(lambda: access.store.put("alpaca-paper", credentials))


def keyring_unavailable_error(secret: str = SECRET) -> BaseException:
    access = initialize_keyring_credential_store(
        SecretLeakingKeyring(secret, available=False)
    )
    assert not access.available
    return _capture(lambda: access.store.get("alpaca-paper"))


def _raise_unknown(secret: str) -> None:
    raise RuntimeError(f"unexpected failure with key {secret}")


def unknown_error(secret: str = SECRET) -> BaseException:
    return _capture(lambda: _raise_unknown(secret))


_INVALID_HEADER = invalid_header_values()

SOURCES: dict[str, Callable[[LocalErrorServer], BaseException]] = {
    "requests_invalid_header": lambda server: requests_invalid_header(
        _INVALID_HEADER["unicode_newline_str"]
    ),
    "requests_invalid_header_bytes": lambda server: requests_invalid_header(
        _INVALID_HEADER["unicode_newline_bytes"]
    ),
    "http_client_invalid_header": http_client_invalid_header,
    "requests_connection_error": requests_connection_error,
    "requests_timeout": requests_timeout,
    "requests_http_401": lambda server: requests_http_error(server, 401),
    "requests_http_403": lambda server: requests_http_error(server, 403),
    "requests_http_429": lambda server: requests_http_error(server, 429),
    "requests_http_500": lambda server: requests_http_error(server, 500),
    "curl_connection_error": curl_connection_error,
    "curl_timeout": curl_timeout,
    "curl_http_401": lambda server: curl_http_error(server, 401),
    "curl_http_403": lambda server: curl_http_error(server, 403),
    "curl_http_429": lambda server: curl_http_error(server, 429),
    "curl_http_500": lambda server: curl_http_error(server, 500),
    "alpaca_api_401": lambda server: alpaca_api_error(server, 401),
    "alpaca_api_403": lambda server: alpaca_api_error(server, 403),
    "alpaca_api_429": lambda server: alpaca_api_error(server, 429),
    "alpaca_api_entitlement": lambda server: alpaca_api_error(
        server, 403, code=_ALPACA_ENTITLEMENT_CODE
    ),
    "alpaca_entitlement": alpaca_entitlement_error,
    "yfinance_rate_limit": lambda server: yfinance_rate_limit_error(),
    "keyring_read": lambda server: keyring_read_error(),
    "keyring_write": lambda server: keyring_write_error(),
    "keyring_unavailable": lambda server: keyring_unavailable_error(),
    "unknown": lambda server: unknown_error(),
}


# Each real-world failure as SimpleChart's real provider methods surface it.
PROVIDER_SOURCES: dict[str, Callable[[LocalErrorServer], BaseException]] = {
    "alpaca_invalid_header": lambda server: alpaca_provider_failure(
        server.status_url(200), api_key=_INVALID_HEADER["unicode_newline_str"]
    ),
    "alpaca_invalid_header_bytes": lambda server: alpaca_provider_failure(
        server.status_url(200), api_key=_INVALID_HEADER["unicode_newline_bytes"]
    ),
    # A trailing newline fails in http.client as a plain ValueError.
    "alpaca_http_client_invalid_header": lambda server: alpaca_provider_failure(
        server.status_url(200), api_key=f"AK{SECRET}\n"
    ),
    "alpaca_connection_error": lambda server: alpaca_provider_failure(
        server.refused_url()
    ),
    "alpaca_timeout": lambda server: alpaca_provider_failure(server.hang_url()),
    "alpaca_api_401": lambda server: alpaca_provider_failure(server.status_url(401)),
    "alpaca_api_403": lambda server: alpaca_provider_failure(server.status_url(403)),
    "alpaca_api_429": lambda server: alpaca_provider_failure(server.status_url(429)),
    "alpaca_api_500": lambda server: alpaca_provider_failure(server.status_url(500)),
    "alpaca_api_entitlement": lambda server: alpaca_provider_failure(
        server.status_url(403, code=_ALPACA_ENTITLEMENT_CODE)
    ),
    "alpaca_entitlement": alpaca_entitlement_error,
    "alpaca_delayed_sip_entitlement": lambda server: alpaca_provider_failure(
        server.status_url(403, code=_ALPACA_ENTITLEMENT_CODE),
        MarketDataFeed.DELAYED_SIP,
    ),
    "yfinance_connection_error": lambda server: _yahoo_curl_failure(
        f"{server.refused_url()}/v8/finance/chart/AAPL?token={SECRET}"
    ),
    "yfinance_timeout": lambda server: _yahoo_curl_failure(
        f"{server.hang_url()}?token={SECRET}"
    ),
    "yfinance_http_401": lambda server: _yahoo_curl_failure(
        f"{server.status_url(401)}?token={SECRET}"
    ),
    "yfinance_http_403": lambda server: _yahoo_curl_failure(
        f"{server.status_url(403)}?token={SECRET}"
    ),
    "yfinance_http_429": lambda server: _yahoo_curl_failure(
        f"{server.status_url(429)}?token={SECRET}"
    ),
    "yfinance_http_500": lambda server: _yahoo_curl_failure(
        f"{server.status_url(500)}?token={SECRET}"
    ),
    "yfinance_rate_limit": lambda server: yahoo_provider_failure(
        _raise_yfinance_rate_limit
    ),
}
