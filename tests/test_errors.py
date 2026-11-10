import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, cast

import pytest
import requests  # type: ignore[import-untyped]

import data.errors
from chart.window import ChartWidget
from data.cache import Cache
from data.errors import UserFacingError, describe, mask_patterns
from data.models import Bar, BarCorrection, Timeframe
from realistic_errors import (
    SECRET,
    SOURCES,
    LocalErrorServer,
    assert_secret_absent,
    requests_invalid_header,
)


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

_SPY_SESSION = datetime(2026, 2, 2, 5, 0, tzinfo=UTC)

# Every line describe writes is a type header or a code location: no source
# lines, locals or messages.
_LINE_SHAPE = re.compile(
    r"((caused by|while handling) )?[\w.]+( \(HTTP \d{3}\))?"
    r"|  at \S.*:\d+ in [\w.<>]+"
)


@pytest.fixture(scope="module")
def error_server() -> Iterator[LocalErrorServer]:
    with LocalErrorServer() as server:
        yield server


def _type_name(exc: BaseException) -> str:
    cls = type(exc)
    if cls.__module__ == "builtins":
        return cls.__qualname__
    return f"{cls.__module__}.{cls.__qualname__}"


def _chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return chain


def _innermost(tb: TracebackType) -> TracebackType:
    while tb.tb_next is not None:
        tb = tb.tb_next
    return tb


def _raise_and_catch(exc: BaseException) -> BaseException:
    try:
        raise exc
    except BaseException as caught:
        return caught


# ------------------------------------------------------------------
# describe: real errors
# ------------------------------------------------------------------


@pytest.mark.parametrize("label", sorted(SOURCES))
def test_describe_real_error_writes_safe_facts_only(
    error_server: LocalErrorServer,
    label: str,
) -> None:
    exc = SOURCES[label](error_server)

    output = describe(exc)

    for member in _chain(exc):
        assert _type_name(member) in output
        assert member.__traceback__ is not None
        frame = _innermost(member.__traceback__)
        location = (
            f"{frame.tb_frame.f_code.co_filename}:{frame.tb_lineno} "
            f"in {frame.tb_frame.f_code.co_qualname}"
        )
        assert location in output
        message = str(member)
        # Only the described exception's own UserFacingError text is written.
        if message and not (member is exc and isinstance(exc, UserFacingError)):
            assert message not in output
    lines = output.splitlines()
    # A UserFacingError head carries its own text; it is checked exactly below.
    for line in lines[1:] if isinstance(exc, UserFacingError) else lines:
        assert _LINE_SHAPE.fullmatch(line), line
    head = lines[0]
    status = _EXPECTED_STATUS.get(label)
    if isinstance(exc, UserFacingError):
        assert head == f"{_type_name(exc)}: {exc}"
    elif status is None:
        assert head == _type_name(exc)
    else:
        assert head == f"{_type_name(exc)} (HTTP {status})"
    if label == "alpaca_entitlement":
        assert "caused by alpaca.common.exceptions.APIError (HTTP 403)" in output
    elif status is None:
        assert "(HTTP " not in output
    assert_secret_absent(output)


# ------------------------------------------------------------------
# describe: chains and hostile exceptions
# ------------------------------------------------------------------


class _Outer(Exception):
    pass


class _Inner(Exception):
    pass


def test_describe_follows_explicit_cause() -> None:
    try:
        try:
            raise _Inner(SECRET)
        except _Inner as inner:
            raise _Outer(SECRET) from inner
    except _Outer as caught:
        outer: BaseException = caught
    output = describe(outer)

    assert output.index(_type_name(outer)) < output.index("caused by")
    assert f"caused by {_type_name(outer.__cause__ or outer)}" in output
    assert_secret_absent(output)


def test_describe_follows_implicit_context() -> None:
    try:
        try:
            raise _Inner(SECRET)
        except _Inner:
            raise _Outer(SECRET)
    except _Outer as caught:
        outer = caught
    output = describe(outer)

    assert f"while handling {_type_name(outer.__context__ or outer)}" in output
    assert_secret_absent(output)


