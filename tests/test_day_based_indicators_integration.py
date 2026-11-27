from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import app.controller as controller
import indicators.avwap  # noqa: F401
import indicators.ema  # noqa: F401
import indicators.rsi  # noqa: F401
import indicators.sma  # noqa: F401
from app.controller import MainWindow
from app.state import ChartExtensionState
from data.aggregator import Aggregator
from data.models import Bar, OHLCVSeries, Timeframe
from simplechart.api import ChartExtensionMutation, get_extension
from test_basis_change import (  # noqa: F401  (fixture)
    ET,
    _SYMBOL,
    _YAHOO,
    _AdjustedSource,
    _at,
    _FixedDateTime,
    _isolated_finplot_windows,
    _ms,
    _settled,
    _show,
    _window,
)
from test_live_refresh import _production_refresh
from test_older_history import _FLOOR, _FlooredSource, _alpaca_window

_NAMES = ("sma", "ema", "rsi")


def _configure(window: MainWindow, days: int) -> None:
    window._state.extensions = [
        ChartExtensionState(name, get_extension(name).default_params() | {"days": days})
        for name in _NAMES
    ]


def _capture_plots(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch,
) -> dict[str, np.ndarray]:
    plots: dict[str, np.ndarray] = {}
    pm = window._chart.plot_manager
    update = pm.update_extension

    def capture(
        key: str, values: np.ndarray, color: str, width: float, style: str, target: str,
    ) -> None:
        update(key, values, color, width, style, target)
        plots[key] = values.copy()

    monkeypatch.setattr(pm, "update_extension", capture)
    return plots


def _price_bar(stamp: datetime, close: float) -> Bar:
    return Bar(stamp.astimezone(UTC), close, close + 1, close - 1, close, 1_000)


def _daily(day: date, close: float) -> Bar:
    return _price_bar(datetime.combine(day, time(0), tzinfo=UTC), close)


def _indicator_values(plots: dict[str, np.ndarray], days: int) -> np.ndarray:
    return np.stack([plots[f"{name}_{days}"] for name in _NAMES])


@pytest.mark.parametrize("timeframe", [
    Timeframe.MIN5, Timeframe.MIN15, Timeframe.MIN30, Timeframe.MIN39, Timeframe.MIN65,
])
def test_main_window_renders_live_daily_state_on_intraday_bars(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeframe: Timeframe,
) -> None:
    window = _window(qtbot, tmp_path / "chart.db", monkeypatch, _AdjustedSource(None))
    _configure(window, 3)
    plots = _capture_plots(window, monkeypatch)
    early_close = date(2025, 11, 28)
    monday = date(2025, 12, 1)
    references = [
        _daily(date(2025, 11, 24), 10), _daily(date(2025, 11, 25), 12),
        _daily(date(2025, 11, 26), 11), _daily(early_close, 14), _daily(monday, 12),
    ]
    source = Aggregator(_AdjustedSource(None))
    minutes = timeframe.minutes
    assert minutes is not None
    bars = []
    for day, length, opening, closing in [(early_close, 210, 8, 14), (monday, 390, 16, 12)]:
        base_minutes = 5 if timeframe in (Timeframe.MIN39, Timeframe.MIN65) else minutes
        session = [
            _price_bar(_at(day) + timedelta(minutes=offset), opening)
            for offset in range(0, length, base_minutes)
        ]
        session[-1] = replace(
            session[-1], close=closing, high=max(opening, closing) + 1,
            low=min(opening, closing) - 1,
        )
        if timeframe in (Timeframe.MIN39, Timeframe.MIN65):
            session = source.synthesize_cached_bars(session, timeframe)
        bars.extend(session)
        if day == early_close:
            half_day_count = len(bars)
    series = OHLCVSeries(_SYMBOL, timeframe, bars)
    window._cache.put_bars(_YAHOO, _SYMBOL, Timeframe.DAILY, references[:-1])
    window._render(series)
    live = _indicator_values(plots, 3)

    np.testing.assert_allclose(live[:, [0, half_day_count - 1, half_day_count, -1]], [
        [31 / 3, 37 / 3, 41 / 3, 37 / 3],
        [9.5, 12.5, 14.25, 12.25],
        [100 * 2 / 6, 100 * 5 / 6, 100 * 16 / 18, 100 * 5 / 9],
    ])
    window._cache.put_bars(_YAHOO, _SYMBOL, Timeframe.DAILY, [_daily(monday, 999)])
    window._render(series)
    np.testing.assert_array_equal(_indicator_values(plots, 3), live)
    window._chart.clear_all()
    window._render(OHLCVSeries(_SYMBOL, Timeframe.DAILY, references))
    np.testing.assert_allclose(
        live[:, [half_day_count - 1, -1]], _indicator_values(plots, 3)[:, -2:],
    )
    assert all("_daily_bars" not in state.params for state in window._state.extensions)


