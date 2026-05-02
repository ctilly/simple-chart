"""
chart/panel.py

A Panel wraps one finplot axis — one horizontal region of the chart.

A typical SimpleChart layout has two always-visible panels:
  - Price panel  (candlesticks + chart indicators like MA and AVWAP)
  - Volume panel (volume bars below the price panel)

Up to three indicator panels sit below volume. They are pre-allocated at
startup with zero height and revealed when a panel indicator is added.
See chart/window.py for slot management and chart/viewport.py for the
viewport behavior that is installed on first use.

finplot creates panels by specifying the number of rows when the plot
window is initialized. Each row is an axis that shares the same x-axis
(time) as the others. This module wraps that concept so the rest of the
chart layer works with Panel objects rather than raw finplot axes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class PanelType(Enum):
    PRICE     = auto()
    VOLUME    = auto()
    INDICATOR = auto()


class Panel:
    """
    Represents one horizontal region of the chart.

    The `ax` attribute is the raw finplot axis object. The chart layer
    passes ax directly to finplot draw calls (candlestick_ochl, plot, etc.)
    to target the correct panel.

    ratio controls the relative height of this panel compared to others.
    A price panel with ratio=4 and a volume panel with ratio=1 means the
    price panel gets 80% of the vertical space.
    """

    def __init__(
        self,
        ax: object,
        panel_type: PanelType,
        ratio: int = 1,
    ) -> None:
        self.ax = ax
        self.panel_type = panel_type
        self.ratio = ratio

    @property
    def is_price(self) -> bool:
        return self.panel_type == PanelType.PRICE

    @property
    def is_volume(self) -> bool:
        return self.panel_type == PanelType.VOLUME

    @property
    def is_indicator(self) -> bool:
        return self.panel_type == PanelType.INDICATOR


@dataclass
class IndicatorPanelSlot:
    """
    One of three pre-allocated indicator panel slots below the volume panel.

    name is the render_target() string of the indicator currently
    occupying this slot, or None if the slot is unoccupied.

    behavior_installed tracks whether viewport behavior (y-autoscale patch,
    drag forwarding to the price viewbox) has been installed on this slot's
    viewbox. Done once on first assignment; survives axis resets.
    """

    panel: Panel
    name: str | None = None
    behavior_installed: bool = False
