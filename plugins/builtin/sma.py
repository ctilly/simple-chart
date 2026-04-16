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

import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from data.calendar import bars_for_n_days
from data.models import Bar, OHLCVSeries
from indicators._fast.ma import sma as _sma_kernel
from indicators.base import ChoiceParam, Indicator, LINE_STYLE_OPTIONS
from indicators.registry import register

_ET = ZoneInfo("America/New_York")


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
        days: int = int(params["days"])
        period: int = bars_for_n_days(days, series.timeframe)
        closes: np.ndarray = np.array([b.close for b in series.bars], dtype=float)
        values: np.ndarray = _sma_kernel(closes, period)

        # Fill the leading NaN zone (where intraday history is too short for
        # the full warmup) with the daily SMA value for each trading day.
        # Intraday bars that already have a valid value are not touched.
        daily_bars: list[Bar] = params.get("_daily_bars") or []
        if series.timeframe.is_intraday and daily_bars:
            _fill_warmup_from_daily(series.bars, values, days, daily_bars)

        return {f"sma_{days}": values}


register(SMAIndicator)


def _fill_warmup_from_daily(
    intraday_bars: list[Bar],
    values: np.ndarray,
    days: int,
    daily_bars: list[Bar],
) -> None:
    """
    Fill NaN slots in `values` (the leading warmup zone) with the daily SMA
    for the corresponding trading day. Only touches bars where values[i] is
    NaN — bars with valid intraday SMA values are left unchanged.
    """
    daily_closes: np.ndarray = np.array([b.close for b in daily_bars], dtype=float)
    daily_sma: np.ndarray = _sma_kernel(daily_closes, days)

    by_date: dict[datetime.date, float] = {}
    for bar, val in zip(daily_bars, daily_sma):
        if not np.isnan(val):
            by_date[bar.timestamp.astimezone(_ET).date()] = float(val)

    for i, bar in enumerate(intraday_bars):
        if np.isnan(values[i]):
            d = bar.timestamp.astimezone(_ET).date()
            if d in by_date:
                values[i] = by_date[d]
