"""
indicators/rsi/__init__.py

Relative Strength Index (RSI) indicator.

RSI measures momentum by comparing the magnitude of recent gains to recent
losses. It oscillates between 0 and 100. Readings above 70 are conventionally
overbought; below 30 is oversold.

RSI is a panel indicator — it draws in a dedicated panel below the price
chart rather than overlaying the price axis. See render_target().
"""

from typing import Any

import numpy as np

from indicators.rsi._kernel import rsi as _rsi_kernel
from simplechart.api import ChartExtension, OHLCVSeries, bars_for_n_days, register_extension


class RSIIndicator(ChartExtension):

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
            f"rsi_{days}":                 _rsi_kernel(closes, period),
            f"rsi_{days}_ref_overbought": np.full(n, float(params["overbought_level"])),
            f"rsi_{days}_ref_oversold":   np.full(n, float(params["oversold_level"])),
        }


register_extension(RSIIndicator)
