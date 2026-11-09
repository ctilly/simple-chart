"""
Characterization: with no holiday calendar, chart loads on a weekday market
holiday and on an early-close afternoon complete cleanly. Each load makes one
tail request per fetched timeframe that comes back empty (or typed
unavailable), and leaves the cache as it was. Simulated behavior only; these
tests make no claim about real network latency.
"""

import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any, ClassVar, Self
from zoneinfo import ZoneInfo

import finplot as fplt
import pytest

import app.controller as controller_module
import app.extension_runtime as extension_runtime_module
from app.controller import MainWindow
from data.cache import Cache
from data.models import Bar, Level1Quote, MarketSnapshot, OHLCVSeries, Timeframe
from data.provider.base import DataProvider, HistoryUnavailableError
from data.provider.config import yfinance_connection
from data.provider.credentials import (
    CredentialStoreAccess,
    UnavailableCredentialStore,
)

ET = ZoneInfo("America/New_York")
_SYMBOL = "XBI"
_NAMESPACE = yfinance_connection().cache_namespace
_DAILY_HISTORY_START = controller_module._DAILY_HISTORY_START
_YAHOO_INTRADAY_LIMIT = timedelta(days=55)
# Labor Day and Thanksgiving: the only full closures inside the seeded range
# that matter here. The app itself has no holiday knowledge.
_CLOSED = {date(2026, 9, 7), date(2026, 11, 26)}
_FIRST_INTRADAY_SESSION = date(2026, 9, 1)

Call = tuple[Timeframe, datetime, datetime]
CacheSnapshot = tuple[list[tuple[Any, ...]], dict[tuple[str, str, str], tuple[int, int]]]


def _et(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


@dataclass(frozen=True)
class _Scenario:
    now: datetime
    last_intraday_bar: datetime
    intraday_coverage_end: datetime
    last_daily_bar: datetime
    daily_coverage_end: datetime
    # The one tail request each fetched timeframe makes per load.
    tails: dict[Timeframe, tuple[datetime, datetime]]


# Criterion 1: Thanksgiving (Thursday) 17:00 ET, cache current through the
# 2026-11-25 regular-session close.
_THANKSGIVING = _Scenario(
    now=_et(2026, 11, 26, 17),
    last_intraday_bar=_et(2026, 11, 25, 15, 55),
    intraday_coverage_end=_et(2026, 11, 25, 16),
    last_daily_bar=_et(2026, 11, 25),
    daily_coverage_end=_et(2026, 11, 26),
    tails={
        Timeframe.MIN5: (_et(2026, 11, 25, 15, 55), _et(2026, 11, 26, 17)),
        Timeframe.DAILY: (_et(2026, 11, 25), _et(2026, 11, 26, 17)),
    },
)

# Criterion 2: the day after Thanksgiving, 14:00 ET, an hour after the 13:00
# early close. 5m bars run through 12:55; daily stays through 11-25 because
# 11-26 was a holiday and the 11-27 bar has not been fetched.
_EARLY_CLOSE = _Scenario(
    now=_et(2026, 11, 27, 14),
    last_intraday_bar=_et(2026, 11, 27, 12, 55),
    intraday_coverage_end=_et(2026, 11, 27, 13),
    last_daily_bar=_et(2026, 11, 25),
    daily_coverage_end=_et(2026, 11, 26),
    tails={
        Timeframe.MIN5: (_et(2026, 11, 27, 13), _et(2026, 11, 27, 14)),
        Timeframe.DAILY: (_et(2026, 11, 26), _et(2026, 11, 27, 14)),
    },
)

_TIMEFRAMES = pytest.mark.parametrize(
    "timeframe", [Timeframe.MIN5, Timeframe.DAILY], ids=["5m", "daily"],
)
_VARIANTS = pytest.mark.parametrize(
    ("tail", "loads"),
    [
        pytest.param("empty", 1, id="empty-tail"),
        pytest.param("unavailable", 1, id="history-unavailable-tail"),
        pytest.param("empty", 2, id="two-loads"),
    ],
)


class _FixedDateTime(datetime):
    frozen: ClassVar[datetime] = datetime.fromtimestamp(0, UTC)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(cls.frozen.timestamp(), tz=tz)


class _HolidayProvider(DataProvider):
    """
    Yahoo-shaped fake (55-day intraday limit, one request per range) that has
    nothing newer than the cache: every bar request returns an empty list or
    raises HistoryUnavailableError.
    """

    def __init__(self, unavailable: bool) -> None:
        self.unavailable = unavailable
        self.calls: list[Call] = []
        self._lock = threading.Lock()

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        with self._lock:
            self.calls.append((timeframe, start, end))
        if self.unavailable:
            raise HistoryUnavailableError(symbol, timeframe)
        return []

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        return {}

    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        return None

    def earliest_history_start(
        self,
        timeframe: Timeframe,
        end: datetime,
    ) -> datetime | None:
        return end - _YAHOO_INTRADAY_LIMIT if timeframe.is_intraday else None

    def native_timeframes(self) -> list[Timeframe]:
        return [
            Timeframe.MIN1,
            Timeframe.MIN5,
            Timeframe.MIN15,
            Timeframe.MIN30,
            Timeframe.DAILY,
            Timeframe.WEEKLY,
        ]


@pytest.fixture(autouse=True)
def _isolated_finplot_windows() -> Iterator[None]:
    # fplt.refresh() walks finplot's process-global window list; drop windows
    # left by earlier tests, whose Qt objects are already deleted.
    fplt.windows.clear()
    yield
    fplt.windows.clear()


def _sessions(first: date, last: date) -> Iterator[date]:
    day = first
    while day <= last:
        if day.weekday() < 5 and day not in _CLOSED:
            yield day
        day += timedelta(days=1)


def _bar(timestamp: datetime, index: int) -> Bar:
    price = 100.0 + index % 97
    return Bar(
        timestamp.astimezone(UTC),
        price,
        price + 1.0,
        price - 1.0,
        price + 0.5,
        1_000 + index % 13,
    )


def _intraday_bars(last_bar: datetime) -> list[Bar]:
    bars: list[Bar] = []
    for day in _sessions(_FIRST_INTRADAY_SESSION, last_bar.date()):
        slot = datetime.combine(day, time(9, 30), tzinfo=ET)
        while slot.time() < time(16) and slot <= last_bar:
            bars.append(_bar(slot, len(bars)))
            slot += timedelta(minutes=5)
    return bars


def _daily_bars(last_bar: datetime) -> list[Bar]:
    first = _DAILY_HISTORY_START.date()
    return [
        _bar(datetime.combine(day, time(0), tzinfo=ET), index)
        for index, day in enumerate(_sessions(first, last_bar.date()))
    ]


def _ms(timestamp: datetime) -> int:
    return int(timestamp.timestamp() * 1000)


def _seed(db_path: Path, scenario: _Scenario) -> None:
    with Cache(str(db_path)) as cache:
        intraday = _intraday_bars(scenario.last_intraday_bar)
        cache.put_bars(_NAMESPACE, _SYMBOL, Timeframe.MIN5, intraday)
        cache.replace_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN5,
            _ms(intraday[0].timestamp),
            _ms(scenario.intraday_coverage_end),
        )
        daily = _daily_bars(scenario.last_daily_bar)
        cache.put_bars(_NAMESPACE, _SYMBOL, Timeframe.DAILY, daily)
        cache.replace_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.DAILY,
            _ms(_DAILY_HISTORY_START),
            _ms(scenario.daily_coverage_end),
        )


