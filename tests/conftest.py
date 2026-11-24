import functools
import logging
import sys
import threading
import warnings
from collections.abc import Iterator

import pytest
from PyQt6.QtCore import qInstallMessageHandler
from PyQt6.QtWidgets import QApplication

from data.provider.registry import ProviderRegistry
from provider_support import startup_registry
from simplechart.extensions import _guard


@pytest.fixture(scope="session", autouse=True)
def provider_registry() -> ProviderRegistry:
    return startup_registry()


@pytest.fixture
def isolated_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.controller as controller
    from data.provider.credentials import CredentialStoreAccess, UnavailableCredentialStore

    monkeypatch.setattr(
        controller,
        "initialize_keyring_credential_store",
        lambda *, registry: CredentialStoreAccess(UnavailableCredentialStore("test"), False, "test"),
    )


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


@pytest.fixture(autouse=True)
def _no_live_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    # The chart polls its newest bars on a wall-clock timer during the
    # regular session; tests that build a MainWindow must not depend on the
    # time of day. tests/test_live_refresh.py opts back in.
    controller = sys.modules.get("app.controller")
    if controller is not None:
        monkeypatch.setattr(controller.MainWindow, "_refresh_live_bars", lambda _window: None)


@pytest.fixture(autouse=True)
def _no_live_earnings_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    from yfinance import Ticker

    def empty_calendar(_ticker: Ticker) -> dict[str, object]:
        return {}

    monkeypatch.setattr(Ticker, "get_calendar", empty_calendar)


_BASE_STYLE: list[str] = []


@pytest.fixture(autouse=True)
def _theme_state_restored(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    # A MainWindow applies its theme to the whole QApplication, and tests
    # must not read the real ~/.simplechart/themes. Put the application's
    # style, palette, stylesheet and active theme back after every test.
    controller = sys.modules.get("app.controller")
    if controller is not None:
        monkeypatch.setattr(controller, "USER_THEMES_DIR", tmp_path_factory.mktemp("themes"))
    app = QApplication.instance()
    if isinstance(app, QApplication) and not _BASE_STYLE:
        _BASE_STYLE.append(app.style().objectName())  # first test: no stylesheet yet
    style = _BASE_STYLE[0] if _BASE_STYLE else ""
    palette = app.palette() if isinstance(app, QApplication) else None
    sheet = app.styleSheet() if isinstance(app, QApplication) else ""
    from app import theme

    active = theme._active
    yield
    theme._active = active
    app = QApplication.instance()
    # Only a dark theme changes the style and palette. A MainWindow's light
    # stylesheet is left in place: resetting it for every test would restyle
    # every widget earlier tests left alive, which slows the suite sharply.
    if isinstance(app, QApplication) and palette is not None and app.palette() != palette:
        app.setStyleSheet("")  # a stylesheet hides the base style's name
        app.setStyle(style)
        app.setPalette(palette)
    if isinstance(app, QApplication) and app.styleSheet() != sheet:
        app.setStyleSheet(sheet)
