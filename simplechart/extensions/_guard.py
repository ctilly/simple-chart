"""
simplechart/extensions/_guard.py

The one boundary through which the host calls extension code (internal; not
part of simplechart.api).

A call that raises is logged with describe() (never the exception's text),
its extension is turned off for the rest of the session, and one notice is
queued for the user. A turned-off extension is never called again: every
guarded call for it returns None without running plugin code. The host
registers one handler to clear its own interaction state and render output
when extensions are turned off; cleanup never calls plugin code.

A store handler belongs to the module that registered it. When one raises,
every extension that module registered is turned off, and its handlers are
skipped from then on. Handlers whose module's extensions are all turned off
are skipped too.
"""

import logging
from collections.abc import Callable
from typing import Any

from data.errors import describe
from simplechart.extensions._registry import extension_label, extensions_owned_by


_LOGGER = logging.getLogger(__name__)

type DisableHandler = Callable[[frozenset[str]], None]

_disabled: set[str] = set()
_failed_owners: set[str] = set()
_notices: list[str] = []
_handlers: list[DisableHandler] = []


def call_extension[T](
    name: str,
    fn: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T | None:
    if name in _disabled:
        return None
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        _LOGGER.error(
            "Extension %r failed in %s and was turned off: %s",
            name,
            _hook_name(fn),
            describe(exc),
        )
        _turn_off(name)
        _notify(frozenset({name}))
        return None


def call_store_handler[T](
    owner: str,
    fn: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T | None:
    if store_owner_disabled(owner):
        return None
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        _LOGGER.error(
            "Store handler from %r failed in %s; its extensions were turned off: %s",
            owner,
            _hook_name(fn),
            describe(exc),
        )
        _failed_owners.add(owner)
        names = [name for name in extensions_owned_by(owner) if name not in _disabled]
        for name in names:
            _turn_off(name)
        if not extensions_owned_by(owner):
            _notices.append(_disabled_notice(owner.rsplit(".", 1)[-1]))
        _notify(frozenset(names))
        return None


def is_disabled(name: str) -> bool:
    return name in _disabled


def store_owner_disabled(owner: str) -> bool:
    if owner in _failed_owners:
        return True
    names = extensions_owned_by(owner)
    return bool(names) and all(name in _disabled for name in names)


def queue_notice(text: str) -> None:
    _notices.append(text)
    _notify(frozenset())


def take_notices() -> list[str]:
    notices = list(_notices)
    _notices.clear()
    return notices


def set_disable_handler(handler: DisableHandler | None) -> None:
    # One host (the main window) cleans up after turned-off extensions; it
    # is also told when a notice is queued (with no names).
    _handlers.clear()
    if handler is not None:
        _handlers.append(handler)


def _turn_off(name: str) -> None:
    _disabled.add(name)
    _notices.append(_disabled_notice(extension_label(name)))


def _disabled_notice(label: str) -> str:
    return f"'{label}' failed and was turned off. Details were logged."


def _notify(names: frozenset[str]) -> None:
    for handler in tuple(_handlers):
        handler(names)


def _hook_name(fn: Callable[..., object]) -> str:
    try:
        name = getattr(fn, "__name__", None)
    except Exception:
        return "extension code"
    return name if type(name) is str else "extension code"