def _snapshot(db_path: Path) -> CacheSnapshot:
    connection = sqlite3.connect(db_path)
    try:
        bars = connection.execute(
            "SELECT * FROM bars ORDER BY cache_namespace, symbol, timeframe, timestamp"
        ).fetchall()
        coverage = {
            (row[0], row[1], row[2]): (row[3], row[4])
            for row in connection.execute(
                "SELECT cache_namespace, symbol, timeframe, start_timestamp, "
                "end_timestamp FROM bar_fetch_coverage"
            )
        }
    finally:
        connection.close()
    return bars, coverage


def _window(
    qtbot: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: _HolidayProvider,
    now: datetime,
) -> tuple[MainWindow, list[str]]:
    # Never touch the host OS keyring from these tests.
    monkeypatch.setattr(
        controller_module,
        "initialize_keyring_credential_store",
        lambda: CredentialStoreAccess(
            UnavailableCredentialStore("test"), False, "test",
        ),
    )
    monkeypatch.setattr(
        controller_module, "create_provider", lambda _connection, _store: provider,
    )
    monkeypatch.setattr(_FixedDateTime, "frozen", now)
    monkeypatch.setattr(controller_module, "datetime", _FixedDateTime)
    monkeypatch.setattr(extension_runtime_module, "datetime", _FixedDateTime)
    warnings: list[str] = []
    monkeypatch.setattr(
        controller_module,
        "show_warning",
        lambda _parent, title, message: warnings.append(f"{title}: {message}"),
    )
    production_load = MainWindow._load
    monkeypatch.setattr(MainWindow, "_load", lambda window: None)
    monkeypatch.setattr(MainWindow, "_refresh_watchlist_snapshots", lambda window: None)
    monkeypatch.setattr(MainWindow, "_refresh_level1", lambda window: None)
    window = MainWindow(str(db_path))
    qtbot.addWidget(window)
    monkeypatch.setattr(MainWindow, "_load", production_load)
    return window, warnings


