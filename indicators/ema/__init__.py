"""
indicators/ema/__init__.py

Exponential Moving Average indicator.

The "days" parameter counts trading sessions. Each intraday bar applies one
EMA step to the prior completed daily state using the current bar's close.
Each weekly value uses the latest daily close in that week, including the
forming daily row when available.
The smoothing factor k = 2 / (period + 1) gives recent closes more weight.
"""

from typing import Any

import numpy as np

from data.models import Timeframe
from indicators._day_based import project_daily_references
from indicators.ema._kernel import ema as _ema_kernel
from simplechart.api import (
    ChoiceParam,
    ChartExtension,
    LINE_STYLE_OPTIONS,
    OHLCVSeries,
    bars_for_n_days,
    register_extension,
)

_CONVERGENCE_PERIODS = 4


class EMAIndicator(ChartExtension):

    def name(self) -> str:
        return "ema"

    def label(self) -> str:
        return "Exponential Moving Average"

    def default_params(self) -> dict[str, Any]:
        return {
            "days": 20,
            "color": "#FF8C00",
            "line_width": 1.0,
            "line_style": ChoiceParam("solid", LINE_STYLE_OPTIONS),
            "smooth_intraday": False,
        }

    def history_lookback_days(self, params: dict[str, Any]) -> int | None:
        # The recurrence never fully forgets its seed; after this many periods
        # the seed's weight is (1 - 2 / (p + 1)) ** (4p) ~= e**-8, so values on
        # the visible bars match full-history values.
        return int(params["days"]) * _CONVERGENCE_PERIODS

    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        days: int = int(params["days"])
        smooth_intraday: bool = params.get("smooth_intraday", False)
        if series.timeframe != Timeframe.DAILY:
            values = project_daily_references(
                "ema", series, params.get("_daily_bars") or [], days,
                params.get("_daily_reference_start"),
                smooth_intraday=smooth_intraday,
            )
        else:
            period = bars_for_n_days(days, series.timeframe)
            closes = np.array([b.close for b in series.bars], dtype=float)
            values = _ema_kernel(closes, period)
        return {f"ema_{days}": values}


register_extension(EMAIndicator)
