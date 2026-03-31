"""
plugins/example_plugin.py

EXAMPLE INDICATOR PLUGIN — copy this file as a starting point.

This file implements a Relative Strength Index (RSI) indicator as a
complete, working example of the plugin system. Every decision is
explained so you understand not just what to do but why.

To use this as a template:
  1. Copy this file to plugins/builtin/your_indicator.py
  2. Replace RSIIndicator with your class name
  3. Replace the name(), label(), default_params(), and compute() bodies
  4. If you added a kernel in indicators/_fast/, update the import and call
  5. Add the import to plugins/builtin/__init__.py
  6. Optionally add to DEFAULT_INDICATORS in app/controller.py

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FULL CHECKLIST FOR A NEW INDICATOR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  [ ] Create plugins/builtin/your_indicator.py (copy this file)
  [ ] Implement name(), label(), default_params(), compute()
  [ ] Call register(YourIndicator) at the bottom of the file
  [ ] If using a kernel: create indicators/_fast/your_kernel.py
  [ ] If using a kernel: add it to [tool.simplechart.compile] in pyproject.toml
  [ ] If using a kernel: run python scripts/build_compiled.py
  [ ] Add import to plugins/builtin/__init__.py
  [ ] Optionally add to DEFAULT_INDICATORS in app/controller.py
  [ ] Test: launch the app, load a symbol, verify the indicator appears

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ABOUT RSI (the example indicator)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RSI (Relative Strength Index) measures momentum by comparing the
magnitude of recent gains to recent losses. It oscillates between 0
and 100. Readings above 70 are conventionally overbought; below 30
is oversold.

Formula (Wilder's smoothing method):
  For each bar, compute the change from the previous close:
    gain = max(change, 0)
    loss = max(-change, 0)

  Seed: average gain/loss over the first `period` bars (simple average)
  Then for each subsequent bar:
    avg_gain = (prev_avg_gain * (period - 1) + gain) / period
    avg_loss = (prev_avg_loss * (period - 1) + loss) / period
    rs  = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

RSI is a standalone-panel indicator (it doesn't overlay the price chart)
but for simplicity this example plots it on the price panel. In a real
implementation you would add a separate panel — see chart/panel.py.
"""

from typing import Any

import numpy as np

from data.calendar import bars_for_n_days
from data.models import OHLCVSeries
from indicators.base import Indicator
from indicators.registry import register


class RSIIndicator(Indicator):
    """
    Relative Strength Index.

    Uses day-based period consistent with the rest of SimpleChart —
    a "14-day RSI" means 14 trading days regardless of the active
    timeframe, not 14 bars.
    """

    # ----------------------------------------------------------------
    # Indicator ABC — these four methods are required
    # ----------------------------------------------------------------

    def name(self) -> str:
        """
        Machine-readable key. Must be unique across all registered
        indicators. Used as the registry key and as a prefix in the
        series keys returned by compute().
        """
        return "rsi"

    def label(self) -> str:
        """Human-readable name shown in the legend and config dialog."""
        return "Relative Strength Index (RSI)"

    def default_params(self) -> dict[str, Any]:
        """
        Default configuration. The config dialog builds its form from
        this dict automatically:
          int   → spin box
          str starting with "#" → color picker
          float → decimal spin box
          str   → text field

        Keep param names snake_case — the dialog converts them to
        Title Case for display ("overbought_level" → "Overbought Level").
        """
        return {
            "days":              14,        # RSI period in trading days
            "color":             "#DA70D6", # orchid — stands out on white bg
            "overbought_level":  70.0,      # drawn as a reference line (future)
            "oversold_level":    30.0,      # drawn as a reference line (future)
        }

    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        """
        Compute RSI and return it as a named array.

        The series key includes the day count so that two RSI instances
        with different periods (e.g. RSI-14 and RSI-21) can coexist on
        the same chart as distinct, independently toggleable lines.

        Steps:
          1. Convert "days" to a bar count for the active timeframe
          2. Extract closes as a numpy array
          3. Compute RSI using Wilder's smoothing method
          4. Return under a stable, unique key

        This indicator's computation is simple enough that it does not
        need a compiled kernel — numpy handles the array operations and
        a short Python loop runs the Wilder smoothing. If profiling ever
        shows RSI is a bottleneck, move the loop to indicators/_fast/.
        """
        days: int   = int(params["days"])
        period: int = bars_for_n_days(days, series.timeframe)
        closes: np.ndarray = np.array(
            [bar.close for bar in series.bars], dtype=float
        )
        values: np.ndarray = _compute_rsi(closes, period)

        # Series key pattern: "{name}_{days}" — same convention as SMA/EMA.
        return {f"rsi_{days}": values}


# ----------------------------------------------------------------
# Computation — defined as a module-level function rather than a
# method so it can be moved to indicators/_fast/ later if needed
# without changing the Indicator class.
# ----------------------------------------------------------------

def _compute_rsi(closes: np.ndarray, period: int) -> np.ndarray:
    """
    Compute RSI using Wilder's smoothing method.

    Returns an array of the same length as closes. The first (period)
    values are NaN — RSI needs `period` bars to seed its averages.
    """
    n: int = len(closes)
    result: np.ndarray = np.full(n, np.nan)

    if period < 1 or n < period + 1:
        return result

    # Price changes: positive = gain, negative = loss.
    # changes[0] is the change from closes[0] to closes[1].
    changes: np.ndarray = np.diff(closes)   # length n-1

    # Seed: simple average of the first `period` gains and losses.
    seed_gains = changes[:period].clip(min=0.0)
    seed_losses = (-changes[:period]).clip(min=0.0)
    avg_gain: float = float(np.mean(seed_gains))
    avg_loss: float = float(np.mean(seed_losses))

    # First RSI value is at index `period` (we consumed bars 0..period-1
    # for the seed, so the first RSI corresponds to bar index `period`).
    if avg_loss == 0.0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100.0 - (100.0 / (1.0 + rs))

    # Wilder's smoothing for the remaining bars.
    for i in range(period, n - 1):
        change: float = float(changes[i])
        gain:   float = change if change > 0.0 else 0.0
        loss:   float = -change if change < 0.0 else 0.0

        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0.0:
            result[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i + 1] = 100.0 - (100.0 / (1.0 + rs))

    return result


# ----------------------------------------------------------------
# Registration — always the last line of a plugin file.
# This runs when the module is imported, which happens at app
# startup via plugins/builtin/__init__.py.
# ----------------------------------------------------------------

register(RSIIndicator)