def test_describe_respects_suppressed_context() -> None:
    try:
        try:
            raise _Inner(SECRET)
        except _Inner:
            raise _Outer(SECRET) from None
    except _Outer as caught:
        output = describe(caught)

    assert "_Inner" not in output
    assert "_Outer" in output


def test_describe_stops_at_a_cycle() -> None:
    first = _raise_and_catch(_Outer(SECRET))
    second = _raise_and_catch(_Inner(SECRET))
    first.__context__ = second
    second.__context__ = first

    output = describe(first)

    assert output.count("_Outer") == 1
    assert output.count("_Inner") == 1
    assert "(cycle in exception chain)" in output


def test_describe_stops_at_a_self_cause() -> None:
    exc = _raise_and_catch(_Outer(SECRET))
    exc.__cause__ = exc

    output = describe(exc)

    assert output.count("_Outer") == 1
    assert "(cycle in exception chain)" in output


class _Link(Exception):
    pass


def test_describe_caps_chain_depth_at_twenty() -> None:
    head = _Link(SECRET)
    current = head
    for _ in range(30):
        cause = _Link(SECRET)
        current.__cause__ = cause
        current = cause

    output = describe(head)

    assert output.count("_Link") == 20
    assert "(exception chain truncated)" in output
    assert_secret_absent(output)


class _CountingError(Exception):
    calls = 0

    def __str__(self) -> str:
        _CountingError.calls += 1
        return SECRET

    def __repr__(self) -> str:
        _CountingError.calls += 1
        return SECRET

    def __format__(self, spec: str) -> str:
        _CountingError.calls += 1
        return SECRET


def test_describe_never_calls_str_repr_or_format() -> None:
    _CountingError.calls = 0
    try:
        try:
            raise _CountingError()
        except _CountingError as inner:
            raise _CountingError() from inner
    except _CountingError as caught:
        output = describe(caught)

    assert _CountingError.calls == 0
    assert output.count("_CountingError") == 2


class _RaisingError(Exception):
    def __str__(self) -> str:
        raise RuntimeError(SECRET)

    def __repr__(self) -> str:
        raise RuntimeError(SECRET)


def test_describe_handles_exceptions_whose_str_and_repr_raise() -> None:
    exc = _raise_and_catch(_RaisingError())

    output = describe(exc)

    assert "_RaisingError" in output
    assert " at " in output
    assert_secret_absent(output)


class _HostileResponseError(Exception):
    @property
    def response(self) -> Any:
        raise RuntimeError(SECRET)


def test_describe_survives_a_response_attribute_that_raises() -> None:
    exc = _raise_and_catch(_HostileResponseError())

    output = describe(exc)

    assert "_HostileResponseError" in output
    assert " at " in output
    assert "(HTTP " not in output


def test_describe_ignores_notes() -> None:
    exc = _Outer(SECRET)
    exc.add_note(f"note {SECRET}")
    caught = _raise_and_catch(exc)

    output = describe(caught)

    assert "note" not in output
    assert_secret_absent(output)


def test_describe_does_not_render_exception_group_messages() -> None:
    group = ExceptionGroup(f"batch {SECRET}", [ValueError(SECRET), _Inner(SECRET)])
    caught = _raise_and_catch(group)

    output = describe(caught)

    assert "ExceptionGroup" in output
    assert "batch" not in output
    assert_secret_absent(output)


def test_describe_returns_type_name_when_rendering_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(exc: BaseException) -> list[str]:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(data.errors, "_locations", broken)
    exc = _raise_and_catch(_Outer(SECRET))

    assert describe(exc) == _type_name(exc)


class _CountingStr(str):
    calls = 0

    def __str__(self) -> str:
        _CountingStr.calls += 1
        return SECRET

    def __repr__(self) -> str:
        _CountingStr.calls += 1
        return SECRET

    def __format__(self, spec: str) -> str:
        _CountingStr.calls += 1
        return SECRET


