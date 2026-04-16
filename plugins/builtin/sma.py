"""
plugins/builtin/sma.py

Simple Moving Average indicator.

One SMAIndicator instance represents one MA line on the chart. To display
multiple MAs (e.g. 50-day and 200-day simultaneously), the controller adds
two separate instances with different "days" params. The registry maps the
name "sma" to this class; the chart distinguishes individual lines by the
series key returned from compute() — e.g. "sma_50" vs "sma_200".

Day-based vs period-based:
    The "days" param means trading days, not bars. bars_for_n_days() converts
    the day count to the correct bar count for whatever timeframe is active.
    This is what keeps a 50-day SMA at the same price value regardless of
    whether you are viewing a daily chart or a 5m chart.
"""

from typing import Any

import numpy as np

from data.calendar import bars_for_n_days
from data.models import OHLCVSeries
from indicators._fast.ma import sma as _sma_kernel
from indicators.base import ChoiceParam, Indicator, LINE_STYLE_OPTIONS
from indicators.registry import register


class SMAIndicator(Indicator):

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
        }

    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        """
        Compute a day-based SMA over the loaded bar series.

        Steps:
          1. Convert the day count to a bar count for the active timeframe
          2. Extract closes as a numpy array
          3. Delegate to the compiled SMA kernel
          4. Return under a key that encodes the day count

        The series key "sma_{days}" is stable — it does not change when
        the timeframe changes, only when the user edits the "days" param.
        The chart layer uses this key to update the existing plot line
        rather than creating a new one on timeframe switches.
        """
        days: int = int(params["days"])
        period: int = bars_for_n_days(days, series.timeframe)
        closes: np.ndarray = np.array([bar.close for bar in series.bars], dtype=float)
        values: np.ndarray = _sma_kernel(closes, period)
        return {f"sma_{days}": values}


register(SMAIndicator)
