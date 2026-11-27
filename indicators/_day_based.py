from datetime import UTC, date, datetime, time
from typing import Literal, cast

import numpy as np
import pandas as pd

from data.calendar import session_date_anchor
from data.models import Timeframe
from data.session import MARKET_TIMEZONE, REGULAR_SESSION_MINUTES, REGULAR_SESSION_OPEN
from simplechart.api import Bar, OHLCVSeries, series_arrays


def project_daily_references(
    indicator: Literal["sma", "ema", "rsi"],
    series: OHLCVSeries,
    daily_bars: list[Bar],
    period: int,
    reference_start: date | None,
    *,
    smooth_intraday: bool = False,
) -> np.ndarray:
    daily_timestamps_ms = np.fromiter(
        (int(bar.timestamp.timestamp() * 1000) for bar in daily_bars),
        dtype=np.int64,
        count=len(daily_bars),
    )
    daily_closes = np.fromiter(
        (bar.close for bar in daily_bars), dtype=float, count=len(daily_bars),
    )
    if series.timeframe == Timeframe.WEEKLY:
        last_by_week = {
            session_date_anchor(bar.timestamp.astimezone(UTC).date(), Timeframe.WEEKLY): bar
            for bar in daily_bars
        }
        week_indices: list[int] = []
        weekly_samples: list[Bar] = []
        for index, bar in enumerate(series.bars):
            week = session_date_anchor(bar.timestamp.astimezone(UTC).date(), Timeframe.WEEKLY)
            daily_bar = last_by_week.get(week)
            if daily_bar is not None:
                week_indices.append(index)
                weekly_samples.append(daily_bar)

        # Daily rows are UTC date anchors. Give the projection timestamps
        # whose ET dates match those session dates, even at UTC midnight.
        sample_timestamps_ms = np.array([
            int(datetime.combine(
                bar.timestamp.astimezone(UTC).date(), time.min, tzinfo=MARKET_TIMEZONE,
            ).timestamp() * 1000)
            for bar in weekly_samples
        ], dtype=np.int64)
        values = np.full(len(series.bars), np.nan)
        values[week_indices] = project_day_based(
            indicator, daily_timestamps_ms, daily_closes, sample_timestamps_ms,
            np.array([bar.close for bar in weekly_samples], dtype=float), period,
        )
        return values

    samples = series_arrays(series)
    values = project_day_based(
        indicator,
        daily_timestamps_ms,
        daily_closes,
        samples.timestamps_ms,
        samples.close,
        period,
        known_session_dates=np.unique(_sample_session_dates(samples.timestamps_ms)),
        reference_start=reference_start,
    )
    if smooth_intraday and indicator != "rsi" and (minutes := series.timeframe.minutes) is not None:
        if indicator == "sma":
            from indicators.sma import _sma

            daily_values = _sma(daily_closes, period)
        else:
            from indicators.ema._kernel import ema

            daily_values = ema(daily_closes, period)
        return _smooth_intraday(
            values, samples.timestamps_ms, daily_timestamps_ms, daily_values,
            period, minutes, reference_start,
        )
    return values


def _regular_session_minutes(session_date: date) -> int:
    weekday = session_date.weekday()
    # The Friday after the fourth Thursday falls on November 23 through 29.
    if session_date.month == 11 and 23 <= session_date.day <= 29 and weekday == 4:
        return 210
    if weekday < 4 and (session_date.month, session_date.day) in ((7, 3), (12, 24)):
        return 210
    return REGULAR_SESSION_MINUTES


def _smooth_intraday(
    live: np.ndarray,
    timestamps_ms: np.ndarray,
    daily_timestamps_ms: np.ndarray,
    daily_values: np.ndarray,
    period: int,
    bar_minutes: int,
    reference_start: date | None,
) -> np.ndarray:
    values = np.full(len(live), np.nan)
    if len(live) == 0 or not 0 < period <= len(daily_values):
        return values

    local_times = _sample_session_times(timestamps_ms).to_numpy(dtype="datetime64[ms]")
    sample_dates = local_times.astype("datetime64[D]")
    sessions, first, session_index = np.unique(
        sample_dates, return_index=True, return_inverse=True,
    )
    last = np.r_[first[1:] - 1, len(live) - 1]
    open_minutes = REGULAR_SESSION_OPEN.hour * 60 + REGULAR_SESSION_OPEN.minute
    bar_ends = (local_times - sample_dates) / np.timedelta64(1, "m") - open_minutes + bar_minutes
    today_date = datetime.now(MARKET_TIMEZONE).date()
    today = np.datetime64(today_date, "D")
    # For prior sessions, the last bucket's nominal end accounts for a half day.
    session_lengths = np.where(
        sessions < today,
        np.minimum(bar_ends[last], REGULAR_SESSION_MINUTES),
        REGULAR_SESSION_MINUTES,
    )
    session_lengths[sessions == today] = _regular_session_minutes(today_date)

    daily_dates = daily_timestamps_ms.astype("datetime64[ms]").astype("datetime64[D]")
    completed = np.searchsorted(daily_dates, sample_dates, side="left")
    ready = np.flatnonzero(completed >= period)
    # An SMA anchor needs one more completed close than the live SMA does.
    missing = _missing_session_dates(sessions, daily_dates, reference_start)
    before_sample = np.searchsorted(missing, sample_dates[ready], side="left")
    before_window = np.searchsorted(missing, daily_dates[completed[ready] - period], side="left")
    ready = ready[before_sample == before_window]

    anchors = daily_values[completed[ready] - 1]
    fractions = np.clip(bar_ends[ready] / session_lengths[session_index[ready]], 0, 1)
    live_ready = live[ready]
    # Use the live close directly to preserve exact day-close parity.
    values[ready] = np.where(
        (fractions == 1) & np.isfinite(anchors),
        live_ready,
        anchors + fractions * (live_ready - anchors),
    )
    return values


