"""
History wave H-S1: render and indicator paths cost O(1) Python work per bar
less, with identical output (docs/agentic-projects/
codebase-review-fixes-2026-11-10.md, "History wave"). These are
characterization tests: they pin today's outputs.
"""

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

import indicators.ema as ema_module
import indicators.pivot_points as pivot_module
import indicators.sma as sma_module
import tools._polyline.base_tool as polyline_module
import tools.fib_retracement as fib_module
import tools.vertical_line as vertical_line_module
from chart.plot_manager import _series_to_candle_df, _series_to_volume_df
from indicators.ema._kernel import ema as ema_kernel
from app.state import ChartExtensionState
from data.models import Bar, OHLCVSeries, Timeframe
from test_basis_change import (  # noqa: F401  (fixture)
    _SYMBOL,
    _AdjustedSource,
    _isolated_finplot_windows,
    _seed_old_basis_5m,
    _window,
)

ET = ZoneInfo("America/New_York")


def _intraday(days: list[date]) -> list[Bar]:
    bars: list[Bar] = []
    for day in days:
        session_open = datetime.combine(day, time(9, 30), tzinfo=ET)
        for slot in range(78):
            price = 100.0 + len(bars) * 0.01
            bars.append(
                Bar(
                    (session_open + timedelta(minutes=5 * slot)).astimezone(timezone.utc),
                    price,
                    price + 1.0,
                    price - 1.0,
                    price + 0.5,
                    1000 + slot,
                    price + 0.25,
                )
            )
    return bars


def _daily(days: list[date]) -> list[Bar]:
    return [
        Bar(
            datetime.combine(day, time(0), tzinfo=ET).astimezone(timezone.utc),
            50.0 + index,
            52.0 + index,
            49.0 + index,
            51.0 + index,
            10_000 + index,
        )
        for index, day in enumerate(days)
    ]


def _weekdays(first: date, count: int) -> list[date]:
    days: list[date] = []
    day = first
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _expected_index(series: OHLCVSeries) -> pd.DatetimeIndex:
    stamps = []
    for bar in series.bars:
        stamp = pd.Timestamp(bar.timestamp).tz_convert("UTC")
        if series.timeframe in (Timeframe.DAILY, Timeframe.WEEKLY):
            stamp = stamp.normalize() + pd.Timedelta(hours=12)
        stamps.append(stamp)
    return pd.DatetimeIndex(stamps, dtype="datetime64[ns, UTC]")


def _assert_frames(series: OHLCVSeries) -> None:
    index = _expected_index(series)
    candles = _series_to_candle_df(series)
    volume = _series_to_volume_df(series)
    pd.testing.assert_frame_equal(
        candles,
        pd.DataFrame(
            {
                "open": [bar.open for bar in series.bars],
                "close": [bar.close for bar in series.bars],
                "high": [bar.high for bar in series.bars],
                "low": [bar.low for bar in series.bars],
            },
            index=index,
        ),
    )
    pd.testing.assert_frame_equal(
        volume,
        pd.DataFrame(
            {
                "open": [bar.open for bar in series.bars],
                "close": [bar.close for bar in series.bars],
                "volume": [bar.volume for bar in series.bars],
            },
            index=index,
        ),
    )


def test_intraday_plot_frames_are_unchanged() -> None:
    _assert_frames(OHLCVSeries("X", Timeframe.MIN5, _intraday(_weekdays(date(2026, 3, 5), 4))))


def test_daily_and_weekly_plot_frames_are_unchanged() -> None:
    # Includes days either side of a daylight-saving change and before 1970.
    days = [date(1962, 1, 2), date(1969, 12, 31)] + _weekdays(date(2026, 3, 5), 6)
    _assert_frames(OHLCVSeries("X", Timeframe.DAILY, _daily(days)))
    mondays = [day for day in _weekdays(date(2025, 12, 29), 40) if day.weekday() == 0]
    _assert_frames(OHLCVSeries("X", Timeframe.WEEKLY, _daily(mondays)))


