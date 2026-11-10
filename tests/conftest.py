import functools
import logging
import sys
import threading
import warnings
from collections.abc import Iterator

import pytest
from PyQt6.QtCore import qInstallMessageHandler

from simplechart.extensions import _guard


def _hook_identity(hook: object) -> object:
    # pytest-qt reinstalls its excepthook and Qt message capture for each
    # test phase as a new partial or bound method around the same function.
    if isinstance(hook, functools.partial):
        return hook.func
    return getattr(hook, "__func__", hook)


def _current_qt_message_handler() -> object:
    previous = qInstallMessageHandler(None)
    qInstallMessageHandler(previous)
    return previous


def _process_output_state() -> tuple[object, ...]:
    return (
        _hook_identity(sys.excepthook),
        _hook_identity(threading.excepthook),
        _hook_identity(sys.unraisablehook),
        _hook_identity(warnings.showwarning),
        logging.Logger.callHandlers,
        logging.lastResort,
        logging.raiseExceptions,
        tuple(logging.getLogger().handlers),
    )


@pytest.fixture(autouse=True)
def _process_output_hooks_restored() -> Iterator[None]:
    # Production output hooks are installed only in subprocess tests; an
    # in-process test must leave every hook and root handler as it found it.
    before = _process_output_state()
    yield
    assert _process_output_state() == before
    # pytest-qt installs its Qt message capture only for parts of each test
    # phase, so the Qt handler is checked for the production one instead.
    qt_handler = _hook_identity(_current_qt_message_handler())
    assert getattr(qt_handler, "__module__", None) != "simplechart.output_safety"


def _reset_extension_guard() -> None:
    _guard.set_disable_handler(None)
    _guard._disabled.clear()
    _guard._failed_owners.clear()
    _guard._notices.clear()


@pytest.fixture(autouse=True)
def _extension_guard_reset() -> Iterator[None]:
    # Turned-off extensions, queued notices and the window's cleanup handler
    # are session state; no test may inherit another's.
    _reset_extension_guard()
    yield
    _reset_extension_guard()
