"""
indicators/rsi/__init__.py

Relative Strength Index (RSI) indicator.

RSI measures momentum by comparing the magnitude of recent gains to recent
losses. It oscillates between 0 and 100. Readings above 70 are conventionally
overbought; below 30 is oversold.

The "days" parameter counts trading sessions. Each intraday bar applies
Wilder smoothing to the prior completed daily state using the change from
the previous daily close to the current bar's close.
Each weekly value uses the latest daily close in that week, including the
forming daily row when available.

RSI is a panel indicator — it draws in a dedicated panel below the price
chart rather than overlaying the price axis. See render_target().
"""

from typing import Any

import numpy as np

from data.models import Timeframe
from indicators._day_based import project_daily_references
from indicators.rsi._kernel import rsi as _rsi_kernel
from simplechart.api import ChartExtension, OHLCVSeries, bars_for_n_days, register_extension

_CONVERGENCE_PERIODS = 8


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

    def history_lookback_days(self, params: dict[str, Any]) -> int | None:
        # Wilder smoothing forgets its seed half as fast as an EMA (alpha = 1/p
        # instead of 2/(p+1)); after this many periods the seed's weight is
        # (1 - 1/p) ** (8p) ~= e**-8, so values on the visible bars match
        # full-history values.
        return int(params["days"]) * _CONVERGENCE_PERIODS

    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        days = int(params["days"])
        n = len(series.bars)
        if series.timeframe != Timeframe.DAILY:
            values = project_daily_references(
                "rsi", series, params.get("_daily_bars") or [], days,
                params.get("_daily_reference_start"),
            )
        else:
            period = bars_for_n_days(days, series.timeframe)
            closes = np.array([bar.close for bar in series.bars], dtype=float)
            values = _rsi_kernel(closes, period)
        return {
            f"rsi_{days}":                 values,
            f"rsi_{days}_ref_overbought": np.full(n, float(params["overbought_level"])),
            f"rsi_{days}_ref_oversold":   np.full(n, float(params["oversold_level"])),
        }


register_extension(RSIIndicator)
