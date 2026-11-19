"""
Live bar refresh: the displayed chart polls for its newest bars during the
regular session (docs/agentic-projects/live-bar-refresh.md).

Reuses the frozen clock and fake sources of test_basis_change.py, moved to
a Monday session. _LiveSource returns the bar whose period contains the
request end as a forming bar: its close tracks the request end.
"""

import logging
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

import app.controller as controller_module
from app.controller import MainWindow, _live_refresh_due
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
from test_basis_change import (  # noqa: F401  (fixture)
    ET,
    _SYMBOL,
    _WEDNESDAY,
    _WEDNESDAY_CLOSE,
    _YAHOO,
    _YAHOO_LIMIT,
    _AdjustedSource,
    _FixedDateTime,
    _bar,
    _bars,
    _isolated_finplot_windows,
    _ms,
    _scaled,
    _seed,
    _settled,
    _show,
    _window,
)

_production_refresh = MainWindow._refresh_live_bars

_MONDAY = date(2026, 10, 26)


def _at(hour: int, minute: int, day: date = _MONDAY) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=ET)


def _period(timeframe: Timeframe) -> timedelta:
    if timeframe == Timeframe.DAILY:
        return timedelta(days=1)
    if timeframe == Timeframe.WEEKLY:
        return timedelta(weeks=1)
    assert timeframe.minutes is not None
    return timedelta(minutes=timeframe.minutes)


def _forming_close(end: datetime) -> float:
    return 500.0 + end.astimezone(ET).minute


class _LiveSource(_AdjustedSource):
    """
    Yahoo-like source whose newest bar is still forming at the request end.
    Bars before scale_before are returned on a new basis (prices halved,
    volumes doubled), as after a two-for-one split.
    """

    def __init__(self) -> None:
        super().__init__(_YAHOO_LIMIT)
        self.gate: threading.Event | None = None
        self.gated = threading.Event()
        self.fail = False
        self.scale_before: datetime | None = None
        self.forming_high: float | None = None

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        if self.gate is not None:
            self.gated.set()
            assert self.gate.wait(20)
        if self.fail:
            raise RuntimeError("provider down")
        bars = super().fetch_bars(symbol, timeframe, start, end)
        if bars and bars[-1].timestamp + _period(timeframe) > end:
            last = bars[-1]
            close = _forming_close(end)
            high = max(last.high, close)
            if self.forming_high is not None:
                high = self.forming_high
            bars[-1] = Bar(
                last.timestamp,
                last.open,
                high,
                min(last.low, close),
                close,
                last.volume,
                last.vwap,
            )
        scale_before = self.scale_before
        if scale_before is not None:
            bars = [
                _scaled(bar, 0.5, 2.0) if bar.timestamp < scale_before else bar
                for bar in bars
            ]
        return bars


@pytest.fixture(autouse=True)
def _live_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest turns polling off for every other test.
    monkeypatch.setattr(MainWindow, "_refresh_live_bars", _production_refresh)


@pytest.fixture
def source() -> _LiveSource:
    return _LiveSource()