class _CountingInt(int):
    calls = 0

    def __str__(self) -> str:
        _CountingInt.calls += 1
        return SECRET

    def __repr__(self) -> str:
        _CountingInt.calls += 1
        return SECRET

    def __format__(self, spec: str) -> str:
        _CountingInt.calls += 1
        return SECRET


@pytest.fixture
def reset_counters() -> None:
    _CountingError.calls = 0
    _CountingStr.calls = 0
    _CountingInt.calls = 0


def _total_calls() -> int:
    return _CountingError.calls + _CountingStr.calls + _CountingInt.calls


@pytest.mark.usefixtures("reset_counters")
@pytest.mark.parametrize(
    "module",
    [ValueError(SECRET), _CountingError(), _CountingStr("plugin.module")],
    ids=["value_error", "counting_error", "str_subclass"],
)
def test_describe_never_renders_a_module_that_is_not_a_plain_str(
    module: object,
) -> None:
    cls = type("DynamicError", (Exception,), {"__module__": module})
    assert cls.__dict__["__module__"] is module
    exc = _raise_and_catch(cls())

    output = describe(exc)

    assert output.splitlines()[0] == "<unknown>.DynamicError"
    assert_secret_absent(output)
    assert _total_calls() == 0


@pytest.mark.usefixtures("reset_counters")
def test_describe_never_renders_a_qualname_that_is_not_a_plain_str() -> None:
    cls = type(
        "DynamicError",
        (Exception,),
        {"__module__": "plugin.module", "__qualname__": _CountingStr("Dynamic")},
    )
    assert type(cls.__qualname__) is _CountingStr
    _CountingStr.calls = 0
    exc = _raise_and_catch(cls())

    output = describe(exc)

    assert output.splitlines()[0] == "plugin.module.<unknown>"
    assert_secret_absent(output)
    assert _total_calls() == 0


class _CountingMeta(type):
    lookups = 0

    def __getattribute__(cls, name: str) -> Any:
        if name in ("__module__", "__qualname__"):
            _CountingMeta.lookups += 1
        return super().__getattribute__(name)


class _MetaError(Exception, metaclass=_CountingMeta):
    pass


def test_describe_reads_names_without_metaclass_attribute_lookup() -> None:
    exc = _raise_and_catch(_MetaError(SECRET))
    expected = _type_name(exc)
    _CountingMeta.lookups = 0

    output = describe(exc)

    assert _CountingMeta.lookups == 0
    assert output.splitlines()[0] == expected


class _HostileGetattrError(_CountingError):
    def __getattr__(self, name: str) -> Any:
        return str(self)


@pytest.mark.usefixtures("reset_counters")
def test_describe_response_lookup_never_reaches_a_hostile_getattr() -> None:
    exc = _raise_and_catch(_HostileGetattrError())

    output = describe(exc)

    assert "_HostileGetattrError" in output
    assert "(HTTP " not in output
    assert_secret_absent(output)
    assert _total_calls() == 0


class _ResponseError(Exception):
    def __init__(self, response: object) -> None:
        super().__init__(SECRET)
        self.response = response


class _StatusResponse:
    def __init__(self, status_code: object) -> None:
        self.status_code = status_code


@pytest.mark.usefixtures("reset_counters")
@pytest.mark.parametrize(
    "status_code",
    [_CountingInt(401), True, 401.0, "401", _CountingStr("401")],
    ids=["int_subclass", "bool", "float", "str", "str_subclass"],
)
def test_describe_accepts_only_a_plain_int_status(status_code: object) -> None:
    exc = _raise_and_catch(_ResponseError(_StatusResponse(status_code)))

    output = describe(exc)

    assert "(HTTP " not in output
    assert_secret_absent(output)
    assert _total_calls() == 0


def test_describe_renders_a_plain_int_status() -> None:
    exc = _raise_and_catch(_ResponseError(_StatusResponse(401)))

    assert describe(exc).splitlines()[0].endswith("_ResponseError (HTTP 401)")