def _sample_session_times(timestamps_ms: np.ndarray) -> pd.DatetimeIndex:
    times_et = pd.to_datetime(timestamps_ms, unit="ms", utc=True).tz_convert(MARKET_TIMEZONE)
    return cast(pd.DatetimeIndex, times_et.tz_localize(None))


def _sample_session_dates(timestamps_ms: np.ndarray) -> np.ndarray:
    return cast(np.ndarray, _sample_session_times(timestamps_ms).to_numpy(dtype="datetime64[D]"))


def _missing_session_dates(
    known_session_dates: np.ndarray, daily_dates: np.ndarray, reference_start: date | None,
) -> np.ndarray:
    missing = np.setdiff1d(known_session_dates, daily_dates)
    if reference_start is not None:
        missing = missing[missing >= np.datetime64(reference_start, "D")]
    return missing


def project_day_based(
    indicator: Literal["sma", "ema", "rsi"],
    daily_timestamps_ms: np.ndarray,
    daily_closes: np.ndarray,
    sample_timestamps_ms: np.ndarray,
    sample_closes: np.ndarray,
    period: int,
    *,
    known_session_dates: np.ndarray | None = None,
    reference_start: date | None = None,
) -> np.ndarray:
    """Evaluate the daily indicator as of each sample's ET date and close.

    Daily references are oldest-first, one row per UTC date anchor. Samples
    carry the timestamp and close of the session to evaluate. Supply any
    independently known sessions in the daily reference range as datetime64[D]
    dates so absent trading sessions can be detected. A reference start excludes
    older known sessions that precede the Daily chart's calculation history.
    """
    values = np.full(len(sample_closes), np.nan)
    if period < 1 or len(sample_closes) == 0:
        return values
    if period == 1 and indicator != "rsi":
        return sample_closes.astype(float)

    daily_dates = daily_timestamps_ms.astype("datetime64[ms]").astype("datetime64[D]")
    sample_dates = _sample_session_dates(sample_timestamps_ms)
    completed = np.searchsorted(daily_dates, sample_dates, side="left")
    required = period if indicator == "rsi" else period - 1
    ready = np.flatnonzero(completed >= required)

    if known_session_dates is not None:
        missing = _missing_session_dates(known_session_dates, daily_dates, reference_start)
        missing_before_sample = np.searchsorted(missing, sample_dates[ready], side="left")
        if indicator == "sma":
            window_start = daily_dates[completed[ready] - required]
            missing_before_window = np.searchsorted(missing, window_start, side="left")
            ready = ready[missing_before_sample == missing_before_window]
        else:
            # A missing session in the seed or recurrence invalidates the state.
            ready = ready[missing_before_sample == 0]

    if not ready.size:
        return values

    counts = completed[ready]
    prices = sample_closes[ready]
    if indicator == "sma":
        prefix = np.cumsum(np.concatenate(([0.0], daily_closes)))
        values[ready] = (prefix[counts] + prices - prefix[counts - required]) / period
    elif indicator == "ema":
        values[ready] = _project_ema(daily_closes, counts, prices, period)
    else:
        values[ready] = _project_rsi(daily_closes, counts, prices, period)
    return values


def _project_ema(
    daily_closes: np.ndarray, counts: np.ndarray, prices: np.ndarray, period: int,
) -> np.ndarray:
    # Import the kernel here because the indicator package imports this module.
    from indicators.ema._kernel import ema

    seed = counts == period - 1
    seeded = np.empty(len(prices), dtype=float)
    seeded[seed] = (daily_closes[:period - 1].sum() + prices[seed]) / period

    warmed = ~seed
    if warmed.any():
        prior_ema = ema(daily_closes, period)[counts[warmed] - 1]
        k = 2.0 / (period + 1)
        seeded[warmed] = k * prices[warmed] + (1.0 - k) * prior_ema
    return seeded


def _project_rsi(
    daily_closes: np.ndarray, counts: np.ndarray, prices: np.ndarray, period: int,
) -> np.ndarray:
    from indicators.rsi._kernel import rsi_from_averages, wilder_averages, wilder_seed_sums

    previous = counts - 1
    changes = prices.astype(float, copy=False) - daily_closes[previous]
    gains = np.maximum(changes, 0.0)
    losses = np.maximum(-changes, 0.0)

    seed = counts == period
    if seed.any():
        gain_sum, loss_sum = wilder_seed_sums(daily_closes, period - 1)
        gains[seed] += gain_sum
        losses[seed] += loss_sum

    warmed = ~seed
    if warmed.any():
        daily_gains, daily_losses = wilder_averages(daily_closes, period)
        gains[warmed] += daily_gains[previous[warmed]] * (period - 1)
        losses[warmed] += daily_losses[previous[warmed]] * (period - 1)
    return rsi_from_averages(gains / period, losses / period)
