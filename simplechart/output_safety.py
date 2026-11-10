"""
simplechart/output_safety.py

Process-wide output safety. install() runs first in both entry points and
routes every process output channel through safe rendering:

- Logging: every record is replaced by a sanitized copy before any handler
  sees it. Message text is kept only for records whose source file is
  SimpleChart's own code or a user plugin (with the pattern backstop
  applied); anything else is written as metadata only. Exceptions,
  including those inside list, tuple, set and mapping arguments, are
  rendered with describe(), never their text or traceback.
- warnings, Qt messages: metadata only (category or type, file:line).
- Unraisable, thread and unhandled exceptions: describe() output. An
  unhandled exception ends the process with status 1.

Every hook guards its own output so a failing write never raises.
"""

import logging
import os
import re
import sys
import threading
import warnings
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from types import TracebackType
from typing import TextIO

from PyQt6.QtCore import QMessageLogContext, QtMsgType, qInstallMessageHandler

from data.errors import describe, mask_patterns
from simplechart.plugins import _USER_PLUGIN_DIR


_LOG_FORMAT = "%(name)s %(levelname)s at %(filename)s:%(lineno)d %(message)s"
_MESSAGE_WITHHELD = "(message withheld)"
_VALUE_WITHHELD = "[withheld]"
_OUTPUT_FAILED = "logging output failed\n"
_MAX_NESTING = 10
_MAX_ITEMS = 1000

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TRUSTED_DIRECTORIES = tuple(
    os.path.join(os.path.realpath(directory), "")
    for directory in (
        *(
            _PROJECT_ROOT / package
            for package in ("app", "chart", "data", "simplechart", "tools", "indicators")
        ),
        _USER_PLUGIN_DIR,
    )
)
_TRUSTED_FILES = (os.path.realpath(_PROJECT_ROOT / "main.py"),)

_STANDARD_ATTRIBUTES = frozenset(
    vars(logging.LogRecord("", logging.NOTSET, "", 0, "", (), None))
)
_STACK_FRAME = re.compile(
    r'^  File "(?P<file>.*)", line (?P<line>\d+), in (?P<function>.*)$',
    re.MULTILINE,
)

_ORIGINAL_CALL_HANDLERS = logging.Logger.callHandlers
_installed = False
_qt_handler_state = threading.local()


class _SanitizedRecord(logging.LogRecord):
    pass


@lru_cache(maxsize=1024)
def _is_trusted_path(pathname: str) -> bool:
    if not os.path.isabs(pathname):
        return False
    path = os.path.realpath(pathname)
    return path in _TRUSTED_FILES or path.startswith(_TRUSTED_DIRECTORIES)


def _is_trusted(pathname: object) -> bool:
    return type(pathname) is str and _is_trusted_path(pathname)


class _BoundExceeded(Exception):
    pass


def _without_exceptions(value: object) -> object:
    # Interpolation formats containers by their items, so exceptions inside
    # them are described too. Past the bounds (or on a cycle) the whole value
    # is withheld.
    try:
        return _replace_exceptions(value, 0, [_MAX_ITEMS])
    except Exception:
        return _VALUE_WITHHELD


def _replace_exceptions(value: object, depth: int, budget: list[int]) -> object:
    if isinstance(value, BaseException):
        return describe(value)
    if not isinstance(value, list | tuple | set | frozenset | Mapping):
        return value
    budget[0] -= len(value)
    if depth == _MAX_NESTING or budget[0] < 0:
        raise _BoundExceeded
    if isinstance(value, Mapping):
        pairs = list(value.items())
        new_pairs = [
            (
                _replace_exceptions(key, depth + 1, budget),
                _replace_exceptions(item, depth + 1, budget),
            )
            for key, item in pairs
        ]
        if all(
            new[0] is old[0] and new[1] is old[1] for new, old in zip(new_pairs, pairs)
        ):
            return value
        return dict(new_pairs)
    items = list(value)
    new_items = [_replace_exceptions(item, depth + 1, budget) for item in items]
    # An unchanged container keeps its own type and formatting.
    if all(new is old for new, old in zip(new_items, items)):
        return value
    if isinstance(value, list):
        return new_items
    if isinstance(value, tuple):
        return tuple(new_items)
    if isinstance(value, frozenset):
        return frozenset(new_items)
    return set(new_items)


def _args_without_exceptions(args: object) -> object:
    # A mapping is logging's single-mapping form: one value, one budget.
    if isinstance(args, tuple):
        return tuple(_without_exceptions(value) for value in args)
    return _without_exceptions(args)


def _stack_locations(stack_info: str) -> str:
    return "stack:\n" + "\n".join(
        f"  at {match['file']}:{match['line']} in {match['function']}"
        for match in _STACK_FRAME.finditer(stack_info)
    )


