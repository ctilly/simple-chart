from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

import numpy as np
import pytest

from data.aggregator import resample
from data.models import Timeframe
from data.session import MARKET_TIMEZONE
from indicators.ema._kernel import ema
from indicators.rsi._kernel import rsi
from indicators.sma import _sma
from simplechart.api import Bar


_KINDS: tuple[Literal["sma", "ema", "rsi"], ...] = ("sma", "ema", "rsi")
_WEDNESDAY = date(2026, 11, 25)
_FRIDAY = date(2026, 11, 27)
_MONDAY = date(2026, 11, 30)
_SESSIONS = [date(2026, 11, 23), date(2026, 11, 24), _WEDNESDAY, _FRIDAY, _MONDAY]
_CLOSES = np.array([10.0, 12.0, 11.0, 14.0, 12.0])


def _daily_timestamps(days: list[date], *, et_midnight: bool = False) -> np.ndarray:
    zone = MARKET_TIMEZONE if et_midnight else UTC
    return np.array(
        [int(datetime.combine(day, time.min, zone).timestamp() * 1000) for day in days],
        dtype=np.int64,
    )


def _timestamps(bars: list[Bar]) -> np.ndarray:
    return np.array([int(bar.timestamp.timestamp() * 1000) for bar in bars], dtype=np.int64)


def _ohlcv(day: date, prices: list[float], *, minutes: int = 5) -> list[Bar]:
    opening = datetime.combine(day, time(9, 30), MARKET_TIMEZONE).astimezone(UTC)
    return [
        Bar(opening + timedelta(minutes=i * minutes), close, close, close, close, 100)
        for i, close in enumerate(prices)
    ]


def _project(
    daily_timestamps_ms: np.ndarray,
    daily_closes: np.ndarray,
    bars: list[Bar],
    period: int,
    *,
    known_session_dates: list[date] | None = None,
) -> np.ndarray:
    from indicators._day_based import project_day_based

    timestamps = _timestamps(bars)
    closes = np.array([bar.close for bar in bars], dtype=float)
    known_dates = (
        None if known_session_dates is None
        else np.array(known_session_dates, dtype="datetime64[D]")
    )
    return np.stack([
        project_day_based(
            kind, daily_timestamps_ms, daily_closes, timestamps, closes, period,
            known_session_dates=known_dates,
        )
        for kind in _KINDS
    ])


@pytest.mark.parametrize("et_midnight", [False, True], ids=["utc-midnight", "et-midnight"])
@pytest.mark.parametrize("period", [1, 2, 3])
def test_last_intraday_bar_matches_daily_kernels(et_midnight: bool, period: int) -> None:
    bars = [
        bar
        for day, close in zip(_SESSIONS, _CLOSES, strict=True)
        for bar in _ohlcv(day, [float(close) + 0.75, float(close)])
    ]
    actual = _project(_daily_timestamps(_SESSIONS, et_midnight=et_midnight), _CLOSES, bars, period)
    expected = np.stack([_sma(_CLOSES, period), ema(_CLOSES, period), rsi(_CLOSES, period)])

    np.testing.assert_allclose(actual[:, 1::2], expected, rtol=1e-13, atol=1e-13)


def test_live_values_use_completed_daily_state_and_current_close() -> None:
    # Thursday was closed. The third close is the EMA seed and the fourth is
    # the RSI seed. Monday uses the official Friday close of 14.
    bars = (
        _ohlcv(_WEDNESDAY, [14.0, 11.0])
        + _ohlcv(_FRIDAY, [8.0, 14.0])
        + _ohlcv(_MONDAY, [16.0, 12.0])
    )

    actual = _project(_daily_timestamps(_SESSIONS), _CLOSES, bars, 3)

    np.testing.assert_allclose(actual, [
        [12.0, 11.0, 31 / 3, 37 / 3, 41 / 3, 37 / 3],
        [12.0, 11.0, 9.5, 12.5, 14.25, 12.25],
        [np.nan, np.nan, 100 * 2 / 6, 100 * 5 / 6, 100 * 16 / 18, 100 * 5 / 9],
    ], rtol=1e-13, atol=1e-13)


def test_same_day_partial_row_and_future_bars_cannot_change_earlier_values() -> None:
    morning = _ohlcv(_FRIDAY, [8.0, 13.0])
    later = _ohlcv(_MONDAY, [12.0])
    no_partial = _project(_daily_timestamps(_SESSIONS[:3]), _CLOSES[:3], morning, 3)
    with_partial = _project(
        _daily_timestamps(_SESSIONS[:4]), np.array([10.0, 12.0, 11.0, 999.0]), morning, 3,
    )
    with_future = _project(_daily_timestamps(_SESSIONS), _CLOSES, morning + later, 3)

    assert np.isfinite(no_partial).all()
    np.testing.assert_array_equal(with_partial, no_partial)
    np.testing.assert_array_equal(with_future[:, :len(morning)], no_partial)


def test_intraday_history_length_and_missing_bars_do_not_change_values() -> None:
    # The 55-day fixture crosses the March daylight-saving transition.
    dates = [date(2026, 2, 2) + timedelta(days=i) for i in range(55)]
    sessions = [day for day in dates if day.weekday() < 5 and day != date(2026, 2, 16)]
    closes = 50.0 + np.sin(np.arange(len(sessions), dtype=float))
    daily_timestamps = _daily_timestamps(sessions, et_midnight=True)
    by_day = [
        _ohlcv(day, [float(close + 1), float(close - 1), float(close)])
        for day, close in zip(sessions, closes, strict=True)
    ]
    full = [bar for day_bars in by_day for bar in day_bars]
    sparse = [
        bar for i, day_bars in enumerate(by_day) if i % 2 == 0 for bar in day_bars[::2]
    ]
    expected = _project(daily_timestamps, closes, full, 14)

    for sample in (by_day[-1], sparse):
        actual = _project(daily_timestamps, closes, sample, 14)
        positions = np.searchsorted(_timestamps(full), _timestamps(sample))
        np.testing.assert_array_equal(actual, expected[:, positions])

    daily_expected = np.stack([_sma(closes, 14), ema(closes, 14), rsi(closes, 14)])
    np.testing.assert_allclose(expected[:, 2::3], daily_expected, rtol=1e-13, atol=1e-13)