def _clock(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(_FixedDateTime, "frozen", moment)


def _live_window(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
    timeframe: Timeframe = Timeframe.MIN5,
    opened_at: datetime = _at(10, 30),
) -> tuple[MainWindow, list[Bar]]:
    window = _window(qtbot, tmp_path / "test.db", monkeypatch, source)
    _clock(monkeypatch, opened_at)
    return window, _show(qtbot, window, timeframe)


def _poll(qtbot: Any, window: MainWindow, at: datetime, monkeypatch: pytest.MonkeyPatch) -> list[Bar]:
    _clock(monkeypatch, at)
    window._refresh_live_bars()
    return _settled(qtbot, window)


def _view(window: MainWindow) -> tuple[float, float]:
    rect = window._chart.plot_manager.price_viewbox().viewRect()  # type: ignore[attr-defined]
    return float(rect.left()), float(rect.right())


def _price_range(window: MainWindow) -> tuple[float, float]:
    rect = window._chart.plot_manager.price_viewbox().viewRect()  # type: ignore[attr-defined]
    top, bottom = float(rect.top()), float(rect.bottom())
    return min(top, bottom), max(top, bottom)


def _source_status(window: MainWindow) -> str:
    return window._symbol_bar._source_dot.toolTip()


# ----------------------------------------------------------------------
# AC1, AC3, AC4: new and forming bars arrive in place
# ----------------------------------------------------------------------


def test_a_poll_adds_new_bars_and_updates_the_forming_bar_in_place(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    assert shown[-1].timestamp == _at(10, 25)
    cleared: list[bool] = []
    monkeypatch.setattr(window._chart, "clear_all", lambda: cleared.append(True))

    bars = _poll(qtbot, window, _at(10, 41), monkeypatch)
    assert [bar.timestamp for bar in bars[-4:]] == [
        _at(10, 25), _at(10, 30), _at(10, 35), _at(10, 40),
    ]
    assert bars[-1].close == _forming_close(_at(10, 41))
    assert bars[-2] == _bar(_at(10, 35))

    bars = _poll(qtbot, window, _at(10, 43), monkeypatch)
    assert bars[-1].timestamp == _at(10, 40)
    assert bars[-1].close == _forming_close(_at(10, 43))
    assert cleared == []
    assert bars[: len(shown)] == shown


def test_a_39m_poll_rebuilds_the_forming_bucket_and_adds_the_next(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, shown = _live_window(qtbot, tmp_path, monkeypatch, source, Timeframe.MIN39)
    # Buckets start 09:30, 10:09 and 10:48; a synthesized bar is stamped
    # with its first 5m bar. The 10:09 bucket is forming at 10:30.
    assert shown[-1].timestamp == _at(10, 10)
    forming_close = shown[-1].close

    bars = _poll(qtbot, window, _at(10, 51), monkeypatch)

    assert [bar.timestamp for bar in bars[-2:]] == [_at(10, 10), _at(10, 50)]
    assert bars[-2].close != forming_close
    assert bars[-1].close == _forming_close(_at(10, 51))


def test_a_daily_poll_updates_todays_bar(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, shown = _live_window(qtbot, tmp_path, monkeypatch, source, Timeframe.DAILY)
    assert shown[-1].timestamp == datetime.combine(_MONDAY, time(0), tzinfo=ET)

    bars = _poll(qtbot, window, _at(11, 7), monkeypatch)

    assert len(bars) == len(shown)
    assert bars[-1].close == _forming_close(_at(11, 7))


# ----------------------------------------------------------------------
# AC2: the view follows the newest bar only when it is in view
# ----------------------------------------------------------------------


def test_the_view_follows_new_bars_when_the_newest_bar_is_in_view(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    left, right = _view(window)
    assert left <= len(shown) - 1 <= right

    bars = _poll(qtbot, window, _at(10, 41), monkeypatch)

    appended = len(bars) - len(shown)
    assert appended == 3
    assert _view(window) == pytest.approx((left + appended, right + appended))


def test_the_view_stays_put_when_panned_into_history(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    viewbox: Any = window._chart.plot_manager.price_viewbox()
    viewbox.setXRange(len(shown) - 600, len(shown) - 400, padding=0)
    before = _view(window)

    _poll(qtbot, window, _at(10, 41), monkeypatch)

    assert _view(window) == pytest.approx(before)


def test_a_new_high_is_in_view_after_a_poll(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    assert window._chart.plot_manager.price_viewbox().v_autozoom  # type: ignore[attr-defined]
    low, high = _price_range(window)
    source.forming_high = high + (high - low)

    _poll(qtbot, window, _at(10, 41), monkeypatch)

    assert _price_range(window)[1] >= source.forming_high


# ----------------------------------------------------------------------
# AC5: when polls run
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "moment", "due"),
    [
        ("XBI", _at(9, 29), False),
        ("XBI", _at(9, 30), True),
        ("XBI", _at(16, 4), True),
        ("XBI", _at(16, 5) + timedelta(seconds=30), True),
        ("XBI", _at(16, 6), False),
        ("XBI", _at(11, 0, date(2026, 10, 24)), False),
        ("ES=F", _at(11, 0, date(2026, 10, 24)), True),
    ],
)
def test_polls_are_due_only_in_the_regular_session_for_equities(
    symbol: str,
    moment: datetime,
    due: bool,
) -> None:
    assert _live_refresh_due(symbol, moment) is due


def test_no_poll_request_outside_the_session(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    calls = len(source.calls)

    _clock(monkeypatch, _at(17, 0))
    window._refresh_live_bars()

    assert not window._chart_load_tasks
    assert len(source.calls) == calls


def test_no_poll_while_a_drag_is_active(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    monkeypatch.setattr(
        type(window._extension_runtime), "drag_active", property(lambda _self: True),
    )

    _clock(monkeypatch, _at(10, 41))
    window._refresh_live_bars()

    assert not window._chart_load_tasks


def test_no_poll_while_older_history_loads(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    source.gate = threading.Event()
    window._on_older_history_wanted(100)
    tasks = dict(window._chart_load_tasks)
    assert [task.older for task in tasks.values()] == [True]

    _clock(monkeypatch, _at(10, 41))
    window._refresh_live_bars()

    assert window._chart_load_tasks == tasks
    source.gate.set()
    _settled(qtbot, window)


# ----------------------------------------------------------------------
# AC6, R3: stale results and writer overlap
# ----------------------------------------------------------------------


def test_a_poll_result_after_a_timeframe_change_is_dropped(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    source.gate = threading.Event()
    _clock(monkeypatch, _at(10, 41))
    window._refresh_live_bars()
    assert source.gated.wait(5)

    window._state.timeframe = Timeframe.MIN15
    window._load()
    source.gate.set()
    bars = _settled(qtbot, window)

    assert window._current_series is not None
    assert window._current_series.timeframe == Timeframe.MIN15
    assert bars[-1].timestamp == _at(10, 30)


def test_a_poll_result_is_dropped_when_the_displayed_series_was_replaced(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    source.gate = threading.Event()
    _clock(monkeypatch, _at(10, 41))
    window._refresh_live_bars()
    assert source.gated.wait(5)

    window._reload_extensions()  # e.g. an indicator setting changed
    assert window._current_series is not None
    assert window._current_series.bars == shown
    source.gate.set()
    bars = _settled(qtbot, window)
    assert bars[-1].timestamp == _at(10, 25)

    source.gate = None
    bars = _poll(qtbot, window, _at(10, 41), monkeypatch)
    assert bars[-1].timestamp == _at(10, 40)


def test_a_poll_result_queued_before_a_load_is_not_drawn_on_the_new_chart(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    request = window._current_chart_request
    displayed = window._current_series
    assert request is not None and displayed is not None
    newer = OHLCVSeries(_SYMBOL, Timeframe.MIN5, [*shown, _bar(_at(10, 30))])

    window._state.timeframe = Timeframe.MIN15
    window._load()
    window._on_live_refresh_done(request, displayed, newer)

    assert window._current_series is displayed
    assert _settled(qtbot, window)[-1].timestamp == _at(10, 15)


def test_a_reset_found_during_a_drag_waits_for_the_next_poll(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    request = window._current_chart_request
    displayed = window._current_series
    assert request is not None and displayed is not None
    monkeypatch.setattr(
        type(window._extension_runtime), "drag_active", property(lambda _self: True),
    )

    window._on_live_refresh_done(request, displayed, None)

    assert window._current_chart_request is request
    assert window._pending_chart_load is None


def test_a_new_load_is_not_held_back_by_a_running_poll(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    source.gate = threading.Event()
    _clock(monkeypatch, _at(10, 41))
    window._refresh_live_bars()
    assert source.gated.wait(5)

    window._state.timeframe = Timeframe.MIN15
    window._load()

    qtbot.waitUntil(
        lambda: any(
            task.request.timeframe == Timeframe.MIN15
            for task in window._chart_load_tasks.values()
        ),
        timeout=5000,
    )
    source.gate.set()
    bars = _settled(qtbot, window)
    assert bars[-1].timestamp == _at(10, 30)


def test_no_poll_after_a_failed_load(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    warnings: list[str] = []
    monkeypatch.setattr(
        controller_module, "show_warning", lambda _parent, title, _message: warnings.append(title),
    )
    source.fail = True
    window._state.timeframe = Timeframe.MIN15
    window._load()
    _settled(qtbot, window)
    assert warnings == ["Load Error"]
    source.fail = False
    calls = len(source.calls)

    _clock(monkeypatch, _at(10, 41))
    window._refresh_live_bars()

    assert not window._chart_load_tasks
    assert len(source.calls) == calls


# ----------------------------------------------------------------------
# AC7, AC8: unchanged bars and errors
# ----------------------------------------------------------------------


def test_unchanged_bars_do_not_redraw(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    merges: list[object] = []
    monkeypatch.setattr(window, "_apply_history_merge", lambda *args, **kwargs: merges.append(args))
    calls = len(source.calls)

    _poll(qtbot, window, _at(10, 30), monkeypatch)

    assert len(source.calls) > calls
    assert merges == []


def test_a_poll_error_shows_no_dialog_and_keeps_the_chart(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    source.fail = True

    bars = _poll(qtbot, window, _at(10, 41), monkeypatch)

    assert bars == shown  # _window fails the test on any show_warning
    assert _source_status(window).endswith("Last data request failed")

    source.fail = False
    _poll(qtbot, window, _at(10, 41), monkeypatch)
    assert _source_status(window).endswith("Last data request succeeded")

    # Recovery is shown even when the poll finds nothing new to draw.
    source.fail = True
    _poll(qtbot, window, _at(10, 41), monkeypatch)
    assert _source_status(window).endswith("Last data request failed")
    source.fail = False
    _poll(qtbot, window, _at(10, 41), monkeypatch)
    assert _source_status(window).endswith("Last data request succeeded")


def test_a_failure_streak_is_logged_once_per_chart(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
    caplog: pytest.LogCaptureFixture,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)

    def logged() -> int:
        return sum("Refreshing the newest" in record.getMessage() for record in caplog.records)

    source.fail = True
    with caplog.at_level(logging.WARNING, logger="app.controller"):
        _poll(qtbot, window, _at(10, 41), monkeypatch)
        _poll(qtbot, window, _at(10, 42), monkeypatch)
        assert logged() == 1

        source.fail = False
        _poll(qtbot, window, _at(10, 43), monkeypatch)
        source.fail = True
        _poll(qtbot, window, _at(10, 44), monkeypatch)
        assert logged() == 2

        _poll(qtbot, window, _at(10, 45), monkeypatch)
        source.fail = False
        window._load()
        _settled(qtbot, window)
        source.fail = True
        _poll(qtbot, window, _at(10, 46), monkeypatch)
        assert logged() == 3


# ----------------------------------------------------------------------
# AC10, AC13: older cached history survives polls
# ----------------------------------------------------------------------


_DEEP_START = date(2026, 6, 1)


def _deep_window(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> tuple[MainWindow, list[Bar]]:
    """5m cached since June, well before Yahoo's 55-day window."""
    db_path = tmp_path / "test.db"
    _seed(
        db_path,
        Timeframe.MIN5,
        _bars(Timeframe.MIN5, _DEEP_START, _WEDNESDAY),
        _at(9, 30, _DEEP_START),
        _WEDNESDAY_CLOSE,
    )
    window = _window(qtbot, db_path, monkeypatch, source)
    _clock(monkeypatch, _at(10, 30))
    return window, _show(qtbot, window, Timeframe.MIN5)


def test_a_poll_keeps_the_older_history_on_screen(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, shown = _deep_window(qtbot, tmp_path, monkeypatch, source)
    assert shown[0].timestamp == _at(9, 30, _DEEP_START)

    bars = _poll(qtbot, window, _at(10, 41), monkeypatch)

    assert bars[0].timestamp == shown[0].timestamp
    assert bars[-1].timestamp == _at(10, 40)


def test_a_basis_change_found_by_a_poll_keeps_older_history_like_a_reload(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, _shown = _deep_window(qtbot, tmp_path, monkeypatch, source)
    source.scale_before = _at(9, 30)

    polled = _poll(qtbot, window, _at(10, 41), monkeypatch)
    window._load()
    reloaded = _settled(qtbot, window)

    # The chart shows what a reload shows: not the old-basis history.
    assert polled == reloaded
    with Cache(str(tmp_path / "test.db")) as cache:
        assert cache.get_basis_changes(_YAHOO, _SYMBOL)
        old = cache.get_raw_bars(
            _YAHOO,
            _SYMBOL,
            Timeframe.MIN5,
            _ms(_at(9, 30, _DEEP_START)),
            _ms(_at(16, 0, _DEEP_START)),
        )
    assert old and old[0] == _bar(_at(9, 30, _DEEP_START))


# ----------------------------------------------------------------------
# AC9: production wiring
# ----------------------------------------------------------------------


def test_the_timer_polls_the_displayed_chart_and_stops_on_close(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _LiveSource,
) -> None:
    window, _shown = _live_window(qtbot, tmp_path, monkeypatch, source)
    timer = window._live_refresh_timer
    assert timer.isActive()
    assert timer.interval() == controller_module._LIVE_REFRESH_MS == 15_000

    _clock(monkeypatch, _at(10, 41))
    timer.timeout.emit()
    bars = _settled(qtbot, window)
    assert bars[-1].timestamp == _at(10, 40)

    window.close()
    assert not timer.isActive()
