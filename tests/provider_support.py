import logging
import sys
import threading
import warnings
from contextlib import ExitStack
from functools import cache
from unittest.mock import patch

from PyQt6.QtCore import qInstallMessageHandler

from data.provider.config import ProviderConnection
from data.provider.registry import FALLBACK_PROVIDER as YFINANCE_CONNECTION_ID
from data.provider.registry import ProviderRegistry
from providers.alpaca import (
    ALPACA_LIVE_CONNECTION_ID,
    ALPACA_PAPER_CONNECTION_ID,
    MarketDataFeed,
)
from simplechart import output_safety
from simplechart.bootstrap import bootstrap


@cache
def startup_registry() -> ProviderRegistry:
    root = logging.getLogger()
    handlers = root.handlers[:]
    qt_handler = qInstallMessageHandler(None)
    qInstallMessageHandler(qt_handler)
    with ExitStack() as restore:
        for target, attribute in (
            (sys, "excepthook"),
            (sys, "unraisablehook"),
            (threading, "excepthook"),
            (warnings, "showwarning"),
            (logging.Logger, "callHandlers"),
            (logging, "lastResort"),
            (logging, "raiseExceptions"),
            (output_safety, "_installed"),
        ):
            restore.enter_context(patch.object(target, attribute, getattr(target, attribute)))
        try:
            return bootstrap(user_plugin_dir=None)
        finally:
            root.handlers[:] = handlers
            qInstallMessageHandler(qt_handler)


def yfinance_connection() -> ProviderConnection:
    return startup_registry().build_connection(YFINANCE_CONNECTION_ID)


def alpaca_paper_connection(feed: MarketDataFeed = MarketDataFeed.IEX) -> ProviderConnection:
    return startup_registry().build_connection(ALPACA_PAPER_CONNECTION_ID, {"feed": feed.value})


def alpaca_live_connection() -> ProviderConnection:
    return startup_registry().build_connection(ALPACA_LIVE_CONNECTION_ID)

