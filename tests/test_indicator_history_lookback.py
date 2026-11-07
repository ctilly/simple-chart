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


def _random_walk_series(sessions: int, seed: int) -> OHLCVSeries:
    rng = np.random.default_rng(seed)
    count = bars_for_n_days(sessions, Timeframe.MIN5)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.002, count)))
    first = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    bars = [
        Bar(first + timedelta(minutes=5 * i), c, c * 1.001, c * 0.999, c, 1_000)
        for i, c in enumerate(closes)
    ]
    return OHLCVSeries("TEST", Timeframe.MIN5, bars)


@pytest.mark.parametrize(
    ("name", "days"),
    [("sma", 50), ("ema", 20), ("ema", 50), ("rsi", 14)],
)
def test_declared_lookback_reproduces_full_history_on_the_initial_view(
    name: str,
    days: int,
) -> None:
    extension = get_extension(name)
    params = {**extension.default_params(), "days": days}
    lookback_days = extension.history_lookback_days(params)
    assert lookback_days is not None
    needed = bars_for_n_days(lookback_days, Timeframe.MIN5) + _VISIBLE

    for seed in range(3):
        full = _random_walk_series(lookback_days + 60, seed)
        truncated = OHLCVSeries(full.symbol, full.timeframe, full.bars[-needed:])
        key = f"{name}_{days}"
        full_values = extension.compute(full, params)[key][-_VISIBLE:]
        truncated_values = extension.compute(truncated, params)[key][-_VISIBLE:]

        assert not np.isnan(truncated_values).any()
        relative = np.abs(full_values - truncated_values) / np.abs(full_values)
        # Within 0.01% on every bar of the initial view.
        assert float(relative.max()) < 1e-4, (name, days, seed)
