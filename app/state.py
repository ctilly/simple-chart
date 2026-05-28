"""
app/state.py

Application state — the single source of truth for what is currently
displayed on the chart.

The controller owns the State object and updates it as the user interacts
with the app. No other layer holds chart state directly. This makes it
straightforward to reason about what is on screen at any point: just
inspect the State object.

State is intentionally a plain dataclass with no methods. Logic belongs
in the controller; this is just a container.
"""

from dataclasses import dataclass, field
from typing import Any

from data.models import Timeframe


@dataclass
class ChartExtensionState:
    """
    The active configuration for one chart extension instance.

    name       — registry key (e.g. "sma", "avwap")
    params     — the current parameter dict (from default_params(), possibly
                 edited by the user via the config dialog)
    visible    — whether the extension's render items are currently shown
    series_keys — the keys last returned by compute(), used to update the
                 PlotManager and legend when params change
    series_visibility — optional per-series visibility overrides for
                 extensions that emit multiple independent series

    One ChartExtensionState entry exists per chart extension instance on the chart.
    Multiple SMAs (50-day and 200-day) are two separate entries with the
    same name but different params.
    """

    name:        str
    params:      dict[str, Any]
    visible:     bool = True
    series_keys: list[str] = field(default_factory=list)
    series_visibility: dict[str, bool] = field(default_factory=dict)


@dataclass
class State:
    """
    Full application state.

    symbol      — the currently loaded ticker symbol (e.g. "QQQ")
    timeframe   — the currently active timeframe
    extensions  — ordered list of active extension instances; order
                  determines draw order on the chart

    symbol and timeframe start as None — the app shows an empty chart
    until the user enters a symbol.
    """

    symbol:     str | None = None
    timeframe:  Timeframe  = Timeframe.DAILY
    extensions: list[ChartExtensionState] = field(default_factory=list)

    def get_extension(self, name: str) -> ChartExtensionState | None:
        """Return the first extension instance with the given name, or None."""
        return next((i for i in self.extensions if i.name == name), None)

    def get_extension_by_series_key(self, series_key: str) -> ChartExtensionState | None:
        """Return the extension that owns a given series key, or None."""
        return next(
            (i for i in self.extensions if series_key in i.series_keys),
            None,
        )