class _PropertyChainError(_CountingError):
    @property
    def __cause__(self) -> Any:  # type: ignore[override]
        return str(self)

    @property
    def __context__(self) -> Any:  # type: ignore[override]
        return str(self)


@pytest.mark.usefixtures("reset_counters")
def test_describe_reads_the_chain_without_subclass_overrides() -> None:
    try:
        try:
            raise _Inner(SECRET)
        except _Inner as inner:
            raise _PropertyChainError() from inner
    except _PropertyChainError as caught:
        outer: BaseException = caught

    output = describe(outer)

    assert f"caused by {_type_name(_Inner())}" in output
    assert_secret_absent(output)
    assert _total_calls() == 0


@pytest.mark.usefixtures("reset_counters")
def test_describe_fallback_never_renders_hostile_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(exc: BaseException) -> list[str]:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(data.errors, "_locations", broken)
    cls = type(
        "DynamicError",
        (Exception,),
        {"__module__": _CountingError(), "__qualname__": _CountingStr("Dynamic")},
    )
    _CountingStr.calls = 0
    exc = _raise_and_catch(cls())

    output = describe(exc)

    assert output == "<unknown>.<unknown>"
    assert _total_calls() == 0


# ------------------------------------------------------------------
# describe: UserFacingError
# ------------------------------------------------------------------


def test_describe_renders_user_facing_text() -> None:
    exc = _raise_and_catch(UserFacingError("Pick a different bar."))

    output = describe(exc)

    assert "data.errors.UserFacingError: Pick a different bar." in output


def test_describe_renders_only_the_user_facing_text_not_its_library_cause() -> None:
    library_error = requests_invalid_header(f"AK{SECRET}\nX-Injected: 1")
    try:
        raise UserFacingError("The saved key is malformed.") from library_error
    except UserFacingError as caught:
        output = describe(caught)

    assert "UserFacingError: The saved key is malformed." in output
    assert "caused by requests.exceptions.InvalidHeader" in output
    assert "Invalid leading whitespace" not in output
    assert_secret_absent(output)


def test_describe_renders_user_facing_text_only_for_the_root_exception() -> None:
    try:
        try:
            raise UserFacingError("Corrected volume must be a nonnegative integer.")
        except UserFacingError as inner:
            raise _Outer(SECRET) from inner
    except _Outer as caught:
        outer = caught
    output = describe(outer)

    assert "caused by data.errors.UserFacingError" in output.splitlines()
    assert "Corrected volume" not in output
    assert_secret_absent(output)


class _SpoofedClassError(Exception):
    @property  # type: ignore[misc]
    def __class__(self) -> type:
        return UserFacingError


def test_describe_does_not_trust_a_spoofed_class() -> None:
    exc = _raise_and_catch(_SpoofedClassError(SECRET))

    output = describe(exc)

    assert_secret_absent(output)


