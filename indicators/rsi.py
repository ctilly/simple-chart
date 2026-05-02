"""
indicators/rsi.py

Relative Strength Index (RSI) indicator.

RSI measures momentum by comparing the magnitude of recent gains to recent
losses. It oscillates between 0 and 100. Readings above 70 are conventionally
overbought; below 30 is oversold.

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

RSI is a panel indicator — it draws in a dedicated panel below the price
chart rather than overlaying the price axis. See render_target().
"""

from typing import Any

import numpy as np

from data.calendar import bars_for_n_days
from data.models import OHLCVSeries
from indicators._base import Indicator
from indicators._registry import register


class RSIIndicator(Indicator):

    def name(self) -> str:
        return "rsi"

    def label(self) -> str:
        return "Relative Strength Index (RSI)"

    def default_params(self) -> dict[str, Any]:
        return {
            "days":             14,        # RSI period in trading days
            "color":            "#DA70D6", # orchid
            "overbought_level": 70.0,
            "oversold_level":   30.0,
        }

    def render_target(self) -> str:
        return "rsi"

    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        days: int   = int(params["days"])
        period: int = bars_for_n_days(days, series.timeframe)
        n: int      = len(series.bars)
        closes: np.ndarray = np.array(
            [bar.close for bar in series.bars], dtype=float
        )
        return {
            f"rsi_{days}":              _compute_rsi(closes, period),
            f"rsi_{days}_ref_overbought": np.full(n, float(params["overbought_level"])),
            f"rsi_{days}_ref_oversold":   np.full(n, float(params["oversold_level"])),
        }


register(RSIIndicator)


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
    changes: np.ndarray = np.diff(closes)   # length n-1

    # Seed: simple average of the first `period` gains and losses.
    seed_gains = changes[:period].clip(min=0.0)
    seed_losses = (-changes[:period]).clip(min=0.0)
    avg_gain: float = float(np.mean(seed_gains))
    avg_loss: float = float(np.mean(seed_losses))

    # First RSI value is at index `period`.
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
