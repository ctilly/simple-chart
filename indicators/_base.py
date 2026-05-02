"""
indicators/_base.py

Abstract base class for all indicators.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW THE INDICATOR SYSTEM WORKS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

There are three layers involved in every indicator:

  1. The Indicator subclass (this file / indicators/*.py)
     - Implements the Indicator ABC
     - Responsible for extracting arrays from OHLCVSeries (e.g. closes,
       volumes) and delegating to any compiled kernels
     - Plain Python — not compiled

  2. Compiled kernels (optional, per-indicator _kernel.py)
     - Pure numeric functions: numpy array(s) in, numpy array out
     - No I/O, no dicts, no strings, no dynamic dispatch
     - Compiled to native extensions via mypyc for maximum speed
     - Live alongside the indicator in its directory (e.g. indicators/ema/)

  3. The registry (indicators/_registry.py)
     - Maps indicator names to Indicator classes
     - All indicators are registered at import time via register()
     - Auto-discovery in _loader.py handles the imports

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
THE MYPYC BOUNDARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

mypyc compiles Python source to C extensions. The rule is:

  Code that CAN be compiled lives in _kernel.py files inside indicator
  directories (e.g. indicators/ema/_kernel.py). It must:
    - Use concrete types only (no Any, no Union in hot paths)
    - Accept and return numpy arrays or plain scalars
    - Perform no I/O (no file access, no SQLite, no network)
    - Use no ABCMeta, no dynamic attribute setting

  Code that CANNOT be compiled (this file, _registry.py, Indicator
  subclasses) handles the "glue": extracting arrays from OHLCVSeries,
  calling compiled functions, packing results back into dicts.

To compile a kernel, run:
    python scripts/build_compiled.py

During development you can skip compilation entirely — the _kernel.py
source files run as normal Python. The compiled .so extensions are
drop-in replacements; Python imports the .so if present, falls back to
the .py if not.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMPUTE() CONTRACT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

compute() returns a dict[str, np.ndarray]. Each key is a unique series
name; each value is an array aligned to series.bars (same length).
Use np.nan for bars where the indicator has no value.

  Example — 50-day SMA:
      {"sma_50": np.array([nan, nan, ..., 14.56, 14.61])}

  Example — two AVWAP anchors (keys include the anchor timestamp so
  each anchor gets its own distinct plot line):
      {
          "avwap_1704067200000": np.array([nan, ..., 14.20, 14.25]),
          "avwap_1706745600000": np.array([nan, ..., nan,   14.31]),
      }

The chart layer uses these keys to identify and toggle individual plot
lines. Stable, unique keys are important — if a key changes between
compute() calls, the chart will create a new line instead of updating
the existing one.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from data.models import OHLCVSeries


LINE_STYLE_OPTIONS: list[str] = ["solid", "dash", "dot", "dash_dot"]

RENDER_CHART: str = "chart"


@dataclass
class SeriesFill:
    """
    Declares a shaded fill between two named series produced by compute().

    series_a and series_b must be keys returned by the indicator's compute()
    method. The fill is drawn between those two lines using the indicator's
    color param at the given alpha (0.0 = fully transparent, 1.0 = opaque).
    Values between 0.1 and 0.3 are typical for chart fills.

    Example (Bollinger Bands):
        SeriesFill("bb_upper", "bb_lower", alpha=0.15)
    """

    series_a: str
    series_b: str
    alpha: float = 0.15


@dataclass
class ChoiceParam:
    """
    A parameter that must be one of a fixed set of string options.

    Indicators put this in their default_params() for any field that maps to
    a dropdown in the config dialog. The value and choices travel together so
    the dialog can reconstruct the combo box without knowing anything about
    the specific indicator.

    Example:
        "line_style": ChoiceParam("solid", LINE_STYLE_OPTIONS)
    """

    value: str
    options: list[str]


class Indicator(ABC):

    @abstractmethod
    def name(self) -> str:
        """
        Unique machine-readable identifier. Used as a registry key and
        as a prefix for plot series names returned by compute().

        Example: "sma", "avwap", "rvol"
        """

    @abstractmethod
    def label(self) -> str:
        """
        Human-readable display name shown in the chart legend and the
        indicator config dialog.

        Example: "Simple Moving Average", "Anchored VWAP"
        """

    @abstractmethod
    def default_params(self) -> dict[str, Any]:
        """
        Return the default parameter set for this indicator.

        The config dialog calls this to build its input form. Keys are
        parameter names; values are the defaults. The same dict structure
        is passed back to compute() after the user edits it.

        Example for SMA:
            {"days": 50, "color": "#00BFFF"}

        Example for AVWAP:
            {"anchors": [], "color": "#00FF88"}
        """

    @abstractmethod
    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        """
        Compute plot series from bar data and return named arrays.

        series  — the currently loaded bars for the active symbol/timeframe
        params  — the active parameter set (from default_params(), possibly
                  edited by the user via the config dialog)

        Returns a dict of named arrays, each the same length as series.bars,
        aligned to the bar timestamps. Use np.nan for bars where the
        indicator has no value (e.g. before an MA has accumulated enough
        bars to produce its first valid output).

        Delegate heavy numeric work to a _kernel.py module when the
        computation involves a tight loop over thousands of bars.
        """

    def render_target(self) -> str:
        """
        Return the render target for this indicator.

        Chart indicators return RENDER_CHART (the default — no override needed).
        They draw directly on the price chart, sharing its time × price axes.

        Panel indicators return a short lowercase string naming their panel
        (e.g. "rsi", "macd"). Each unique string gets its own dedicated panel
        below the chart, sharing only the x (time) axis. Two instances
        returning the same string share one panel.
        """
        return RENDER_CHART

    def series_fills(self) -> list[SeriesFill]:
        """
        Declare shaded fills between pairs of series produced by compute().

        Override this to have the chart draw a translucent fill between two
        named series (e.g. Bollinger Bands upper and lower). Returns an empty
        list by default — chart indicators need not override it.

        Note: fill rendering support in PlotManager is planned but not yet
        implemented. Declaring fills here is forward-compatible; they will
        be drawn automatically once support is added.
        """
        return []
