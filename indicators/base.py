"""
indicators/base.py

Abstract base class for all indicators.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW THE INDICATOR SYSTEM WORKS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

There are three layers involved in every indicator:

  1. The Indicator subclass (this file / plugins/)
     - Implements the Indicator ABC
     - Responsible for extracting arrays from OHLCVSeries (e.g. closes,
       volumes) and delegating to the compiled kernels
     - Plain Python — not compiled

  2. The compiled kernels (indicators/_fast/)
     - Pure numeric functions: numpy array(s) in, numpy array out
     - No I/O, no dicts, no strings, no dynamic dispatch
     - Compiled to native extensions via mypyc for maximum speed
     - See indicators/_fast/ for the actual math

  3. The registry (indicators/registry.py)
     - Maps indicator names to Indicator classes
     - Built-in indicators are registered at import time
     - Plugin authors register their indicators the same way

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
THE MYPYC BOUNDARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

mypyc compiles Python source to C extensions. It is not magic — it works
by requiring strict, static typing with no dynamic dispatch. The rule is:

  Code that CAN be compiled lives in indicators/_fast/ (or any _fast/
  subpackage in any layer). It must:
    - Use concrete types only (no Any, no Union in hot paths)
    - Accept and return numpy arrays or plain scalars
    - Perform no I/O (no file access, no SQLite, no network)
    - Use no ABCMeta, no dynamic attribute setting

  Code that CANNOT be compiled (this file, registry.py, Indicator
  subclasses) lives outside _fast/. It handles the "glue": extracting
  arrays from OHLCVSeries, calling compiled functions, packing results
  back into dicts.

To compile the _fast/ modules, run:
    python scripts/build_compiled.py

During development you can skip compilation entirely — the .py source
files in _fast/ run as normal Python. The compiled .so extensions are
drop-in replacements; Python imports the .so if present, falls back to
the .py if not.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WRITING A NEW INDICATOR — COMPLETE STEP-BY-STEP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

See plugins/example_plugin.py for a fully worked example you can copy.

STEP 1 — Create the plugin file
  Built-in indicators (shipped with the app):
      plugins/builtin/your_indicator.py

  Third-party / contributed indicators:
      Any .py file on the Python path that calls register() at import
      time. No changes to the core codebase needed.

STEP 2 — Subclass Indicator and implement the four methods

  from indicators.base import Indicator
  from indicators.registry import register

  class MyIndicator(Indicator):
      def name(self)          -> str:   return "my_indicator"
      def label(self)         -> str:   return "My Indicator"
      def default_params(self)-> dict:  return {"days": 14, "color": "#DA70D6"}
      def compute(self, series, params) -> dict[str, np.ndarray]:
          ...
          return {"my_indicator_14": values}

  register(MyIndicator)   # <-- always at the bottom of the file

STEP 3 — If the indicator needs a compiled kernel (optional)

  Only needed if the computation involves a tight loop over thousands
  of bars. Simple indicators can call numpy directly from compute()
  without a separate kernel.

  If you do need a kernel, create it in indicators/_fast/your_kernel.py
  and follow ALL of these rules (required for mypyc to compile it):

    ✓ Every parameter and return value must have an explicit type annotation
    ✓ Accept numpy arrays (np.ndarray) and plain Python scalars (int, float)
    ✓ Return np.ndarray (or list[np.ndarray] for multiple outputs)
    ✓ Use float() and int() to convert numpy scalars in loop bodies —
      mypyc generates tighter C for Python builtins than numpy scalars
    ✓ Pre-compute any loop-invariant values outside the loop
    ✗ No I/O of any kind (no file access, no SQLite, no network, no print)
    ✗ No Any, no Union in function signatures
    ✗ No ABCMeta, no dynamic attribute access (no getattr/setattr)
    ✗ No **kwargs, no default mutable arguments

  Then add the module path to pyproject.toml so the build script picks
  it up:

    [tool.simplechart.compile]
    targets = [
        "indicators._fast.ma",
        "indicators._fast.avwap",
        "indicators._fast.your_kernel",   # <-- add this line
    ]

  Run the build:
      python scripts/build_compiled.py

  The compiled .so file is placed next to the .py source. Delete the
  .so at any time to revert to interpreted mode — no other changes needed.

STEP 4 — Register the import (built-ins only)

  Add one line to plugins/builtin/__init__.py:
      from plugins.builtin import your_indicator  # noqa: F401

  This ensures the register() call at the bottom of your module fires
  at app startup. Third-party plugins handle their own import — they
  are not added here.

STEP 5 — Add to DEFAULT_INDICATORS (optional)

  If the indicator should appear on every chart automatically, add it
  to DEFAULT_INDICATORS in app/controller.py:
      ("my_indicator", {"days": 14, "color": "#DA70D6"}),

  Otherwise it is available in the registry but not added by default.

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
from dataclasses import dataclass
from typing import Any

import numpy as np

from data.models import OHLCVSeries


LINE_STYLE_OPTIONS: list[str] = ["solid", "dash", "dot", "dash_dot"]


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

        Delegate heavy numeric work to the compiled kernels in
        indicators/_fast/. This method is called on every symbol load
        and on timeframe switches, so it should return quickly.
        """