class _LimitedDailySource(_AdjustedSource):
    floor = datetime(2024, 1, 2, tzinfo=UTC)

    def earliest_history_start(self, timeframe: Timeframe, end: datetime) -> datetime | None:
        if timeframe == Timeframe.DAILY:
            return self.floor
        return super().earliest_history_start(timeframe, end)


def test_daily_prefix_and_values_survive_intraday_backfill_and_pan(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _LimitedDailySource(None)
    window = _window(qtbot, tmp_path / "chart.db", monkeypatch, source)
    plots = _capture_plots(window, monkeypatch)
    start = date(2026, 6, 1)
    recent_dates = [
        day for offset in range(77)
        if (day := start + timedelta(days=offset)).weekday() < 5
    ]
    assert len(recent_dates) == 55
    daily = [_daily(source.floor.date(), 80), _daily(date(2024, 1, 3), 30)] + [
        _daily(day, float(50 + i % 9)) for i, day in enumerate(recent_dates)
    ]
    route = window._selected_route
    window._cache.put_bars(
        route.cache_namespace, _SYMBOL, Timeframe.DAILY, [_daily(date(2023, 12, 29), 200), *daily],
    )
    now = _FixedDateTime.now(tz=UTC)
    window._cache.replace_bar_fetch_coverage(
        route.cache_namespace, _SYMBOL, Timeframe.DAILY, _ms(source.floor), _ms(now),
    )
    assert _show(qtbot, window, Timeframe.DAILY)[:len(daily)] == daily
    _configure(window, 14)
    window._reload_extensions(draw_bars=False)
    expected = _indicator_values(plots, 14)[:, len(daily) - 1]
    assert np.isfinite(expected).all()
    window._chart.clear_all()

    intraday = [
        _price_bar(_at(day, 15, 55), bar.close)
        for day, bar in zip(recent_dates, daily[-55:], strict=True)
    ]
    one_day = OHLCVSeries(_SYMBOL, Timeframe.MIN5, intraday[-1:])
    window._current_series = one_day
    window._render(one_day)
    first = _indicator_values(plots, 14)[:, -1]
    np.testing.assert_allclose(first, expected, rtol=1e-13)

    full = OHLCVSeries(_SYMBOL, Timeframe.MIN5, intraday)
    window._apply_history_merge(full)
    np.testing.assert_array_equal(_indicator_values(plots, 14)[:, -1], first)

    # Older intraday coverage can extend before the ordinary Daily fetch floor.
    panned = OHLCVSeries(_SYMBOL, Timeframe.MIN5, [
        _price_bar(_at(date(2023, 12, 29)), 200),
        _price_bar(_at(source.floor.date()), daily[0].close),
        *intraday,
    ])
    window._apply_history_merge(panned)
    np.testing.assert_array_equal(_indicator_values(plots, 14)[:, -1], first)


def test_sma_200_renders_on_view_only_request_and_avwap_still_loads_older_history(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _FlooredSource(_FLOOR)
    window = _alpaca_window(qtbot, tmp_path / "chart.db", monkeypatch, source)
    timeframe = Timeframe.MIN5
    # Leave history that an on-demand AVWAP request can reach within its cap.
    monkeypatch.setattr(controller, "_DEFAULT_LOOKBACK_DAYS", 120)
    params = get_extension("sma").default_params() | {"days": 200}
    monkeypatch.setattr(controller, "INITIAL_EXTENSIONS", [("sma", params)])
    plots = _capture_plots(window, monkeypatch)
    recent: list[tuple[Bar, int | None, float]] = []
    render = window._render

    def capture_recent(series: OHLCVSeries, history_complete: bool = True) -> None:
        render(series, history_complete)
        if not history_complete:
            values = plots.get("sma_200", np.array([np.nan]))
            recent.append((
                series.bars[-1], window._extension_runtime.pending_history_start_ms,
                float(values[-1]),
            ))

    monkeypatch.setattr(window, "_render", capture_recent)
    _show(qtbot, window, timeframe)
    request = window._current_chart_request
    assert request is not None
    now = _FixedDateTime.now(tz=UTC)
    floor = controller._history_start(timeframe, window._selected_route.aggregator, now)
    expected_start = controller._recent_history_start(timeframe, floor, now, 0)
    first_request = next(start for tf, start, _ in source.calls if tf == timeframe)
    assert (request.lookback_days, first_request) == (0, expected_start)
    [(last_bar, pending, last_value)] = recent
    assert pending is None
    prior = [
        bar.close for bar in window._cache.get_bars(
            request.cache_namespace, _SYMBOL, Timeframe.DAILY,
            _ms(controller._DAILY_HISTORY_START), _ms(now),
        )
        if bar.timestamp.date() < last_bar.timestamp.astimezone(ET).date()
    ]
    assert len(prior) >= 199
    assert last_value == pytest.approx((sum(prior[-199:]) + last_bar.close) / 200)

    source.calls.clear()
    anchor = _at(date(2026, 5, 15))
    window._extension_store.apply(ChartExtensionMutation("avwap", "add_anchor", {
        "anchor_ts": _ms(anchor), "label": "Older anchor", "color": "#4488aa",
    }))
    window._reload_extensions(draw_bars=False)
    assert window._extension_runtime.pending_history_start_ms == _ms(anchor)

    def anchor_plotted() -> bool:
        return any(key.startswith("avwap_") for key in window._chart.plot_manager.active_series_keys())

    qtbot.waitUntil(anchor_plotted, timeout=20_000)
    bars = _settled(qtbot, window)
    assert bars[0].timestamp <= anchor
    assert any(tf == timeframe and start < floor for tf, start, _ in source.calls)


class _LateDailySource(_AdjustedSource):
    missing_day = date(2026, 10, 23)
    include_missing_day = False

    def fetch_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime,
    ) -> list[Bar]:
        bars = super().fetch_bars(symbol, timeframe, start, end)
        if timeframe == Timeframe.DAILY and not self.include_missing_day:
            return [bar for bar in bars if bar.timestamp.date() != self.missing_day]
        return bars


def test_daily_reference_arrival_on_live_poll_redraws_unchanged_candles(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _LateDailySource(timedelta(days=55))
    window = _window(qtbot, tmp_path / "chart.db", monkeypatch, source)
    now = _at(date(2026, 10, 26), 10, 30)
    monkeypatch.setattr(_FixedDateTime, "frozen", now)
    monkeypatch.setattr(MainWindow, "_refresh_live_bars", _production_refresh)
    plots = _capture_plots(window, monkeypatch)
    candles = _show(qtbot, window, Timeframe.MIN5)
    _configure(window, 3)
    window._reload_extensions(draw_bars=False)
    assert any(bar.timestamp.date() == source.missing_day for bar in candles)
    cached = window._cache.get_bars(_YAHOO, _SYMBOL, Timeframe.DAILY, 0, _ms(now))
    assert not any(bar.timestamp.date() == source.missing_day for bar in cached)
    before = _indicator_values(plots, 3)[:, -1]

    # The reference feed's coverage is still behind its missing session.
    window._cache.replace_bar_fetch_coverage(
        _YAHOO, _SYMBOL, Timeframe.DAILY, _ms(controller._DAILY_HISTORY_START),
        _ms(_at(source.missing_day - timedelta(days=1), 16, 5)),
    )
    source.include_missing_day = True
    window._refresh_live_bars()
    assert _settled(qtbot, window) == candles
    cached = window._cache.get_bars(_YAHOO, _SYMBOL, Timeframe.DAILY, 0, _ms(now))
    assert any(bar.timestamp.date() == source.missing_day for bar in cached)
    last = _indicator_values(plots, 3)[:, -1]

    assert np.isnan(before).all()
    assert np.isfinite(last).all()


def test_main_window_renders_weekly_values_from_each_weeks_last_daily_bar(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _LimitedDailySource(None)
    window = _window(qtbot, tmp_path / "chart.db", monkeypatch, source)
    today = date(2025, 12, 30)
    monkeypatch.setattr(_FixedDateTime, "frozen", _at(today, 11, 30))
    _configure(window, 3)
    plots = _capture_plots(window, monkeypatch)

    week_starts = [date(2025, 11, 24) + timedelta(weeks=i) for i in range(6)]
    sessions_by_week = [
        (0, 1, 2, 4),  # Thanksgiving week ends with a half day.
        (0, 1, 2, 3, 4),
        (0, 1, 2, 3, 4),
        (),  # This weekly candle has no daily references.
        (0, 1, 2, 4),  # Christmas week ends with a full session.
        (0, 1),  # Tuesday's daily row is still forming.
    ]
    references = [
        _daily(monday + timedelta(days=offset), 20 + (i * 7 + offset * 3) % 11)
        for i, (monday, sessions) in enumerate(zip(week_starts, sessions_by_week, strict=True))
        for offset in sessions
    ]
    window._render(OHLCVSeries(_SYMBOL, Timeframe.DAILY, references))
    daily_values = _indicator_values(plots, 3)
    window._chart.clear_all()
    window._cache.put_bars(_YAHOO, _SYMBOL, Timeframe.DAILY, [
        _daily(source.floor.date() - timedelta(days=1), 500), *references,
    ])

    weekly = [_daily(monday, 100 + i) for i, monday in enumerate(week_starts)]
    window._render(OHLCVSeries(_SYMBOL, Timeframe.WEEKLY, weekly))
    np.testing.assert_allclose(
        _indicator_values(plots, 3),
        np.column_stack((
            daily_values[:, [3, 8, 13]], np.full(3, np.nan), daily_values[:, [17, 19]],
        )),
        rtol=1e-13,
    )


def test_weekly_load_fetches_daily_floor_without_requesting_indicator_history(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _LimitedDailySource(None)
    window = _window(qtbot, tmp_path / "chart.db", monkeypatch, source)
    monkeypatch.setattr(controller, "INITIAL_EXTENSIONS", [
        (name, get_extension(name).default_params() | {"days": 200}) for name in _NAMES
    ])
    plots = _capture_plots(window, monkeypatch)
    _show(qtbot, window, Timeframe.WEEKLY)

    now = _FixedDateTime.now(tz=UTC)
    assert (Timeframe.DAILY, source.floor, now) in source.calls
    request = window._current_chart_request
    assert request is not None and request.lookback_days == 0
    assert np.isfinite(_indicator_values(plots, 200)[:, -1]).all()


class _LiveWeeklyReferenceSource(_LimitedDailySource):
    partial_close = 125.0

    def fetch_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime,
    ) -> list[Bar]:
        bars = super().fetch_bars(symbol, timeframe, start, end)
        if timeframe == Timeframe.DAILY and bars and bars[-1].timestamp.date() == end.date():
            bars[-1] = _price_bar(bars[-1].timestamp, self.partial_close)
        return bars


def test_weekly_live_poll_redraws_when_only_the_partial_daily_reference_changes(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _LiveWeeklyReferenceSource(None)
    window = _window(qtbot, tmp_path / "chart.db", monkeypatch, source)
    today = date(2026, 10, 26)
    now = _at(today, 10, 30)
    monkeypatch.setattr(_FixedDateTime, "frozen", now)
    monkeypatch.setattr(MainWindow, "_refresh_live_bars", _production_refresh)
    plots = _capture_plots(window, monkeypatch)
    candles = _show(qtbot, window, Timeframe.WEEKLY)
    _configure(window, 3)
    window._reload_extensions(draw_bars=False)
    before = _indicator_values(plots, 3)

    source.calls.clear()
    source.partial_close = 115.0
    window._refresh_live_bars()
    assert _settled(qtbot, window) == candles
    assert any(tf == Timeframe.DAILY for tf, _, _ in source.calls)
    references = window._cache.get_bars(
        _YAHOO, _SYMBOL, Timeframe.DAILY, _ms(source.floor), _ms(now),
    )
    assert references[-1].timestamp.date() == today
    assert references[-1].close == source.partial_close
    after = _indicator_values(plots, 3)
    np.testing.assert_array_equal(after[:, :-1], before[:, :-1])
    assert (after[:, -1] != before[:, -1]).all()
    daily = OHLCVSeries(_SYMBOL, Timeframe.DAILY, references)
    np.testing.assert_allclose(after[:, -1], [
        indicator.compute(daily, indicator.default_params() | {"days": 3})[f"{indicator.name()}_3"][-1]
        for indicator in map(get_extension, _NAMES)
    ], rtol=1e-13)
