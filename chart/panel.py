"""
chart/panel.py

A Panel wraps one finplot axis — one horizontal region of the chart.

A typical SimpleChart layout has two panels:
  - Price panel  (candlesticks + overlaid indicators like MA and AVWAP)
  - Volume panel (volume bars below the price panel)

Additional panels could be added below volume for standalone indicators
(e.g. RVOL, RSI) but that is a future concern.

finplot creates panels by specifying the number of rows when the plot
window is initialized. Each row is an axis that shares the same x-axis
(time) as the others. This module wraps that concept so the rest of the
chart layer works with Panel objects rather than raw finplot axes.
"""

from __future__ import annotations

from enum import Enum, auto


class PanelType(Enum):
    PRICE  = auto()
    VOLUME = auto()


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
        ax: object,          # finplot axis — typed as object to avoid
        panel_type: PanelType,  # importing finplot at module level
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
