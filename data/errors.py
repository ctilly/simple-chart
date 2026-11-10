"""
data/errors.py

Shared error vocabulary for every layer.

- UserFacingError: an exception whose message SimpleChart composed from
  app-controlled values; it is the only exception text that may be shown.
- describe(): the only way exception details are written. It renders safe
  structured facts (type names along the cause chain, HTTP status, code
  locations) and never the text of any other exception.
- mask_patterns(): the pattern backstop for text SimpleChart writes but did
  not compose. It is a safety net, not the guarantee.
"""

import re


class UserFacingError(Exception):
    pass


_MAX_CHAIN_DEPTH = 20
_UNKNOWN = "<unknown>"

# Read class names and the exception chain through the base descriptors, so
# metaclass lookups and subclass overrides of these attributes never run.
_CLASS_MODULE = type.__dict__["__module__"]
_CLASS_QUALNAME = type.__dict__["__qualname__"]
_CAUSE = BaseException.__dict__["__cause__"]
_CONTEXT = BaseException.__dict__["__context__"]
_SUPPRESS_CONTEXT = BaseException.__dict__["__suppress_context__"]
_TRACEBACK = BaseException.__dict__["__traceback__"]


def describe(exc: BaseException) -> str:
    try:
        return _describe(exc)
    except Exception:
        return _type_name_or_fallback(exc)


def _describe(exc: BaseException) -> str:
    lines: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    relation = ""
    while current is not None:
        if id(current) in seen:
            lines.append("(cycle in exception chain)")
            break
        if len(seen) == _MAX_CHAIN_DEPTH:
            lines.append("(exception chain truncated)")
            break
        seen.add(id(current))
        lines.append(f"{relation}{_header(current, current is exc)}")
        lines.extend(_locations(current))
        cause = _CAUSE.__get__(current)
        context = _CONTEXT.__get__(current)
        if cause is not None:
            relation = "caused by "
            current = cause
        elif context is not None and not _SUPPRESS_CONTEXT.__get__(current):
            relation = "while handling "
            current = context
        else:
            current = None
    return "\n".join(lines)


def _header(exc: BaseException, is_root: bool) -> str:
    header = _type_name(exc)
    status = http_status(exc)
    if status is not None:
        header += f" (HTTP {status})"
    # Only the described exception's own text; a UserFacingError deeper in
    # the chain is named only. issubclass on the real type: an instance
    # cannot claim the class.
    if is_root and issubclass(type(exc), UserFacingError):
        header += f": {str(exc)}"
    return header


def _type_name(exc: BaseException) -> str:
    cls = type(exc)
    module = _plain_text(_CLASS_MODULE.__get__(cls))
    qualname = _plain_text(_CLASS_QUALNAME.__get__(cls))
    if module == "builtins":
        return qualname
    return f"{module}.{qualname}"


def _plain_text(value: object) -> str:
    # Anything but an exact str could run code when formatted.
    return value if type(value) is str else _UNKNOWN


def _type_name_or_fallback(exc: BaseException) -> str:
    try:
        return _type_name(exc)
    except Exception:
        return "exception"


def http_status(exc: BaseException) -> int | None:
    # requests and curl_cffi errors carry `response`; alpaca-py's APIError
    # exposes the underlying requests response the same way. An error
    # response is falsy in requests, so test against None.
    # object.__getattribute__ never falls back to a class's __getattr__.
    try:
        response = object.__getattribute__(exc, "response")
        if response is None:
            return None
        status = object.__getattribute__(response, "status_code")
    except Exception:
        return None
    # curl_cffi reports status 0 on connection failures.
    if type(status) is int and 100 <= status <= 599:
        return status
    return None


def _locations(exc: BaseException) -> list[str]:
    locations: list[str] = []
    tb = _TRACEBACK.__get__(exc)
    while tb is not None:
        code = tb.tb_frame.f_code
        filename = _plain_text(code.co_filename)
        function = _plain_text(code.co_qualname)
        locations.append(f"  at {filename}:{tb.tb_lineno} in {function}")
        tb = tb.tb_next
    return locations


# Values of Alpaca key headers, Authorization, and key/secret/token/password
# pairs, in header, query-string, dict-repr and JSON shapes. A quoted value
# may be a bytes literal (b'...').
_SENSITIVE_VALUE = re.compile(
    r"""
    (?<![\w-])
    (?P<name>
        apca-api-key-id | apca-api-secret-key | authorization
      | [\w-]*(?:key|secret|token|password)
    )
    (?P<separator>["']?\s*[:=]\s*)
    (?P<value>
        b?"(?:\\.|[^"\\])*"?
      | b?'(?:\\.|[^'\\])*'?
      | (?:(?:bearer|basic|token)\s+)?[^\s"',;&)}\]]+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def mask_patterns(text: str) -> str:
    try:
        return _SENSITIVE_VALUE.sub(r"\g<name>\g<separator>[redacted]", text)
    except Exception:
        return "[withheld]"