@pytest.mark.parametrize(
    ("timeframe", "base_minutes", "expected_counts"),
    [(Timeframe.MIN39, 1, (10, 6)), (Timeframe.MIN65, 5, (6, 4))],
)
def test_synthesized_full_and_half_day_buckets_land_on_daily_values(
    timeframe: Timeframe, base_minutes: int, expected_counts: tuple[int, int],
) -> None:
    full = _ohlcv(_WEDNESDAY, [9.0] * (390 // base_minutes - 1) + [11.0], minutes=base_minutes)
    half = _ohlcv(_FRIDAY, [13.0] * (210 // base_minutes - 1) + [14.0], minutes=base_minutes)
    full_buckets = resample(full, timeframe)
    half_buckets = resample(half, timeframe)
    assert (len(full_buckets), len(half_buckets)) == expected_counts
    timestamps = _daily_timestamps(_SESSIONS)
    projected = _project(timestamps, _CLOSES, full_buckets + half_buckets, 2)
    daily_values = np.stack([_sma(_CLOSES, 2), ema(_CLOSES, 2), rsi(_CLOSES, 2)])
    np.testing.assert_allclose(
        projected[:, [len(full_buckets) - 1, -1]], daily_values[:, [2, 3]],
        rtol=1e-13, atol=1e-13,
    )

    monday = _ohlcv(_MONDAY, [16.0, 12.0])
    with_half_day = _project(timestamps, _CLOSES, full_buckets + half_buckets + monday, 2)
    without_half_day = _project(timestamps, _CLOSES, full_buckets + monday, 2)
    np.testing.assert_array_equal(with_half_day[:, -len(monday):], without_half_day[:, -len(monday):])


def test_missing_prior_session_returns_nan_until_daily_reference_arrives() -> None:
    observed_days = _SESSIONS[:3] + [_MONDAY]
    observed_closes = _CLOSES[[0, 1, 2, 4]]
    bars = _ohlcv(_FRIDAY, [14.0]) + _ohlcv(_MONDAY, [12.0])
    actual = _project(
        _daily_timestamps(observed_days), observed_closes, bars, 3, known_session_dates=_SESSIONS,
    )

    np.testing.assert_allclose(actual[:, 0], [37 / 3, 12.5, 100 * 5 / 6], rtol=1e-13)
    assert np.isnan(actual[:, 1]).all()
    restored = _project(
        _daily_timestamps(_SESSIONS), _CLOSES, bars, 3, known_session_dates=_SESSIONS,
    )
    np.testing.assert_allclose(restored[:, 1], [37 / 3, 12.25, 100 * 5 / 9], rtol=1e-13)


def test_older_known_gap_only_masks_values_that_depend_on_it() -> None:
    bars = _ohlcv(_MONDAY, [12.0])
    actual = _project(
        _daily_timestamps(_SESSIONS[1:]), _CLOSES[1:], bars, 3, known_session_dates=_SESSIONS,
    )

    np.testing.assert_allclose(actual[:, 0], [37 / 3, np.nan, np.nan], rtol=1e-13)


@pytest.mark.parametrize("prior_close", [10.0, 12.0], ids=["flat", "rising"])
def test_zero_loss_rsi_returns_100_at_seed_and_afterward(prior_close: float) -> None:
    days = _SESSIONS[:4]
    closes = np.array([10.0, prior_close, prior_close, prior_close])
    bars = _ohlcv(days[2], [prior_close]) + _ohlcv(days[3], [prior_close + 1.0])

    np.testing.assert_array_equal(_project(_daily_timestamps(days), closes, bars, 2)[2], [100, 100])


def test_empty_daily_history_still_allows_first_close_to_seed_one_day_averages() -> None:
    prices = [12.0, 13.0]
    actual = _project(_daily_timestamps([]), np.array([]), _ohlcv(_MONDAY, prices), 1)

    np.testing.assert_array_equal(actual, [prices, prices, [np.nan, np.nan]])


def test_projected_rsi_uses_double_precision_with_float32_closes() -> None:
    from indicators._day_based import project_day_based

    value = project_day_based(
        "rsi", _daily_timestamps(_SESSIONS[:4]), np.array([10, 12, 11, 14], dtype=np.float32),
        _timestamps(_ohlcv(_MONDAY, [12.0])), np.array([12], dtype=np.float32), 3,
    )

    np.testing.assert_allclose(value, [100 * 5 / 9], rtol=1e-13)


def test_wilder_averages_expose_daily_seed_and_smoothed_state() -> None:
    from indicators.rsi._kernel import wilder_averages

    avg_gains, avg_losses = wilder_averages(_CLOSES, 3)

    np.testing.assert_allclose(avg_gains, [np.nan, np.nan, np.nan, 5 / 3, 10 / 9], rtol=1e-13)
    np.testing.assert_allclose(avg_losses, [np.nan, np.nan, np.nan, 1 / 3, 8 / 9], rtol=1e-13)