# ------------------------------------------------------------------
# mask_patterns
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"APCA-API-KEY-ID: {SECRET}",
        f"apca-api-secret-key: {SECRET}",
        f"{{'APCA-API-KEY-ID': '{SECRET}', 'User-Agent': 'APCA-PY/0.44'}}",
        f'{{"APCA-API-SECRET-KEY": "{SECRET}"}}',
        f"APCA-API-KEY-ID={SECRET}",
        f"Authorization: Bearer {SECRET}",
        f"authorization: Basic {SECRET}",
        f"'Authorization': 'Bearer {SECRET}'",
        f"key={SECRET}",
        f"KEY: {SECRET}",
        f"api_key={SECRET}&symbol=AAPL",
        f"https://example.test/v1?apikey={SECRET}&range=1d",
        f"secret={SECRET}",
        f"Secret : {SECRET}",
        f"secret_key='{SECRET}'",
        f"token={SECRET}",
        f"Access-Token: {SECRET}",
        f'"refresh_token": "{SECRET}"',
        f"password={SECRET}",
        f"PASSWORD: {SECRET}",
        f"password = '{SECRET}'",
        f"token: 'a\\'b{SECRET}'",
    ],
)
def test_mask_patterns_masks_sensitive_values(text: str) -> None:
    masked = mask_patterns(text)

    assert_secret_absent(masked)
    assert "[redacted]" in masked


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f"{{'APCA-API-KEY-ID': b'{SECRET}', 'User-Agent': 'APCA-PY/0.44'}}",
            "{'APCA-API-KEY-ID': [redacted], 'User-Agent': 'APCA-PY/0.44'}",
        ),
        (
            f'{{"APCA-API-SECRET-KEY": b"{SECRET}"}}',
            '{"APCA-API-SECRET-KEY": [redacted]}',
        ),
        (
            f"{{'Authorization': b'Bearer {SECRET}'}}",
            "{'Authorization': [redacted]}",
        ),
        (
            f'Authorization: B"Basic {SECRET}\\x00"',
            "Authorization: [redacted]",
        ),
        (
            f"APCA-API-KEY-ID: b'\\xc3\\xa9{SECRET}\\n'",
            "APCA-API-KEY-ID: [redacted]",
        ),
        (
            f'apca-api-secret-key: b"{SECRET}\\r\\nX-Next: 1"',
            "apca-api-secret-key: [redacted]",
        ),
        (
            f"api_secret=b'ab\\'{SECRET}' next",
            "api_secret=[redacted] next",
        ),
        (
            f'password: b"q\\"{SECRET}\\\\"',
            "password: [redacted]",
        ),
    ],
)
def test_mask_patterns_masks_bytes_valued_credentials(
    text: str,
    expected: str,
) -> None:
    masked = mask_patterns(text)

    assert masked == expected
    assert_secret_absent(masked)


def test_mask_patterns_keeps_names_and_surrounding_text() -> None:
    masked = mask_patterns(f"GET /v2/stocks/bars api_key={SECRET}&symbol=AAPL")

    assert masked == "GET /v2/stocks/bars api_key=[redacted]&symbol=AAPL"


@pytest.mark.parametrize(
    "text",
    [
        "AAPL",
        "Loading TRAX 5m history",
        "close=189.50 high=191.25 volume=79286521",
        "2026-11-09T14:30:00Z",
        "https://data.alpaca.markets/v2/stocks/bars?symbols=AAPL&timeframe=5Min",
        "HTTP 401",
        "Alpaca returned HTTP 429; alpaca-py waits and retries",
        "/home/user/.simplechart/cache.sqlite3",
        "C:\\Users\\trader\\simplechart\\plugins\\keys.py",
        "Rate limiter has 7 tokens in reserve",
        "keyboard shortcut",
        "authorization was rejected",
        "",
    ],
)
def test_mask_patterns_leaves_ordinary_text_unchanged(text: str) -> None:
    assert mask_patterns(text) == text


def test_mask_patterns_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Broken:
        def sub(self, *args: object) -> str:
            raise RuntimeError(SECRET)

    monkeypatch.setattr(data.errors, "_SENSITIVE_VALUE", _Broken())

    result = mask_patterns(f"token={SECRET}")

    assert_secret_absent(result)


# ------------------------------------------------------------------
# UserFacingError subclasses at existing guidance sites
# ------------------------------------------------------------------


def _bar(timestamp: datetime, *, low: float = 685.77) -> Bar:
    return Bar(
        timestamp=timestamp,
        open=685.9,
        high=693.21,
        low=low,
        close=691.7,
        volume=79_286_521,
        vwap=691.25,
    )