def test_empty_plot_frames() -> None:
    # The empty volume column is now int64 (it was pandas' float64 default
    # for an empty list); nothing is drawn either way.
    series = OHLCVSeries("X", Timeframe.MIN5, [])
    candles = _series_to_candle_df(series)
    volume = _series_to_volume_df(series)
    assert list(candles.columns) == ["open", "close", "high", "low"] and candles.empty
    assert list(volume.columns) == ["open", "close", "volume"] and volume.empty
    assert str(candles.index.dtype) == "datetime64[ns, UTC]"


def _reference_fill(
    intraday_bars: list[Bar],
    values: np.ndarray,
    daily_values: dict[date, float],
) -> np.ndarray:
    filled = values.copy()
    for index, bar in enumerate(intraday_bars):
        if np.isnan(filled[index]):
            day = bar.timestamp.astimezone(ET).date()
            if day in daily_values:
                filled[index] = daily_values[day]
    return filled


def test_moving_average_warmup_fill_is_unchanged() -> None:
    days = _weekdays(date(2026, 1, 5), 30)
    daily = _daily(days)
    intraday = _intraday(days[-3:])
    for module, average in (
        (sma_module, sma_module._sma),
        (ema_module, ema_kernel),
    ):
        values = np.full(len(intraday), np.nan)
        values[100:] = 7.0
        daily_average = average(np.array([bar.close for bar in daily]), 5)
        daily_values = {
            bar.timestamp.astimezone(ET).date(): float(value)
            for bar, value in zip(daily, daily_average)
            if not np.isnan(value)
        }
        expected = _reference_fill(intraday, values, daily_values)

        module._fill_warmup_from_daily(intraday, values, 5, daily)

        np.testing.assert_array_equal(values, expected)


def test_pivot_session_ranges_are_unchanged() -> None:
    days = _weekdays(date(2026, 3, 2), 8)
    intraday = OHLCVSeries("X", Timeframe.MIN5, _intraday(days))
    daily = OHLCVSeries("X", Timeframe.DAILY, _daily(days))
    sessions = set(days[-3:])

    intraday_ranges = pivot_module._session_ranges(intraday.bars, sessions, True)
    daily_ranges = pivot_module._session_ranges(daily.bars, sessions, False)

    assert {
        day: (segment.x_start, segment.x_end, segment.label_index)
        for day, segment in intraday_ranges.items()
    } == {
        day: (float(78 * index), float(78 * index + 78), 78 * index)
        for index, day in enumerate(days)
        if day in sessions
    }
    assert {
        day: (segment.x_start, segment.x_end, segment.label_index)
        for day, segment in daily_ranges.items()
    } == {
        day: (index - 0.45, index + 0.45, index)
        for index, day in enumerate(days)
        if day in sessions
    }


def test_tool_series_timestamps_are_unchanged() -> None:
    series = OHLCVSeries("X", Timeframe.MIN5, _intraday(_weekdays(date(2026, 3, 5), 2)))
    expected = [int(bar.timestamp.timestamp() * 1000) for bar in series.bars]
    for module in (vertical_line_module, polyline_module, fib_module):
        timestamps = module._series_timestamps(series)
        assert timestamps == expected
        assert all(type(value) is int for value in timestamps)


def test_a_redraw_before_any_series_is_shown_waits_for_the_load(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HS-1: Add Extension during the first load used to read the cache on the
    # UI thread and draw indicator lines with no candles.
    db_path = tmp_path / "test.db"
    _seed_old_basis_5m(db_path)
    window = _window(qtbot, db_path, monkeypatch, _AdjustedSource(None))
    window._state.symbol = _SYMBOL
    window._state.timeframe = Timeframe.MIN5
    window._state.extensions.append(
        ChartExtensionState(name="sma", params={"days": 5})
    )
    reads: list[object] = []
    production_get_bars = window._cache.get_bars

    def get_bars(*args: Any) -> list[Bar]:
        reads.append(args)
        return production_get_bars(*args)

    monkeypatch.setattr(window._cache, "get_bars", get_bars)

    window._reload_extensions(draw_bars=False)

    assert reads == []
    assert window._current_series is None
    assert window._chart.plot_manager.active_series_keys() == []
