"""
indicators/ema/__init__.py

Exponential Moving Average indicator.

Follows the same day-based convention as SMAIndicator — the "days" param
is trading days, not bars. See indicators/sma.py for a full explanation
of why this matters.

EMA gives more weight to recent bars than SMA does. The smoothing factor
k = 2 / (period + 1) means the most recent bar has the highest influence
and older bars decay exponentially.
"""

import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from data.calendar import bars_for_n_days
from data.models import Bar, OHLCVSeries
from indicators._base import ChoiceParam, Indicator, LINE_STYLE_OPTIONS
from indicators._registry import register
from indicators.ema._kernel import ema as _ema_kernel

_ET = ZoneInfo("America/New_York")


class EMAIndicator(Indicator):

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
        }

    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        days: int = int(params["days"])
        period: int = bars_for_n_days(days, series.timeframe)
        closes: np.ndarray = np.array([b.close for b in series.bars], dtype=float)
        values: np.ndarray = _ema_kernel(closes, period)

        daily_bars: list[Bar] = params.get("_daily_bars") or []
        if series.timeframe.is_intraday and daily_bars:
            _fill_warmup_from_daily(series.bars, values, days, daily_bars)

        return {f"ema_{days}": values}


register(EMAIndicator)


def _fill_warmup_from_daily(
    intraday_bars: list[Bar],
    values: np.ndarray,
    days: int,
    daily_bars: list[Bar],
) -> None:
    """
    Fill NaN slots in `values` (the leading warmup zone) with the daily EMA
    for the corresponding trading day. Only touches bars where values[i] is
    NaN — bars with valid intraday EMA values are left unchanged.
    """
    daily_closes: np.ndarray = np.array([b.close for b in daily_bars], dtype=float)
    daily_ema: np.ndarray = _ema_kernel(daily_closes, days)

    by_date: dict[datetime.date, float] = {}
    for bar, val in zip(daily_bars, daily_ema):
        if not np.isnan(val):
            by_date[bar.timestamp.astimezone(_ET).date()] = float(val)

    for i, bar in enumerate(intraday_bars):
        if np.isnan(values[i]):
            d = bar.timestamp.astimezone(_ET).date()
            if d in by_date:
                values[i] = by_date[d]