@pytest.mark.parametrize(
    ("correction", "message"),
    [
        (
            {"high": 680.0},
            "Corrected high cannot be below open or close.",
        ),
        ({}, "A bar correction must override at least one field."),
        (
            {"low": 68.64},
            "A bar correction must differ from the provider bar.",
        ),
        (
            {"close": float("nan")},
            "Corrected prices must be positive and finite.",
        ),
    ],
)
def test_bar_correction_guidance_is_user_facing_and_still_a_value_error(
    tmp_path: Path,
    correction: dict[str, Any],
    message: str,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [_bar(_SPY_SESSION, low=68.64)],
        )
        with pytest.raises(ValueError) as raised:
            cache.put_bar_correction(
                BarCorrection(
                    cache_namespace="alpaca:delayed_sip",
                    symbol="SPY",
                    timeframe=Timeframe.DAILY,
                    timestamp=_SPY_SESSION,
                    **correction,
                )
            )

    assert isinstance(raised.value, UserFacingError)
    assert str(raised.value) == message


def test_bar_correction_on_missing_bar_is_user_facing(tmp_path: Path) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        with pytest.raises(ValueError) as raised:
            cache.put_bar_correction(
                BarCorrection(
                    cache_namespace="alpaca:delayed_sip",
                    symbol="SPY",
                    timeframe=Timeframe.DAILY,
                    timestamp=_SPY_SESSION,
                    low=685.77,
                )
            )

    assert isinstance(raised.value, UserFacingError)
    assert str(raised.value) == "The provider bar to correct does not exist."


def test_provider_bar_refresh_guidance_is_user_facing(tmp_path: Path) -> None:
    other_session = datetime(2026, 2, 3, 5, 0, tzinfo=UTC)
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "SPY",
            Timeframe.DAILY,
            [_bar(_SPY_SESSION)],
        )
        with pytest.raises(ValueError) as mismatch:
            cache.refresh_provider_bar(
                "alpaca:delayed_sip",
                "SPY",
                Timeframe.DAILY,
                _SPY_SESSION,
                _bar(other_session),
            )
        with pytest.raises(ValueError) as missing:
            cache.refresh_provider_bar(
                "alpaca:delayed_sip",
                "SPY",
                Timeframe.DAILY,
                other_session,
                _bar(other_session),
            )
        with pytest.raises(ValueError) as invalid:
            cache.refresh_provider_bar(
                "alpaca:delayed_sip",
                "SPY",
                Timeframe.DAILY,
                _SPY_SESSION,
                _bar(_SPY_SESSION, low=700.0),
            )

    assert isinstance(mismatch.value, UserFacingError)
    assert str(mismatch.value) == (
        "The refreshed provider bar does not match the same bar session."
    )
    assert isinstance(missing.value, UserFacingError)
    assert str(missing.value) == "The provider bar to refresh does not exist."
    assert isinstance(invalid.value, UserFacingError)
    assert str(invalid.value) == "Corrected low cannot be above open or close."


def test_timezone_naive_timestamp_stays_an_internal_value_error(
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        with pytest.raises(ValueError) as raised:
            cache.put_bar_correction(
                BarCorrection(
                    cache_namespace="alpaca:delayed_sip",
                    symbol="SPY",
                    timeframe=Timeframe.DAILY,
                    timestamp=datetime(2026, 2, 2, 5, 0),
                    low=685.77,
                )
            )

    assert not isinstance(raised.value, UserFacingError)


def test_panel_limit_is_user_facing_and_still_a_runtime_error() -> None:
    # The limit branch reads only the slot names. A real ChartWidget here
    # crashed the interpreter once leftover windows from
    # test_chart_load_lifecycle were garbage-collected (pre-existing hazard).
    occupied = SimpleNamespace(
        _extension_slots=[
            SimpleNamespace(name=name) for name in ("rsi", "macd", "volume_profile")
        ]
    )

    with pytest.raises(RuntimeError) as raised:
        ChartWidget.ensure_extension_panel(cast(ChartWidget, occupied), "stochastic")

    assert isinstance(raised.value, UserFacingError)
    assert str(raised.value) == (
        "All 3 extension panel slots are in use. "
        "Remove an existing panel extension before adding another."
    )


def test_requests_invalid_header_is_not_user_facing() -> None:
    exc = requests_invalid_header(f"AK{SECRET}\nX")

    assert isinstance(exc, requests.exceptions.InvalidHeader)
    assert not isinstance(exc, UserFacingError)
