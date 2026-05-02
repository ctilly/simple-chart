"""
simplechart/api.py

Public API for SimpleChart indicator plugins.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHO THIS IS FOR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This module is the only import surface plugin authors ever need. Import
everything from here — never from the internal packages (indicators.*,
data.*, etc.). Internal paths may change across versions; this module
will not.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INDICATOR CATEGORIES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Chart indicators — draw directly on the price chart (time × price axes).
  Examples: SMA, EMA, AVWAP.
  These do not override render_target(); the default RENDER_CHART applies.

Panel indicators — draw in a dedicated panel below the chart, sharing
  only the x (time) axis with the price chart. Their y-axis is scaled
  independently to the indicator's value range.
  Examples: RSI, MACD, RVOL.
  These override render_target() and return a short lowercase string
  naming their panel (e.g. "rsi", "macd"). Each unique string gets its
  own panel. Two instances returning the same string share one panel.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MINIMAL EXAMPLE — a 20-day simple moving average
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    from typing import Any
    import numpy as np
    from simplechart.api import Indicator, OHLCVSeries, bars_for_n_days, register

    class MySMAIndicator(Indicator):

        def name(self) -> str:
            return "my_sma"

        def label(self) -> str:
            return "My Simple Moving Average"

        def default_params(self) -> dict[str, Any]:
            return {"days": 20, "color": "#00BFFF"}

        def compute(
            self,
            series: OHLCVSeries,
            params: dict[str, Any],
        ) -> dict[str, np.ndarray]:
            period = bars_for_n_days(int(params["days"]), series.timeframe)
            closes = np.array([bar.close for bar in series.bars], dtype=float)
            result = np.full(len(closes), np.nan)
            for i in range(period - 1, len(closes)):
                result[i] = closes[i - period + 1 : i + 1].mean()
            return {f"my_sma_{params['days']}": result}

    register(MySMAIndicator)

For a panel indicator (e.g. RSI), add one override:

    def render_target(self) -> str:
        return "my_rsi"   # unique string → dedicated panel below the chart

Drop the file into ~/.simplechart/plugins/ — no other steps needed.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXPORTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Name                      Description
  ────────────────────────  ──────────────────────────────────────────
  Indicator                 ABC to subclass for every indicator
  ChoiceParam               Dropdown parameter (value + allowed options)
  LINE_STYLE_OPTIONS        Standard line style strings for ChoiceParam
  RENDER_CHART              render_target() constant for chart indicators
  SeriesFill                Declares a shaded fill between two series
  register                  Register an Indicator class at import time
  OHLCVSeries               Bar series type passed to compute()
  Bar                       Single OHLCV bar
  AnchorRecord              Anchor record for AVWAP-style indicators
  bars_for_n_days           Convert a day count to a bar count for a timeframe
  timestamp_ms_to_bar_index Convert a UTC ms timestamp to a bar index
"""

from indicators._base import (  # noqa: F401
    RENDER_CHART,               # "chart" — default render_target() return value
    ChoiceParam,                # dropdown param: ChoiceParam(value, options)
    Indicator,                  # ABC: subclass this for every indicator
    LINE_STYLE_OPTIONS,         # ["solid", "dash", "dot", "dash_dot"]
    SeriesFill,                 # fill between two series: SeriesFill(a, b, alpha)
)
from indicators._registry import register  # noqa: F401  — register(MyClass) at module bottom
from data.models import (      # noqa: F401
    AnchorRecord,              # anchor record for AVWAP-style indicators
    Bar,                       # single OHLCV bar
    OHLCVSeries,               # full bar series passed to compute()
)
from data.calendar import (    # noqa: F401
    bars_for_n_days,           # bars_for_n_days(days, timeframe) -> int
    timestamp_ms_to_bar_index, # timestamp_ms_to_bar_index(ts_ms, series) -> int
)