def _load_to_terminal(
    qtbot: Any,
    window: MainWindow,
    timeframe: Timeframe,
) -> tuple[list[OHLCVSeries], list[str]]:
    """Run one production load (debounce, worker thread, delivery, render)."""
    delivered: list[OHLCVSeries] = []
    errors: list[str] = []
    production_done = window._on_chart_fetch_done
    production_error = window._on_chart_fetch_error

    def record_done(request: Any, series: OHLCVSeries) -> None:
        delivered.append(series)
        production_done(request, series)

    def record_error(request: Any, message: str) -> None:
        errors.append(message)
        production_error(request, message)

    # Instance attributes shadow the methods the worker signals connect to.
    window._on_chart_fetch_done = record_done  # type: ignore[method-assign]
    window._on_chart_fetch_error = record_error  # type: ignore[method-assign]
    window._state.symbol = _SYMBOL
    window._state.timeframe = timeframe
    window._load()
    request = window._current_chart_request
    assert request is not None
    qtbot.waitUntil(
        lambda: window._terminal_chart_generation == request.generation
        and window._pending_chart_load is None
        and not window._chart_load_tasks,
        timeout=10_000,
    )
    return delivered, errors


def _bar_tuples(bars: list[Bar]) -> list[tuple[Any, ...]]:
    return [
        (bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume)
        for bar in bars
    ]


def _expected_series(scenario: _Scenario, timeframe: Timeframe) -> list[Bar]:
    if timeframe == Timeframe.DAILY:
        return _daily_bars(scenario.last_daily_bar)
    window_start = scenario.now - _YAHOO_INTRADAY_LIMIT
    return [
        bar
        for bar in _intraday_bars(scenario.last_intraday_bar)
        if bar.timestamp >= window_start
    ]


def _expected_calls(scenario: _Scenario, timeframe: Timeframe) -> list[Call]:
    # An intraday load also refreshes the daily references.
    fetched: list[Timeframe] = (
        [timeframe] if timeframe == Timeframe.DAILY else [timeframe, Timeframe.DAILY]
    )
    return [(fetched_tf, *scenario.tails[fetched_tf]) for fetched_tf in fetched]


def _call_key(call: Call) -> tuple[str, datetime, datetime]:
    return (call[0].value, call[1], call[2])


def _assert_loads_complete_cleanly(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: _Scenario,
    timeframe: Timeframe,
    tail: str,
    loads: int,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, scenario)
    seeded_bars, seeded_coverage = _snapshot(db_path)
    provider = _HolidayProvider(unavailable=tail == "unavailable")
    window, warnings = _window(qtbot, db_path, monkeypatch, provider, scenario.now)
    expected_bars = _bar_tuples(_expected_series(scenario, timeframe))
    expected_calls = _expected_calls(scenario, timeframe)
    assert expected_bars

    for load in range(1, loads + 1):
        delivered, errors = _load_to_terminal(qtbot, window, timeframe)

        # Delivered and rendered, with no error anywhere.
        assert errors == [] and warnings == []
        assert len(delivered) == 1
        assert _bar_tuples(delivered[0].bars) == expected_bars
        assert window._current_series is delivered[0]
        assert window._displayed_history_complete
        plot_manager = window._chart.plot_manager
        assert plot_manager._candle_plot is not None
        assert plot_manager._bar_index is not None
        assert len(plot_manager._bar_index) == len(expected_bars)
        assert window._symbol_bar._source_dot.toolTip() == (
            "Yahoo Finance: Last data request succeeded"
        )
        status_bar = window.statusBar()
        assert status_bar is not None
        assert status_bar.currentMessage() == ""

        # Exactly one tail request per fetched timeframe per load.
        assert sorted(provider.calls, key=_call_key) == sorted(
            expected_calls * load, key=_call_key,
        )

        # Bars unchanged; coverage unchanged apart from an end that may only
        # move forward.
        bars, coverage = _snapshot(db_path)
        assert bars == seeded_bars
        assert coverage.keys() == seeded_coverage.keys()
        for key, (start, end) in coverage.items():
            assert start == seeded_coverage[key][0]
            assert end >= seeded_coverage[key][1]


@_TIMEFRAMES
@_VARIANTS
def test_weekday_holiday_load_completes_cleanly(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeframe: Timeframe,
    tail: str,
    loads: int,
) -> None:
    _assert_loads_complete_cleanly(
        qtbot, tmp_path, monkeypatch, _THANKSGIVING, timeframe, tail, loads,
    )


@_TIMEFRAMES
@_VARIANTS
def test_early_close_afternoon_load_completes_cleanly(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeframe: Timeframe,
    tail: str,
    loads: int,
) -> None:
    _assert_loads_complete_cleanly(
        qtbot, tmp_path, monkeypatch, _EARLY_CLOSE, timeframe, tail, loads,
    )
