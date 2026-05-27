"""
simplechart/api.py

Public API for SimpleChart extension plugins (indicators and tools).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHO THIS IS FOR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This module is the only import surface plugin authors ever need. Import
everything from here — never from the internal packages (indicators.*,
tools.*, data.*, etc.). Internal paths may change across versions; this
module will not.

A ChartExtension is the single base class for everything that draws on the
chart: compute-style indicators (SMA, EMA, RSI) and interactive tools
(vertical line, Fibonacci retracement) alike.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXTENSION CATEGORIES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Chart extensions — draw directly on the price chart (time × price axes).
  Examples: SMA, EMA, AVWAP.
  These do not override render_target(); the default RENDER_CHART applies.

Panel extensions — draw in a dedicated panel below the chart, sharing
  only the x (time) axis with the price chart. Their y-axis is scaled
  independently to the extension's value range.
  Examples: RSI, MACD, RVOL.
  These override render_target() and return a short lowercase string
  naming their panel (e.g. "rsi", "macd"). Each unique string gets its
  own panel. Two instances returning the same string share one panel.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MINIMAL EXAMPLE — a 20-day simple moving average
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    from typing import Any
    import numpy as np
    from simplechart.api import ChartExtension, OHLCVSeries, bars_for_n_days, register_extension

    class MySMA(ChartExtension):

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

    register_extension(MySMA)

For a panel extension (e.g. RSI), add one override:

    def render_target(self) -> str:
        return "my_rsi"   # unique string → dedicated panel below the chart

Drop the file into ~/.simplechart/plugins/ — no other steps needed.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXPORTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Name                         Description
  ───────────────────────────  ───────────────────────────────────────────
  ChartExtension               ABC to subclass for every extension
  ChartExtensionAddMode        Declares where/how an extension can be added
  ChoiceParam                  Dropdown parameter (value + allowed options)
  LINE_STYLE_OPTIONS           Standard line style strings for ChoiceParam
  RENDER_CHART                 render_target() constant for chart extensions
  SeriesFill                   Declares a shaded fill between two series
  SeriesRender                 Render description for a line series
  HorizontalSegmentRender      Render description for a horizontal line segment
  VerticalLineRender           Render description for a full-height vertical line
  MarkerRender                 Render description for a chart marker
  ChartExtensionRender         Render output produced by an extension
  ChartEvent                   Generic chart interaction event
  ChartExtensionAction         User-triggered extension action
  ChartExtensionMutation       Requested extension state mutation
  ChartExtensionConfig         Extension-owned config form for a render series
  DrawingSession               Active toolbar drawing-tool state
  DrawingToolResult            Result of advancing a toolbar drawing tool
  ChartExtensionStoreRecord    Opaque plugin-owned persistence record
  ChartExtensionStoreContext   Store-handler access to app persistence/state services
  ChartExtensionStoreHandler   Store-handler protocol
  DrawingStore                 Generic per-tool persistence store base
  AxisPolicy                   FIXED_ON | FIXED_OFF | USER per persistence axis
  HitTestResult                Interactive handle hit-test result
  DragSession                  Active extension drag state
  register_extension           Register a ChartExtension class at import time
  register_store_handler       Register an extension-owned store handler
  all_extensions               Return registered extension classes
  get_extension                Instantiate a registered extension
  OHLCVSeries                  Bar series type passed to compute()
  Bar                          Single OHLCV bar
  bars_for_n_days              Convert a day count to a bar count for a timeframe
  timestamp_ms_to_bar_index    Convert a UTC ms timestamp to a bar index
"""

from simplechart.extensions._base import (  # noqa: F401
    RENDER_CHART,               # "chart" — default render_target() return value
    ChartEvent,                 # generic chart interaction event
    ChartExtension,             # ABC: subclass this for every extension
    ChartExtensionAction,       # user-triggered extension action
    ChartExtensionAddMode,      # declares where/how an extension can be added
    ChartExtensionConfig,       # extension-owned config form for a render series
    ChartExtensionMutation,     # requested extension state mutation
    ChartExtensionRender,       # render output: series + markers
    ChartExtensionStoreContext, # store-handler access to app persistence/state services
    ChartExtensionStoreHandler, # store-handler protocol
    ChoiceParam,                # dropdown param: ChoiceParam(value, options)
    DrawingSession,             # active toolbar drawing-tool state
    DrawingToolResult,          # drawing-tool advance result
    DragSession,                # active extension drag state
    HitTestResult,              # interactive handle hit-test result
    HorizontalSegmentRender,    # horizontal line segment render primitive
    LINE_STYLE_OPTIONS,         # ["solid", "dash", "dot", "dash_dot"]
    MarkerRender,               # chart marker render primitive
    SeriesFill,                 # fill between two series: SeriesFill(a, b, alpha)
    SeriesRender,               # line series render primitive
    VerticalLineRender,         # full-height vertical line render primitive
)
from simplechart.extensions._registry import (  # noqa: F401
    all_extensions,
    get_extension,
    register_extension,
)
from simplechart.extensions._store_registry import register_store_handler  # noqa: F401
from simplechart.extensions._drawing_store import (  # noqa: F401
    AxisPolicy,                 # FIXED_ON | FIXED_OFF | USER per persistence axis
    DrawingStore,               # generic per-tool persistence store base
)
from data.models import (         # noqa: F401
    Bar,                          # single OHLCV bar
    ChartExtensionStoreRecord,    # opaque plugin-owned persistence record
    OHLCVSeries,                  # full bar series passed to compute()
)
from data.calendar import (    # noqa: F401
    bars_for_n_days,           # bars_for_n_days(days, timeframe) -> int
    timestamp_ms_to_bar_index, # timestamp_ms_to_bar_index(ts_ms, series) -> int
)


__all__: list[str] = [
    "AxisPolicy",
    "Bar",
    "ChartEvent",
    "ChartExtension",
    "ChartExtensionAction",
    "ChartExtensionAddMode",
    "ChartExtensionConfig",
    "ChartExtensionMutation",
    "ChartExtensionRender",
    "ChartExtensionStoreContext",
    "ChartExtensionStoreHandler",
    "ChartExtensionStoreRecord",
    "ChoiceParam",
    "DrawingSession",
    "DrawingStore",
    "DrawingToolResult",
    "DragSession",
    "HitTestResult",
    "HorizontalSegmentRender",
    "LINE_STYLE_OPTIONS",
    "MarkerRender",
    "OHLCVSeries",
    "RENDER_CHART",
    "SeriesFill",
    "SeriesRender",
    "VerticalLineRender",
    "bars_for_n_days",
    "register_extension",
    "register_store_handler",
    "all_extensions",
    "get_extension",
    "timestamp_ms_to_bar_index",
]
