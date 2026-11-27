"""
indicators/sma.py

Simple Moving Average indicator.

One SMAIndicator instance represents one MA line on the chart. To display
multiple MAs (e.g. 50-day and 200-day simultaneously), the controller adds
two separate instances with different "days" params. The registry maps the
name "sma" to this class; the chart distinguishes individual lines by the
series key returned from compute() — e.g. "sma_50" vs "sma_200".

The "days" parameter counts trading sessions. Each intraday value averages
the prior completed daily closes in the window and the current bar's close.
Each weekly value uses the latest daily close in that week, including the
forming daily row when available.
"""

from typing import Any

import numpy as np

from data.models import Timeframe
from indicators._day_based import project_daily_references
from simplechart.api import (
    ChoiceParam,
    ChartExtension,
    LINE_STYLE_OPTIONS,
    OHLCVSeries,
    bars_for_n_days,
    register_extension,
)

class SMAIndicator(ChartExtension):

    def name(self) -> str:
        return "sma"

    def label(self) -> str:
        return "Simple Moving Average"

    def default_params(self) -> dict[str, Any]:
        return {
            "days": 50,
            "color": "#00BFFF",
            "line_width": 1.0,
            "line_style": ChoiceParam("solid", LINE_STYLE_OPTIONS),
            "smooth_intraday": False,
        }

    def history_lookback_days(self, params: dict[str, Any]) -> int | None:
        # A window SMA is exact once its full window of bars is loaded.
        return int(params["days"])

    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        days: int = int(params["days"])
        smooth_intraday: bool = params.get("smooth_intraday", False)
        if series.timeframe != Timeframe.DAILY:
            values = project_daily_references(
                "sma", series, params.get("_daily_bars") or [], days,
                params.get("_daily_reference_start"),
                smooth_intraday=smooth_intraday,
            )
        else:
            period = bars_for_n_days(days, series.timeframe)
            closes = np.array([b.close for b in series.bars], dtype=float)
            values = _sma(closes, period)
        return {f"sma_{days}": values}


register_extension(SMAIndicator)


def _sma(closes: np.ndarray, period: int) -> np.ndarray:
    """
    Simple Moving Average using the cumulative sum trick — O(n) overall.

    result[i] = mean of closes[i - period + 1 : i + 1]

    The first valid value is at index period - 1. All prior values are NaN.
    """
    n: int = len(closes)
    result: np.ndarray = np.full(n, np.nan)

    if period < 1 or period > n:
        return result

    # Prepend 0.0 so cumsum[i] = sum of closes[0 : i].
    # Window sum for bar i (i >= period-1):
    #   sum(closes[i-period+1 : i+1]) = cumsum[i+1] - cumsum[i+1-period]
    padded: np.ndarray = np.concatenate(([0.0], closes.astype(float)))
    cumsum: np.ndarray = np.cumsum(padded)
    result[period - 1:] = (cumsum[period:] - cumsum[:n - period + 1]) / period

    return result
