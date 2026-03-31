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

from data.models import AnchorRecord, Timeframe


@dataclass
class IndicatorState:
    """
    The active configuration for one indicator instance.

    name       — registry key (e.g. "sma", "avwap")
    params     — the current parameter dict (from default_params(), possibly
                 edited by the user via the config dialog)
    visible    — whether the indicator's plot lines are currently shown
    series_keys — the keys last returned by compute(), used to update the
                 PlotManager and legend when params change

    One IndicatorState entry exists per indicator instance on the chart.
    Multiple SMAs (50-day and 200-day) are two separate entries with the
    same name but different params.
    """

    name:        str
    params:      dict[str, Any]
    visible:     bool = True
    series_keys: list[str] = field(default_factory=list)


@dataclass
class State:
    """
    Full application state.

    symbol      — the currently loaded ticker symbol (e.g. "QQQ")
    timeframe   — the currently active timeframe
    indicators  — ordered list of active indicator instances; order
                  determines draw order on the chart
    anchors     — AVWAP anchors for the current symbol, loaded from
                  SQLite on symbol load and updated as anchors are
                  added or removed during the session

    symbol and timeframe start as None — the app shows an empty chart
    until the user enters a symbol.
    """

    symbol:     str | None = None
    timeframe:  Timeframe  = Timeframe.DAILY
    indicators: list[IndicatorState] = field(default_factory=list)
    anchors:    list[AnchorRecord]   = field(default_factory=list)

    def get_indicator(self, name: str) -> IndicatorState | None:
        """Return the first indicator instance with the given name, or None."""
        return next((i for i in self.indicators if i.name == name), None)

    def get_indicator_by_series_key(self, series_key: str) -> IndicatorState | None:
        """Return the indicator that owns a given series key, or None."""
        return next(
            (i for i in self.indicators if series_key in i.series_keys),
            None,
        )

    def get_anchor(self, anchor_id: int) -> AnchorRecord | None:
        """Return the anchor with the given database ID, or None."""
        return next((a for a in self.anchors if a.anchor_id == anchor_id), None)
