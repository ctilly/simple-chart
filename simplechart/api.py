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
    from simplechart.api import Indicator, OHLCVSeries, bars_for_n_days, register_indicator

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

    register_indicator(MySMAIndicator)

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
  IndicatorAddMode          Declares where/how an indicator can be added
  ChoiceParam               Dropdown parameter (value + allowed options)
  LINE_STYLE_OPTIONS        Standard line style strings for ChoiceParam
  RENDER_CHART              render_target() constant for chart indicators
  SeriesFill                Declares a shaded fill between two series
  SeriesRender              Render description for a line series
  HorizontalSegmentRender   Render description for a horizontal line segment
  MarkerRender              Render description for a chart marker
  IndicatorRender           Render output produced by an indicator
  ChartEvent                Generic chart interaction event
  IndicatorAction           User-triggered indicator action
  IndicatorMutation         Requested indicator state mutation
  IndicatorConfig           Indicator-owned config form for a render series
  IndicatorStoreRecord      Opaque plugin-owned persistence record
  IndicatorStoreContext     Store-handler access to app persistence/state services
  IndicatorStoreHandler     Store-handler protocol
  HitTestResult             Interactive handle hit-test result
  DragSession               Active indicator drag state
  register_indicator        Register an Indicator class at import time
  register                  Backward-compatible alias for register_indicator
  register_store_handler    Register an indicator-owned store handler
  all_indicators            Return registered indicator classes
  get_indicator             Instantiate a registered indicator
  OHLCVSeries               Bar series type passed to compute()
  Bar                       Single OHLCV bar
  bars_for_n_days           Convert a day count to a bar count for a timeframe
  timestamp_ms_to_bar_index Convert a UTC ms timestamp to a bar index
"""

from indicators._base import (  # noqa: F401
    RENDER_CHART,               # "chart" — default render_target() return value
    ChartEvent,                 # generic chart interaction event
    ChoiceParam,                # dropdown param: ChoiceParam(value, options)
    DragSession,                # active indicator drag state
    HitTestResult,              # interactive handle hit-test result
    HorizontalSegmentRender,    # horizontal line segment render primitive
    Indicator,                  # ABC: subclass this for every indicator
    IndicatorAddMode,           # declares where/how an indicator can be added
    IndicatorAction,            # user-triggered indicator action
    IndicatorConfig,            # indicator-owned config form for a render series
    IndicatorMutation,          # requested indicator state mutation
    IndicatorRender,            # render output: series + markers
    IndicatorStoreContext,      # store-handler access to app persistence/state services
    IndicatorStoreHandler,      # store-handler protocol
    LINE_STYLE_OPTIONS,         # ["solid", "dash", "dot", "dash_dot"]
    MarkerRender,               # chart marker render primitive
    SeriesFill,                 # fill between two series: SeriesFill(a, b, alpha)
    SeriesRender,               # line series render primitive
)
from indicators._registry import (  # noqa: F401
    all_indicators,
    get_indicator,
    register,
    register_indicator,
)
from indicators._store_registry import register_store_handler  # noqa: F401
from data.models import (      # noqa: F401
    Bar,                       # single OHLCV bar
    IndicatorStoreRecord,      # opaque plugin-owned persistence record
    OHLCVSeries,               # full bar series passed to compute()
)
from data.calendar import (    # noqa: F401
    bars_for_n_days,           # bars_for_n_days(days, timeframe) -> int
    timestamp_ms_to_bar_index, # timestamp_ms_to_bar_index(ts_ms, series) -> int
)


__all__: list[str] = [
    "Bar",
    "ChartEvent",
    "ChoiceParam",
    "DragSession",
    "HitTestResult",
    "HorizontalSegmentRender",
    "Indicator",
    "IndicatorAddMode",
    "IndicatorAction",
    "IndicatorConfig",
    "IndicatorMutation",
    "IndicatorRender",
    "IndicatorStoreRecord",
    "IndicatorStoreContext",
    "IndicatorStoreHandler",
    "LINE_STYLE_OPTIONS",
    "MarkerRender",
    "OHLCVSeries",
    "RENDER_CHART",
    "SeriesFill",
    "SeriesRender",
    "bars_for_n_days",
    "register",
    "register_indicator",
    "register_store_handler",
    "all_indicators",
    "get_indicator",
    "timestamp_ms_to_bar_index",
]
