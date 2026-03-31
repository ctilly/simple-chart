"""
plugins/builtin/ema.py

Exponential Moving Average indicator.

Follows the same day-based convention as SMAIndicator — the "days" param
is trading days, not bars. See plugins/builtin/sma.py for a full
explanation of why this matters.

EMA gives more weight to recent bars than SMA does. The smoothing factor
k = 2 / (period + 1) means the most recent bar has the highest influence
and older bars decay exponentially.
"""

from typing import Any

import numpy as np

from data.calendar import bars_for_n_days
from data.models import OHLCVSeries
from indicators._fast.ma import ema as _ema_kernel
from indicators.base import Indicator
from indicators.registry import register


class EMAIndicator(Indicator):

    def name(self) -> str:
        return "ema"

    def label(self) -> str:
        return "Exponential Moving Average"

    def default_params(self) -> dict[str, Any]:
        return {
            "days": 20,
            "color": "#FF8C00",
        }

    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        days: int = int(params["days"])
        period: int = bars_for_n_days(days, series.timeframe)
        closes: np.ndarray = np.array([bar.close for bar in series.bars], dtype=float)
        values: np.ndarray = _ema_kernel(closes, period)
        return {f"ema_{days}": values}


register(EMAIndicator)
