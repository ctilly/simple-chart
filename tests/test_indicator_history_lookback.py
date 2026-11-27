from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

import indicators.ema  # noqa: F401
import indicators.rsi  # noqa: F401
import indicators.sma  # noqa: F401
from chart.window import INITIAL_VISIBLE_BARS
from data.models import Bar, OHLCVSeries, Timeframe
from simplechart.api import bars_for_n_days, get_extension

_VISIBLE = INITIAL_VISIBLE_BARS


def _random_walk_series(sessions: int, seed: int) -> tuple[OHLCVSeries, list[Bar]]:
    rng = np.random.default_rng(seed)
    bars_per_session = bars_for_n_days(1, Timeframe.MIN5)
    changes = rng.normal(0.0, 0.002, sessions * bars_per_session)
    closes = (100.0 * np.exp(np.cumsum(changes))).reshape(sessions, bars_per_session)
    first = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    bars: list[Bar] = []
    daily_bars: list[Bar] = []
    for prices in closes:
        bars.extend(
            Bar(first + timedelta(minutes=5 * i), c, c * 1.001, c * 0.999, c, 1_000)
            for i, c in enumerate(prices)
        )
        close = prices[-1]
        daily_bars.append(Bar(
            first.replace(hour=0, minute=0), close, close, close, close, 78_000,
        ))
        first += timedelta(days=3 if first.weekday() == 4 else 1)
    return OHLCVSeries("TEST", Timeframe.MIN5, bars), daily_bars


@pytest.mark.parametrize(
    ("name", "days"),
    [("sma", 50), ("ema", 20), ("ema", 50), ("rsi", 14)],
)
def test_daily_references_reproduce_full_history_on_the_initial_view(
    name: str,
    days: int,
) -> None:
    extension = get_extension(name)
    params = {**extension.default_params(), "days": days}
    lookback_days = extension.history_lookback_days(params)
    assert lookback_days is not None
    for seed in range(3):
        full, daily_bars = _random_walk_series(lookback_days + 60, seed)
        truncated = OHLCVSeries(full.symbol, full.timeframe, full.bars[-_VISIBLE:])
        params["_daily_bars"] = daily_bars
        key = f"{name}_{days}"
        full_values = extension.compute(full, params)[key][-_VISIBLE:]
        truncated_values = extension.compute(truncated, params)[key][-_VISIBLE:]

        assert not np.isnan(truncated_values).any()
        np.testing.assert_array_equal(truncated_values, full_values)