def _sanitized(record: logging.LogRecord) -> logging.LogRecord:
    if type(record) is _SanitizedRecord:
        return record
    copy = _SanitizedRecord.__new__(_SanitizedRecord)
    for name, value in vars(record).items():
        if name in _STANDARD_ATTRIBUTES:
            copy.__dict__[name] = value
        elif isinstance(value, BaseException):
            copy.__dict__[name] = describe(value)
        else:
            copy.__dict__[name] = _VALUE_WITHHELD

    if _is_trusted(record.pathname):
        copy.msg = _without_exceptions(record.msg)
        copy.args = _args_without_exceptions(record.args)  # type: ignore[assignment]
        try:
            text = mask_patterns(copy.getMessage())
        except Exception:
            text = _MESSAGE_WITHHELD
    else:
        text = _MESSAGE_WITHHELD

    exc_info = record.exc_info
    exc = exc_info[1] if exc_info else None
    if isinstance(exc, BaseException):
        text += "\n" + describe(exc)
    if isinstance(record.stack_info, str):
        text += "\n" + _stack_locations(record.stack_info)

    copy.msg = text
    copy.args = ()
    copy.exc_info = None
    copy.exc_text = None
    copy.stack_info = None
    return copy


def _call_handlers(self: logging.Logger, record: logging.LogRecord) -> None:
    _ORIGINAL_CALL_HANDLERS(self, _sanitized(record))


class SafeFormatter(logging.Formatter):
    # The sanitized copy carries no exc_info or stack_info, so the base
    # class never reaches formatException or formatStack.
    def format(self, record: logging.LogRecord) -> str:
        return super().format(_sanitized(record))


def _write(text: str) -> None:
    try:
        sys.stderr.write(text)
        sys.stderr.flush()
    except BaseException:
        pass


class _SafeStreamHandler(logging.StreamHandler[TextIO]):
    def __init__(self, stream: TextIO, level: int = logging.NOTSET) -> None:
        super().__init__(stream)
        self.setLevel(level)
        self.setFormatter(SafeFormatter(_LOG_FORMAT))

    def handleError(self, record: logging.LogRecord) -> None:
        _write(_OUTPUT_FAILED)


def _show_warning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: TextIO | None = None,
    line: str | None = None,
) -> None:
    try:
        stream = sys.stderr if file is None else file
        stream.write(f"{category.__name__} at {filename}:{lineno} {_MESSAGE_WITHHELD}\n")
        stream.flush()
    except BaseException:
        pass


def _qt_message(
    message_type: QtMsgType,
    context: QMessageLogContext,
    message: str | None,
) -> None:
    # A write that itself produced a Qt message would re-enter on this thread.
    if getattr(_qt_handler_state, "active", False):
        return
    _qt_handler_state.active = True
    try:
        _write(
            f"Qt {message_type.name} at {context.file}:{context.line} "
            f"({context.category}) {_MESSAGE_WITHHELD}\n"
        )
    except BaseException:
        pass
    finally:
        _qt_handler_state.active = False


def _describe_or_type(exc: BaseException | None, exc_type: type[BaseException] | None) -> str:
    if exc is not None:
        return describe(exc)
    return getattr(exc_type, "__name__", "exception")


def _unraisable_hook(unraisable: "sys.UnraisableHookArgs") -> None:
    try:
        _write(
            "Exception ignored:\n"
            + _describe_or_type(unraisable.exc_value, unraisable.exc_type)
            + "\n"
        )
    except BaseException:
        pass


def _thread_hook(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return
    try:
        _write(
            "Unhandled exception in a thread:\n"
            + _describe_or_type(args.exc_value, args.exc_type)
            + "\n"
        )
    except BaseException:
        pass


def _fatal_hook(
    exc_type: type[BaseException],
    exc: BaseException,
    traceback: TracebackType | None,
) -> None:
    try:
        try:
            sys.stdout.flush()
        except BaseException:
            pass
        _write("Unhandled exception; Simple Chart is exiting:\n" + describe(exc) + "\n")
    finally:
        os._exit(1)


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    logging.raiseExceptions = False
    logging.Logger.callHandlers = _call_handlers  # type: ignore[method-assign]
    logging.getLogger().addHandler(_SafeStreamHandler(sys.stderr))
    logging.lastResort = _SafeStreamHandler(sys.stderr, logging.WARNING)
    warnings.showwarning = _show_warning
    qInstallMessageHandler(_qt_message)
    sys.unraisablehook = _unraisable_hook
    threading.excepthook = _thread_hook
    sys.excepthook = _fatal_hook
