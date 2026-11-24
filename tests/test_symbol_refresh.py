"""
Refresh symbol from source, driven through MainWindow: the rewind boundaries,
the load barrier and drain, and the refill through the normal load path
(docs/agentic-projects/symbol-refresh-and-holiday-check.md, criteria 5, 5a,
5b, 6, 6d, 6e, 6f, 7 and 10).

The fake source serves 71 days of intraday history. With the clock frozen on
Saturday 2026-10-24 10:30 ET, that limit falls inside the Friday 2026-08-14
session at 10:30 ET: inside the 39m period 10:09-10:48 and the 65m period
9:30-10:35. _history_start, and so the intraday rewind boundary B, is the next
session open, Monday 2026-08-17 09:30 ET. The cache was filled from
2026-08-03, before that limit; the bars before B are kept by the refresh.
"""

import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any, ClassVar, Self, cast
from zoneinfo import ZoneInfo

import finplot as fplt
import pyqtgraph as pg
import pytest
import yfinance as yf
from PyQt6.QtCore import QDate, QPoint, Qt, QThread
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
)
from pytestqt.qtbot import QtBot

import app.controller as controller_module
import app.data_quality as data_quality_module
import app.extension_runtime as extension_runtime_module
import app.market_status_bar as market_status_bar_module
import app.symbol_details as symbol_details_module
from app.application_settings import ApplicationSettingsDialog
from app.controller import MainWindow
from app.watchlist import SymbolListWidget
from chart.window import ChartWidget
from app.data_quality import DataQualityTab
from data.provider.registry import ProviderRegistry
from data.aggregator import Aggregator
from data.cache import Cache
from data.models import (
    Bar, BarCorrection, Holding, Level1Quote, MarketSnapshot, NewsArticle,
    SymbolMatch, SymbolProfile, Timeframe,
)
from data.provider import ProviderAvailability
from data.provider.base import DataProvider
from data.provider.config import ProviderConnection
from data.provider.registry import FALLBACK_PROVIDER as YFINANCE_CONNECTION_ID
from providers.alpaca import ALPACA_PAPER_CONNECTION_ID
from data.session import is_regular_session_timestamp
from data.provider.credentials import (
    CredentialStoreAccess,
    UnavailableCredentialStore,
)
from provider_support import startup_registry

ET = ZoneInfo("America/New_York")
_SYMBOL = "XBI"
_OTHER_SYMBOL = "SPY"
_YAHOO = "yfinance"
_ALPACA = "alpaca:iex"
_NOW = datetime(2026, 10, 24, 10, 30, tzinfo=ET)
_INTRADAY_LIMIT = timedelta(days=71)
_CUTOFF = datetime(2026, 8, 14, 10, 30, tzinfo=ET)
_HISTORY_START = datetime(2026, 8, 17, 9, 30, tzinfo=ET)
_BOUNDARY = _HISTORY_START
_DAILY_HISTORY_START = datetime(2016, 1, 1, tzinfo=UTC)
_DAILY_BOUNDARY = _DAILY_HISTORY_START
_WEEKLY_BOUNDARY = datetime(2016, 1, 4, tzinfo=ET)
_SEED_START = date(2026, 8, 3)
_LAST_SESSION = date(2026, 10, 23)
_FIRST_DAILY = date(2015, 12, 1)
_PAGE_SPAN = timedelta(days=10)
# Seeded data the source no longer serves: a bar with wrong prices and
# volume, and an after-hours bar, both after B.
_BAD_BAR = datetime(2026, 8, 17, 10, 0, tzinfo=ET)
_GONE_BAR = datetime(2026, 8, 17, 17, 0, tzinfo=ET)
_BAD_VOLUME = 999_999
_BAD_PRICE_SHIFT = 50.0

Call = tuple[Timeframe, datetime, datetime]
Row = tuple[datetime, float, float, float, float, int]
FetchHook = Callable[[Timeframe, datetime, datetime], None]

_BASES = pytest.mark.parametrize(
    "base", [Timeframe.MIN5, Timeframe.MIN1], ids=["5m-base", "1m-base"],
)
_TARGETS = pytest.mark.parametrize(
    "target", [Timeframe.MIN39, Timeframe.MIN65], ids=["39m", "65m"],
)
_SHAPES = pytest.mark.parametrize("paged", [True, False], ids=["paged", "single"])


class _FixedDateTime(datetime):
    frozen: ClassVar[datetime] = datetime.fromtimestamp(0, UTC)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(cls.frozen.timestamp(), tz=tz)


class _SourceProvider(DataProvider):
    """
    Serves every regular-session bar of a weekday calendar (no holidays) for
    any requested [start, end). Intraday volume equals the bar's minutes, so a
    complete 39m or 65m period has volume 39-40 or 65 whatever its base.
    """

    def __init__(
        self,
        base: Timeframe,
        paged: bool,
        intraday_limit: timedelta | None = _INTRADAY_LIMIT,
    ) -> None:
        self._native = [
            base,
            Timeframe.MIN15,
            Timeframe.MIN30,
            Timeframe.DAILY,
            Timeframe.WEEKLY,
        ]
        self._page_span = _PAGE_SPAN if paged else None
        self._intraday_limit = intraday_limit
        self.calls: list[Call] = []
        self.before_fetch: FetchHook | None = None
        self.extra_bars: dict[Timeframe, list[Bar]] = {}
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
        if self.before_fetch is not None:
            self.before_fetch(timeframe, start, end)
        extra = [
            bar
            for bar in self.extra_bars.get(timeframe, [])
            if start <= bar.timestamp < end
        ]
        return sorted(
            _source_bars(timeframe, start, end) + extra,
            key=lambda bar: bar.timestamp,
        )

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        return {}

    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        return None

    def earliest_history_start(
        self,
        timeframe: Timeframe,
        end: datetime,
    ) -> datetime | None:
        if not timeframe.is_intraday or self._intraday_limit is None:
            return None
        return end - self._intraday_limit

    def history_page_span(self, timeframe: Timeframe) -> timedelta | None:
        return self._page_span if timeframe.is_intraday else None

    def native_timeframes(self) -> list[Timeframe]:
        return list(self._native)


@pytest.fixture(autouse=True)
def _isolated_finplot_windows() -> Iterator[None]:
    fplt.windows.clear()
    yield
    fplt.windows.clear()


# ----------------------------------------------------------------------
# Source data and the independent expectation for synthesized bars
# ----------------------------------------------------------------------


def _weekdays(first: date, last: date) -> Iterator[date]:
    day = first
    while day <= last:
        if day.weekday() < 5:
            yield day
        day += timedelta(days=1)


def _price(timestamp: datetime) -> float:
    return 100.0 + (int(timestamp.timestamp()) // 60) % 997 / 10.0


def _source_bar(timestamp: datetime, volume: int) -> Bar:
    price = _price(timestamp)
    return Bar(
        timestamp.astimezone(UTC),
        price,
        price + 1.0,
        price - 1.0,
        price + 0.5,
        volume,
    )


def _source_bars(timeframe: Timeframe, start: datetime, end: datetime) -> list[Bar]:
    first = start.astimezone(ET).date() - timedelta(days=7)
    last = end.astimezone(ET).date()
    if timeframe == Timeframe.DAILY:
        stamps = [
            (datetime.combine(day, time(0), tzinfo=ET), 390)
            for day in _weekdays(first, last)
        ]
    elif timeframe == Timeframe.WEEKLY:
        stamps = [
            (datetime.combine(day, time(0), tzinfo=ET), 1950)
            for day in _weekdays(first, last)
            if day.weekday() == 0
        ]
    else:
        minutes = timeframe.minutes
        assert minutes is not None
        stamps = [
            (
                datetime.combine(day, time(9, 30), tzinfo=ET)
                + timedelta(minutes=offset),
                minutes,
            )
            for day in _weekdays(first, last)
            for offset in range(0, 390, minutes)
        ]
    return [
        _source_bar(stamp, volume)
        for stamp, volume in stamps
        if start <= stamp < end
    ]


def _expected_synthesized(
    base: Timeframe,
    target: Timeframe,
    first: date = _SEED_START,
    last: date = _LAST_SESSION,
) -> list[Row]:
    """One bar per period, timestamped by its first base bar."""
    period = target.minutes
    assert period is not None
    rows: list[Row] = []
    for day in _weekdays(first, last):
        session_open = datetime.combine(day, time(9, 30), tzinfo=ET)
        groups: dict[int, list[Bar]] = {}
        for bar in _source_bars(base, session_open, session_open + timedelta(hours=7)):
            offset = int((bar.timestamp - session_open).total_seconds() // 60)
            groups.setdefault(offset // period, []).append(bar)
        for group in groups.values():
            rows.append(
                (
                    group[0].timestamp,
                    group[0].open,
                    max(bar.high for bar in group),
                    min(bar.low for bar in group),
                    group[-1].close,
                    sum(bar.volume for bar in group),
                )
            )
    return rows


def _expected_native(timeframe: Timeframe, first: date, last: date) -> list[Row]:
    return _rows(
        _source_bars(
            timeframe,
            datetime.combine(first, time(0), tzinfo=ET),
            datetime.combine(last + timedelta(days=1), time(0), tzinfo=ET),
        )
    )


def _rows(bars: list[Bar]) -> list[Row]:
    return [
        (bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume)
        for bar in bars
    ]


def _ms(timestamp: datetime) -> int:
    return int(timestamp.timestamp() * 1000)


def _from_ms(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC)


# ----------------------------------------------------------------------
# Cache seeding and inspection
# ----------------------------------------------------------------------


def _seed(
    db_path: Path,
    base: Timeframe,
    namespace: str = _YAHOO,
    symbol: str = _SYMBOL,
    last_session: date = _LAST_SESSION,
    natives: tuple[Timeframe, ...] = (),
) -> None:
    """
    The cache as a normal load from before the source limit left it, except
    that one base bar after B has wrong prices and volume and one after-hours
    base bar is no longer served. Synthesized targets aggregate the seeded
    base bars.
    """
    coverage_end = (
        _NOW
        if last_session == _LAST_SESSION
        else datetime.combine(last_session, time(16), tzinfo=ET)
    )
    seed_start = datetime.combine(_SEED_START, time(0), tzinfo=ET)
    seed_end = datetime.combine(last_session + timedelta(days=1), time(0), tzinfo=ET)
    with Cache(str(db_path), registry=startup_registry()) as cache:
        base_bars = [
            Bar(
                bar.timestamp,
                bar.open + _BAD_PRICE_SHIFT,
                bar.high + _BAD_PRICE_SHIFT,
                bar.low + _BAD_PRICE_SHIFT,
                bar.close + _BAD_PRICE_SHIFT,
                _BAD_VOLUME,
            )
            if bar.timestamp == _BAD_BAR
            else bar
            for bar in _source_bars(base, seed_start, seed_end)
        ]
        base_bars.append(_source_bar(_GONE_BAR, base.minutes or 0))
        cache.put_bars(namespace, symbol, base, base_bars)
        cache.replace_bar_fetch_coverage(
            namespace, symbol, base, _ms(seed_start), _ms(coverage_end),
        )
        aggregator = Aggregator(_SourceProvider(base, paged=False))
        session_bars = [
            bar
            for bar in base_bars
            if is_regular_session_timestamp(bar.timestamp)
        ]
        for target in (Timeframe.MIN39, Timeframe.MIN65):
            cache.put_bars(
                namespace,
                symbol,
                target,
                aggregator.synthesize_cached_bars(session_bars, target),
            )
            cache.replace_bar_fetch_coverage(
                namespace, symbol, target, _ms(seed_start), _ms(coverage_end),
            )
        for native in natives:
            cache.put_bars(
                namespace, symbol, native, _source_bars(native, seed_start, seed_end),
            )
            cache.replace_bar_fetch_coverage(
                namespace, symbol, native, _ms(seed_start), _ms(coverage_end),
            )
        daily_start = datetime.combine(_FIRST_DAILY, time(0), tzinfo=ET)
        cache.put_bars(
            namespace,
            symbol,
            Timeframe.DAILY,
            _source_bars(Timeframe.DAILY, daily_start, seed_end),
        )
        cache.replace_bar_fetch_coverage(
            namespace,
            symbol,
            Timeframe.DAILY,
            _ms(_DAILY_HISTORY_START),
            _ms(seed_end),
        )
        cache.put_bars(
            namespace,
            symbol,
            Timeframe.WEEKLY,
            _source_bars(Timeframe.WEEKLY, daily_start, seed_end),
        )
        cache.replace_bar_fetch_coverage(
            namespace,
            symbol,
            Timeframe.WEEKLY,
            _ms(_DAILY_HISTORY_START),
            _ms(datetime(2026, 10, 19, tzinfo=ET)),
        )


def _cached_rows(
    db_path: Path,
    timeframe: Timeframe,
    namespace: str = _YAHOO,
    symbol: str = _SYMBOL,
) -> list[Row]:
    with Cache(str(db_path), registry=startup_registry()) as cache:
        return _rows(cache.get_raw_bars(namespace, symbol, timeframe, 0, 2**62))


def _coverage(
    db_path: Path,
    timeframe: Timeframe,
    namespace: str = _YAHOO,
    symbol: str = _SYMBOL,
) -> tuple[int, int] | None:
    with Cache(str(db_path), registry=startup_registry()) as cache:
        return cache.get_bar_fetch_coverage(namespace, symbol, timeframe)


def _table_rows(db_path: Path, sql: str, parameters: tuple[object, ...]) -> list[Any]:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def _covers(calls: list[Call], timeframe: Timeframe, start: datetime, end: datetime) -> bool:
    """Whether the union of the timeframe's requests covers [start, end]."""
    reached = start
    for _timeframe, call_start, call_end in sorted(
        call for call in calls if call[0] == timeframe
    ):
        if call_start > reached:
            break
        reached = max(reached, call_end)
    return reached >= end


def _period_key(timestamp: datetime, period: int) -> tuple[date, int]:
    local = timestamp.astimezone(ET)
    session_open = datetime.combine(local.date(), time(9, 30), tzinfo=ET)
    return local.date(), int((local - session_open).total_seconds() // 60) // period


def _assert_one_bar_per_period(rows: list[Row], target: Timeframe) -> None:
    period = target.minutes
    assert period is not None
    keys = [_period_key(row[0], period) for row in rows]
    assert len(keys) == len(set(keys))


def _assert_whole_cache_synthesized(
    db_path: Path,
    base: Timeframe,
    target: Timeframe,
) -> None:
    rows = _cached_rows(db_path, target)
    assert rows == _expected_synthesized(base, target)
    _assert_one_bar_per_period(rows, target)
    by_time = {row[0]: row for row in rows}
    # The kept period containing the source limit: one complete bar, stamped
    # by its first base bar, and no partial bar at the limit itself.
    if target == Timeframe.MIN39:
        first_minute = 10 if base == Timeframe.MIN5 else 9
        cutoff_period = datetime(2026, 8, 14, 10, first_minute, tzinfo=ET)
        complete_volume = 40 if base == Timeframe.MIN5 else 39
    else:
        cutoff_period = datetime(2026, 8, 14, 9, 30, tzinfo=ET)
        complete_volume = 65
    assert by_time[cutoff_period.astimezone(UTC)][5] == complete_volume
    assert _CUTOFF.astimezone(UTC) not in by_time
    # The first period at B is rebuilt from the source and complete; the
    # 09:30 period has the same volume as the one containing the limit.
    assert by_time[_BOUNDARY.astimezone(UTC)][5] == (
        65 if target == Timeframe.MIN65 else complete_volume
    )
    assert all(row[5] < _BAD_VOLUME for row in rows)


def _assert_truthful_coverage(
    db_path: Path,
    base: Timeframe,
    target: Timeframe,
) -> None:
    """Every covered interval holds exactly what the source serves for it."""
    base_coverage = _coverage(db_path, base)
    if base_coverage is not None:
        start, end = (_from_ms(value) for value in base_coverage)
        assert [
            row for row in _cached_rows(db_path, base) if start <= row[0] < end
        ] == _rows(_source_bars(base, start, end))
    target_coverage = _coverage(db_path, target)
    if target_coverage is not None:
        start, end = (_from_ms(value) for value in target_coverage)
        assert base_coverage is not None
        assert base_coverage[0] <= target_coverage[0]
        assert target_coverage[1] <= base_coverage[1]
        assert [
            row for row in _cached_rows(db_path, target) if start <= row[0] < end
        ] == [
            row
            for row in _expected_synthesized(base, target)
            if start <= row[0] < end
        ]


# ----------------------------------------------------------------------
# MainWindow harness
# ----------------------------------------------------------------------


def _window(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: _SourceProvider,
    active_connection_id: str = YFINANCE_CONNECTION_ID,
    *,
    reference_provider: DataProvider | None = None,
) -> tuple[MainWindow, list[str]]:
    with Cache(str(db_path), registry=provider_registry) as cache:
        cache.set_active_provider_connection_id(active_connection_id)
    monkeypatch.setattr(
        controller_module,
        "initialize_keyring_credential_store",
        lambda *, registry: CredentialStoreAccess(
            UnavailableCredentialStore("test"), False, "test",
        ),
    )
    monkeypatch.setattr(
        controller_module,
        "_provider_availability_by_name",
        lambda connections, _access, _registry: {
            connection.provider_name: ProviderAvailability(True, None)
            for connection in connections
        },
    )

    def create_provider(
        connection: ProviderConnection,
        _store: object,
    ) -> DataProvider:
        if reference_provider is not None and connection.connection_id == YFINANCE_CONNECTION_ID:
            return reference_provider
        return provider

    monkeypatch.setattr(controller_module.ProviderRegistry, "create_provider", staticmethod(create_provider))
    monkeypatch.setattr(_FixedDateTime, "frozen", _NOW)
    monkeypatch.setattr(controller_module, "datetime", _FixedDateTime)
    monkeypatch.setattr(extension_runtime_module, "datetime", _FixedDateTime)
    # No indicators, so the recent window is the initial view alone.
    monkeypatch.setattr(controller_module, "INITIAL_EXTENSIONS", [])
    warnings: list[str] = []
    monkeypatch.setattr(
        controller_module,
        "show_warning",
        lambda _parent, title, message: warnings.append(f"{title}: {message}"),
    )
    production_load = MainWindow._load
    monkeypatch.setattr(MainWindow, "_load", lambda window, **_kwargs: None)
    monkeypatch.setattr(MainWindow, "_refresh_watchlist_snapshots", lambda window: None)
    monkeypatch.setattr(MainWindow, "_refresh_level1", lambda window: None)
    # Older history loads on its own when a short test chart's view is near
    # its oldest bar; tests/test_older_history.py covers that path.
    monkeypatch.setattr(ChartWidget, "older_history_bars_wanted", lambda _chart: 0)
    window = MainWindow(str(db_path), provider_registry=provider_registry)
    qtbot.addWidget(window)
    monkeypatch.setattr(MainWindow, "_load", production_load)
    return window, warnings


def _wait_terminal(qtbot: Any, window: MainWindow) -> None:
    request = window._current_chart_request
    assert request is not None
    qtbot.waitUntil(
        lambda: window._terminal_chart_generation == request.generation
        and window._pending_chart_load is None
        and not window._chart_load_tasks,
        timeout=30_000,
    )


def _show(qtbot: Any, window: MainWindow, timeframe: Timeframe) -> None:
    window._state.symbol = _SYMBOL
    window._state.timeframe = timeframe
    window._load()
    _wait_terminal(qtbot, window)


def _refresh(
    window: MainWindow,
    namespace: str = _YAHOO,
    symbol: str = _SYMBOL,
) -> None:
    """Confirm a refresh the way the Data Quality tab does."""
    plan = window.plan_symbol_refresh(namespace, symbol)
    assert plan is not None
    plan.refresh()


def _refresh_and_reload(qtbot: Any, window: MainWindow) -> None:
    before = window._current_chart_request
    _refresh(window)
    assert window._current_chart_request is not before
    _wait_terminal(qtbot, window)


_RewindCall = tuple[dict[Timeframe, int], tuple[Timeframe, ...]]


def _record_rewinds(
    window: MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> list[_RewindCall]:
    """Record the boundaries and synthesized timeframes of each rewind."""
    calls: list[_RewindCall] = []
    production_rewind = window._cache.rewind_symbol_history

    def rewind(
        cache_namespace: str,
        symbol: str,
        boundaries: Any,
        synthesized_timeframes: Any,
    ) -> None:
        calls.append((dict(boundaries), tuple(synthesized_timeframes)))
        production_rewind(cache_namespace, symbol, boundaries, synthesized_timeframes)

    monkeypatch.setattr(window._cache, "rewind_symbol_history", rewind)
    return calls


# ----------------------------------------------------------------------
# 6f: B == _history_start, computed at rewind time; all coverage removed
# ----------------------------------------------------------------------


@_BASES
@_SHAPES
def test_rewind_boundaries_are_the_history_starts_and_remove_all_coverage(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base: Timeframe,
    paged: bool,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, base, natives=(Timeframe.MIN15, Timeframe.MIN30))
    provider = _SourceProvider(base, paged)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN39)
    rewinds = _record_rewinds(window, monkeypatch)
    coverage_at_refill: list[dict[Timeframe, tuple[int, int] | None]] = []
    production_start = window._start_chart_load

    def start(request: Any) -> None:
        coverage_at_refill.append(
            {timeframe: _coverage(db_path, timeframe) for timeframe in Timeframe}
        )
        production_start(request)

    monkeypatch.setattr(window, "_start_chart_load", start)

    _refresh_and_reload(qtbot, window)

    assert warnings == []
    aggregator = Aggregator(provider)
    intraday = _ms(_HISTORY_START)
    expected = {
        base: intraday,
        Timeframe.MIN15: intraday,
        Timeframe.MIN30: intraday,
        Timeframe.MIN39: intraday,
        Timeframe.MIN65: intraday,
        Timeframe.DAILY: _ms(_DAILY_HISTORY_START),
        Timeframe.WEEKLY: _ms(_WEEKLY_BOUNDARY),
    }
    assert rewinds == [(expected, ())]
    for timeframe, boundary in expected.items():
        if timeframe == Timeframe.WEEKLY:
            continue
        history_start = controller_module._history_start(
            timeframe,
            aggregator,
            controller_module._history_end(timeframe, aggregator, _NOW),
        )
        assert boundary == _ms(history_start)
    # Every rewound timeframe's coverage, native and synthesized, is gone
    # before the refill starts.
    assert coverage_at_refill == [{timeframe: None for timeframe in Timeframe}]
    request = window._current_chart_request
    assert request is not None and request.history_start == _HISTORY_START


def test_plan_describes_the_ranges_at_planning_time(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SourceProvider(Timeframe.MIN5, paged=True)
    window, _warnings = _window(provider_registry, qtbot, tmp_path / "test.db", monkeypatch, provider)

    plan = window.plan_symbol_refresh(_YAHOO, _SYMBOL)

    assert plan is not None
    # Saturday 10:30 back to Monday 2026-08-17 09:30 is 68 days and an hour.
    assert plan.intraday_days == 68
    assert plan.daily_since == date(2016, 1, 1)


# ----------------------------------------------------------------------
# 5a: one complete bar per period after the refill, a second ordinary load,
# and a refill that fails partway then is retried
# ----------------------------------------------------------------------


@_SHAPES
@_BASES
@_TARGETS
def test_refill_keeps_one_complete_bar_per_period(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paged: bool,
    base: Timeframe,
    target: Timeframe,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, base)
    provider = _SourceProvider(base, paged)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, target)
    assert provider.calls == []
    assert any(row[5] >= _BAD_VOLUME for row in _cached_rows(db_path, target))

    _refresh_and_reload(qtbot, window)

    assert warnings == []
    assert window._displayed_history_complete
    _assert_whole_cache_synthesized(db_path, base, target)
    # Coverage describes only what the refill fetched and materialized.
    assert _coverage(db_path, base) == (_ms(_BOUNDARY), _ms(_NOW))
    assert _coverage(db_path, target) == (_ms(_BOUNDARY), _ms(_NOW))
    _assert_truthful_coverage(db_path, base, target)
    assert _covers(provider.calls, base, _BOUNDARY, _NOW)
    displayed = window._current_series
    assert displayed is not None
    assert _rows(displayed.bars) == [
        row
        for row in _expected_synthesized(base, target)
        if row[0] >= _HISTORY_START
    ]

    _show(qtbot, window, target)

    assert warnings == []
    _assert_whole_cache_synthesized(db_path, base, target)
    assert _coverage(db_path, target) == (_ms(_BOUNDARY), _ms(_NOW))
    _assert_truthful_coverage(db_path, base, target)


@_BASES
@_TARGETS
def test_refill_that_fails_partway_leaves_a_gap_the_next_load_fills(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base: Timeframe,
    target: Timeframe,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, base)
    provider = _SourceProvider(base, paged=True)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, target)
    recent_start = controller_module._recent_history_start(
        target, _HISTORY_START, _NOW,
    )
    failed: list[Call] = []

    def fail_oldest_refill_page(
        timeframe: Timeframe, start: datetime, end: datetime,
    ) -> None:
        if timeframe == base and start == _BOUNDARY and not failed:
            failed.append((timeframe, start, end))
            raise RuntimeError("source unavailable")

    provider.before_fetch = fail_oldest_refill_page

    _refresh_and_reload(qtbot, window)

    # Criterion 7: the failure is reported without a dialog, and coverage
    # describes only bars that are present: the recent window for the
    # target, the committed run of pages for the base.
    assert failed and warnings == []
    assert window._history_status_message is not None
    assert window._history_status_message == (
        f"Older history could not be loaded: Loading XBI {target.value} history "
        "failed. Details were logged. It will be retried on the next load."
    )
    assert _coverage(db_path, target) == (_ms(recent_start), _ms(_NOW))
    base_coverage = _coverage(db_path, base)
    assert base_coverage is not None
    gap_end = _from_ms(base_coverage[0])
    assert _BOUNDARY < gap_end <= recent_start and base_coverage[1] == _ms(_NOW)
    _assert_truthful_coverage(db_path, base, target)
    kept = _expected_synthesized(base, target, last=date(2026, 8, 14))
    assert [
        row for row in _cached_rows(db_path, target) if row[0] < recent_start
    ] == kept
    assert not [
        row
        for row in _cached_rows(db_path, base)
        if _BOUNDARY <= row[0] < gap_end
    ]

    provider.calls.clear()
    _show(qtbot, window, target)

    assert warnings == []
    assert _covers(provider.calls, base, _BOUNDARY, gap_end)
    assert window._displayed_history_complete
    _assert_whole_cache_synthesized(db_path, base, target)
    assert _coverage(db_path, target) == (_ms(_BOUNDARY), _ms(_NOW))
    _assert_truthful_coverage(db_path, base, target)


def _stale_target_coverage(db_path: Path, target: Timeframe, retained: str) -> None:
    """
    Codex's R2 reproductions. "absent": the target's bars are cached without
    coverage. "ends-before": the target was last viewed during the
    2026-08-11 session at 13:00 ET, several sessions before _history_start:
    its coverage ends there, with a forming bar, while the base kept loading.
    """
    if retained == "absent":
        connection = sqlite3.connect(db_path)
        try:
            with connection:
                connection.execute(
                    "DELETE FROM bar_fetch_coverage"
                    " WHERE cache_namespace = ? AND symbol = ? AND timeframe = ?",
                    (_YAHOO, _SYMBOL, target.value),
                )
        finally:
            connection.close()
        return
    seed_start = datetime.combine(_SEED_START, time(0), tzinfo=ET)
    with Cache(str(db_path), registry=startup_registry()) as cache:
        last_viewed = datetime(2026, 8, 11, 13, 0, tzinfo=ET)
        stale = cache.get_raw_bars(_YAHOO, _SYMBOL, target, 0, _ms(last_viewed))
        forming_start = stale[-1].timestamp
        forming = _source_bars(Timeframe.MIN5, forming_start, last_viewed)
        stale[-1] = Bar(
            forming[0].timestamp,
            forming[0].open,
            max(bar.high for bar in forming),
            min(bar.low for bar in forming),
            forming[-1].close,
            sum(bar.volume for bar in forming),
        )
        cache.replace_materialized_bars(
            _YAHOO,
            _SYMBOL,
            target,
            0,
            2**62,
            stale,
            _ms(seed_start),
            _ms(last_viewed),
        )


@_SHAPES
@_TARGETS
@pytest.mark.parametrize("retained", ["absent", "ends-before"])
def test_refresh_with_absent_or_stale_target_coverage_keeps_one_bar_per_period(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paged: bool,
    target: Timeframe,
    retained: str,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, Timeframe.MIN5)
    _stale_target_coverage(db_path, target, retained)
    kept = [row for row in _cached_rows(db_path, target) if row[0] < _BOUNDARY]
    provider = _SourceProvider(Timeframe.MIN5, paged)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    window._state.symbol = _SYMBOL
    window._state.timeframe = target
    rebuilt = [
        row
        for row in _expected_synthesized(Timeframe.MIN5, target)
        if row[0] >= _BOUNDARY
    ]

    _refresh(window)
    _wait_terminal(qtbot, window)

    assert warnings == []
    rows = _cached_rows(db_path, target)
    _assert_one_bar_per_period(rows, target)
    assert rows == kept + rebuilt
    assert _coverage(db_path, target) == (_ms(_BOUNDARY), _ms(_NOW))
    _assert_truthful_coverage(db_path, Timeframe.MIN5, target)

    _show(qtbot, window, target)

    assert warnings == []
    rows = _cached_rows(db_path, target)
    _assert_one_bar_per_period(rows, target)
    assert rows == kept + rebuilt
    assert _coverage(db_path, target) == (_ms(_BOUNDARY), _ms(_NOW))
    _assert_truthful_coverage(db_path, Timeframe.MIN5, target)


def test_weekly_and_daily_refill_restores_the_boundary_bars(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, Timeframe.MIN5)
    provider = _SourceProvider(Timeframe.MIN5, paged=True)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.WEEKLY)

    _refresh_and_reload(qtbot, window)

    assert warnings == []
    weekly = _cached_rows(db_path, Timeframe.WEEKLY)
    assert weekly == _expected_native(Timeframe.WEEKLY, _FIRST_DAILY, _LAST_SESSION)
    assert _WEEKLY_BOUNDARY.astimezone(UTC) in {row[0] for row in weekly}
    assert (Timeframe.WEEKLY, _DAILY_HISTORY_START, _NOW) in [
        (call[0], call[1], call[2]) for call in provider.calls
    ]
    # Daily is refilled with the next intraday chart.
    _show(qtbot, window, Timeframe.MIN5)
    assert _cached_rows(db_path, Timeframe.DAILY) == _expected_native(
        Timeframe.DAILY, _FIRST_DAILY, _LAST_SESSION,
    )


# ----------------------------------------------------------------------
# 5b: recent-first refill for a date-paged source, with truthful coverage
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "timeframe", [Timeframe.MIN5, Timeframe.MIN39], ids=["5m", "39m"],
)
def test_paged_refill_renders_the_recent_window_before_older_history(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeframe: Timeframe,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, Timeframe.MIN5)
    provider = _SourceProvider(Timeframe.MIN5, paged=True)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, timeframe)
    recent_start = controller_module._recent_history_start(
        timeframe, _HISTORY_START, _NOW,
    )
    release = threading.Event()
    held: list[Call] = []
    returned: list[Call] = []

    def hold_older_history(
        fetched: Timeframe, start: datetime, end: datetime,
    ) -> None:
        if fetched == Timeframe.MIN5 and end <= recent_start:
            held.append((fetched, start, end))
            assert release.wait(20)
            returned.append((fetched, start, end))

    provider.before_fetch = hold_older_history
    _refresh(window)
    request = window._current_chart_request
    assert request is not None

    qtbot.waitUntil(lambda: window._partial_history_request == request, timeout=20_000)

    displayed = window._current_series
    assert displayed is not None and displayed.bars
    assert displayed.bars[0].timestamp >= recent_start
    assert not window._displayed_history_complete
    qtbot.waitUntil(lambda: bool(held), timeout=10_000)
    assert returned == []
    # During the recent-first phase coverage claims the recent window only.
    assert _coverage(db_path, Timeframe.MIN5) == (_ms(recent_start), _ms(_NOW))
    if timeframe == Timeframe.MIN39:
        assert _coverage(db_path, timeframe) == (_ms(recent_start), _ms(_NOW))
    _assert_truthful_coverage(db_path, Timeframe.MIN5, Timeframe.MIN39)

    release.set()
    _wait_terminal(qtbot, window)

    assert warnings == []
    assert window._displayed_history_complete
    full = window._current_series
    assert full is not None and full.bars[0].timestamp == _HISTORY_START
    assert _coverage(db_path, Timeframe.MIN5) == (_ms(_BOUNDARY), _ms(_NOW))
    _assert_truthful_coverage(db_path, Timeframe.MIN5, Timeframe.MIN39)
    if timeframe == Timeframe.MIN39:
        _assert_whole_cache_synthesized(db_path, Timeframe.MIN5, timeframe)
        assert _coverage(db_path, timeframe) == (_ms(_BOUNDARY), _ms(_NOW))


# ----------------------------------------------------------------------
# 5 and 10: the on-screen chart reloads; other timeframes refill lazily
# ----------------------------------------------------------------------


def test_refresh_on_5m_reloads_only_5m_and_daily_then_15m_loads_cold(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, Timeframe.MIN5, natives=(Timeframe.MIN15,))
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN5)
    assert provider.calls == []

    _refresh_and_reload(qtbot, window)

    assert warnings == []
    assert {call[0] for call in provider.calls} == {Timeframe.MIN5, Timeframe.DAILY}
    assert (Timeframe.MIN5, _HISTORY_START, _NOW) in provider.calls
    assert _covers(provider.calls, Timeframe.DAILY, _DAILY_BOUNDARY, _NOW)
    # The wrong bar and the bar the source no longer serves are gone.
    assert _cached_rows(db_path, Timeframe.MIN5) == _expected_native(
        Timeframe.MIN5, _SEED_START, _LAST_SESSION,
    )
    # Other timeframes are untouched until viewed.
    assert _coverage(db_path, Timeframe.MIN15) is None
    assert not [
        row for row in _cached_rows(db_path, Timeframe.MIN15) if row[0] >= _BOUNDARY
    ]

    provider.calls.clear()
    _show(qtbot, window, Timeframe.MIN15)

    assert warnings == []
    assert [call for call in provider.calls if call[0] == Timeframe.MIN15] == [
        (Timeframe.MIN15, _HISTORY_START, _NOW),
    ]
    assert _cached_rows(db_path, Timeframe.MIN15) == _expected_native(
        Timeframe.MIN15, _SEED_START, _LAST_SESSION,
    )


@pytest.mark.parametrize("case", ["other-symbol", "inactive-source"])
def test_refresh_of_data_not_on_screen_rewinds_without_reloading(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    db_path = tmp_path / "test.db"
    if case == "other-symbol":
        # SPY on Yahoo is refreshed while XBI on Yahoo is on screen.
        namespace, symbol, active = _YAHOO, _OTHER_SYMBOL, YFINANCE_CONNECTION_ID
        _seed(db_path, Timeframe.MIN5, symbol=_OTHER_SYMBOL)
        _seed(db_path, Timeframe.MIN5)
    else:
        # Yahoo XBI is refreshed while XBI is on screen from Alpaca.
        namespace, symbol, active = _YAHOO, _SYMBOL, ALPACA_PAPER_CONNECTION_ID
        _seed(db_path, Timeframe.MIN5)
        _seed(db_path, Timeframe.MIN5, namespace=_ALPACA)
    provider = _SourceProvider(Timeframe.MIN5, paged=True)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider, active)
    _show(qtbot, window, Timeframe.MIN5)
    shown = window._current_chart_request
    on_screen_namespace = window._route_for_symbol(_SYMBOL).cache_namespace
    on_screen_before = _cached_rows(db_path, Timeframe.MIN5, on_screen_namespace)
    provider.calls.clear()

    plan = window.plan_symbol_refresh(namespace, symbol)
    assert plan is not None
    plan.refresh()
    qtbot.wait(controller_module._CHART_LOAD_DEBOUNCE_MS * 3)

    assert warnings == []
    assert window._current_chart_request is shown
    assert window._pending_chart_load is None and not window._chart_load_tasks
    assert provider.calls == []
    assert not [
        row
        for row in _cached_rows(db_path, Timeframe.MIN5, namespace, symbol)
        if row[0] >= _BOUNDARY
    ]
    assert _coverage(db_path, Timeframe.MIN5, namespace, symbol) is None
    assert _cached_rows(db_path, Timeframe.MIN5, on_screen_namespace) == on_screen_before


# ----------------------------------------------------------------------
# 6: running and pending loads for the symbol cannot write after the rewind
# ----------------------------------------------------------------------


class _Gate:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()


def _instrument_rewind_and_starts(
    window: MainWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[tuple[str, bool]]]:
    """Record each rewind with the chart threads it saw, and each load start."""
    rewinds: list[str] = []
    starts: list[tuple[str, bool]] = []
    production_rewind = window._cache.rewind_symbol_history
    production_start = window._start_chart_load

    def rewind(*args: Any, **kwargs: Any) -> None:
        running = [
            task.request.timeframe.value
            for task in window._chart_load_tasks.values()
            if not task.thread.isFinished()
        ]
        rewinds.append(",".join(running))
        production_rewind(*args, **kwargs)

    def start(request: Any) -> None:
        starts.append((request.timeframe.value, bool(rewinds)))
        production_start(request)

    monkeypatch.setattr(window._cache, "rewind_symbol_history", rewind)
    monkeypatch.setattr(window, "_start_chart_load", start)
    return rewinds, starts


def _hold_tail_requests(
    provider: _SourceProvider,
    gates: dict[Timeframe, _Gate],
) -> None:
    # The cache ends on Thursday, so each load makes one tail request for
    # Friday, starting at Wednesday's open with its basis validation bars
    # (the last two cached sessions). A held tail returns a bar the source
    # does not serve after the refresh; it must be deleted by the rewind, not
    # written after it.
    for timeframe in gates:
        provider.extra_bars[timeframe] = [
            _source_bar(datetime(2026, 10, 23, 17, 30, tzinfo=ET), 777),
        ]

    def hold(timeframe: Timeframe, start: datetime, end: datetime) -> None:
        gate = gates.get(timeframe)
        if gate is not None and start >= datetime(2026, 10, 21, tzinfo=ET):
            gate.entered.set()
            assert gate.release.wait(20)

    provider.before_fetch = hold


def test_running_load_drains_before_rewind_and_pending_load_waits(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, Timeframe.MIN5, last_session=date(2026, 10, 22))
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    rewinds, starts = _instrument_rewind_and_starts(window, monkeypatch)
    gate = _Gate()
    _hold_tail_requests(provider, {Timeframe.MIN5: gate})
    window._state.symbol = _SYMBOL
    window._state.timeframe = Timeframe.MIN5
    window._load()
    qtbot.waitUntil(gate.entered.is_set, timeout=10_000)

    _refresh(window)
    # A load queued during the drain stays pending through its debounce.
    window._state.timeframe = Timeframe.MIN15
    window._load()
    pending = window._pending_chart_load
    qtbot.wait(controller_module._CHART_LOAD_DEBOUNCE_MS * 3)

    assert rewinds == []
    assert window._pending_chart_load is pending
    assert [task.request.timeframe for task in window._chart_load_tasks.values()] == [
        Timeframe.MIN5,
    ]

    gate.release.set()
    qtbot.waitUntil(lambda: bool(rewinds), timeout=10_000)
    _wait_terminal(qtbot, window)

    assert warnings == []
    assert rewinds == [""]
    assert starts == [("5m", False), ("15m", True)]
    assert not [
        row for row in _cached_rows(db_path, Timeframe.MIN5) if row[5] == 777
    ]
    assert _coverage(db_path, Timeframe.MIN5) is None


def test_pending_load_waits_when_one_of_two_draining_loads_finishes(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(
        db_path,
        Timeframe.MIN5,
        last_session=date(2026, 10, 22),
        natives=(Timeframe.MIN15,),
    )
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    rewinds, starts = _instrument_rewind_and_starts(window, monkeypatch)
    gates = {Timeframe.MIN5: _Gate(), Timeframe.MIN15: _Gate()}
    _hold_tail_requests(provider, gates)
    window._state.symbol = _SYMBOL
    window._state.timeframe = Timeframe.MIN5
    window._load()
    qtbot.waitUntil(gates[Timeframe.MIN5].entered.is_set, timeout=10_000)
    window._state.timeframe = Timeframe.MIN15
    window._load()
    qtbot.waitUntil(gates[Timeframe.MIN15].entered.is_set, timeout=10_000)

    _refresh(window)
    window._state.timeframe = Timeframe.MIN30
    window._load()
    pending = window._pending_chart_load
    gates[Timeframe.MIN5].release.set()
    qtbot.waitUntil(lambda: len(window._chart_load_tasks) == 1, timeout=10_000)
    qtbot.wait(controller_module._CHART_LOAD_DEBOUNCE_MS * 3)

    # The finished thread's dispatch is held by the barrier.
    assert rewinds == []
    assert window._pending_chart_load is pending
    assert starts == [("5m", False), ("15m", False)]

    gates[Timeframe.MIN15].release.set()
    qtbot.waitUntil(lambda: bool(rewinds), timeout=10_000)
    _wait_terminal(qtbot, window)

    assert warnings == []
    assert rewinds == [""]
    assert starts == [("5m", False), ("15m", False), ("30m", True)]
    for timeframe in (Timeframe.MIN5, Timeframe.MIN15):
        assert not [
            row for row in _cached_rows(db_path, timeframe) if row[5] == 777
        ]
        assert _coverage(db_path, timeframe) is None


# ----------------------------------------------------------------------
# 6f: a whole-day cutoff crossing between confirm, rewind and reload
# ----------------------------------------------------------------------

# With this limit the source's cutoff at _NOW is Friday 2026-08-14 09:31 ET,
# so _history_start is Monday 2026-08-17 09:30. Two minutes earlier the
# cutoff is 09:29 and _history_start is Friday 2026-08-14 09:30.
_CROSSING_LIMIT = timedelta(days=71, minutes=59)
_BEFORE_CROSSING = _NOW - timedelta(minutes=2)
_FRIDAY_OPEN = datetime(2026, 8, 14, 9, 30, tzinfo=ET)


def _assert_refill_reached(
    db_path: Path,
    provider: _SourceProvider,
    timeframe: Timeframe,
    boundary: datetime,
    rewinds: list[_RewindCall],
) -> None:
    """The refill reached exactly the deleted boundary; nothing is skipped."""
    assert [calls[0][Timeframe.MIN5] for calls in rewinds] == [_ms(boundary)]
    assert _covers(provider.calls, Timeframe.MIN5, boundary, _NOW)
    assert min(
        call[1] for call in provider.calls if call[0] == Timeframe.MIN5
    ) == boundary
    assert _cached_rows(db_path, Timeframe.MIN5) == _expected_native(
        Timeframe.MIN5, _SEED_START, _LAST_SESSION,
    )
    assert _coverage(db_path, Timeframe.MIN5) == (_ms(boundary), _ms(_NOW))
    if timeframe == Timeframe.MIN39:
        assert _cached_rows(db_path, timeframe) == _expected_synthesized(
            Timeframe.MIN5, timeframe,
        )
        assert _coverage(db_path, timeframe) == (_ms(boundary), _ms(_NOW))
    _assert_truthful_coverage(db_path, Timeframe.MIN5, Timeframe.MIN39)


@_SHAPES
@pytest.mark.parametrize(
    "timeframe", [Timeframe.MIN5, Timeframe.MIN39], ids=["5m", "39m"],
)
def test_confirm_before_cutoff_then_rewind_and_reload_after_it(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paged: bool,
    timeframe: Timeframe,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, Timeframe.MIN5, last_session=date(2026, 10, 22))
    provider = _SourceProvider(Timeframe.MIN5, paged, _CROSSING_LIMIT)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    monkeypatch.setattr(_FixedDateTime, "frozen", _BEFORE_CROSSING)
    rewinds = _record_rewinds(window, monkeypatch)
    gate = _Gate()
    _hold_tail_requests(provider, {Timeframe.MIN5: gate})
    # The refill below serves the source as is.
    provider.extra_bars.clear()
    window._state.symbol = _SYMBOL
    window._state.timeframe = timeframe
    window._load()
    qtbot.waitUntil(gate.entered.is_set, timeout=10_000)
    plan = window.plan_symbol_refresh(_YAHOO, _SYMBOL)
    assert plan is not None

    plan.refresh()
    monkeypatch.setattr(_FixedDateTime, "frozen", _NOW)
    provider.calls.clear()
    gate.release.set()
    qtbot.waitUntil(lambda: bool(rewinds), timeout=10_000)
    _wait_terminal(qtbot, window)

    assert warnings == []
    # The rewind ran after the cutoff moved: Friday's session is kept.
    _assert_refill_reached(db_path, provider, timeframe, _HISTORY_START, rewinds)
    assert [
        row
        for row in _cached_rows(db_path, Timeframe.MIN5)
        if _FRIDAY_OPEN <= row[0] < _HISTORY_START
    ] == _expected_native(Timeframe.MIN5, date(2026, 8, 14), date(2026, 8, 14))


@_SHAPES
@pytest.mark.parametrize(
    "timeframe", [Timeframe.MIN5, Timeframe.MIN39], ids=["5m", "39m"],
)
def test_rewind_before_cutoff_then_reload_worker_after_it(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paged: bool,
    timeframe: Timeframe,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, Timeframe.MIN5)
    provider = _SourceProvider(Timeframe.MIN5, paged, _CROSSING_LIMIT)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    monkeypatch.setattr(_FixedDateTime, "frozen", _BEFORE_CROSSING)
    _show(qtbot, window, timeframe)
    rewinds = _record_rewinds(window, monkeypatch)
    provider.calls.clear()

    _refresh(window)
    # The rewind has run; the reload's worker starts after the cutoff moved.
    assert len(rewinds) == 1 and window._chart_load_tasks == {}
    monkeypatch.setattr(_FixedDateTime, "frozen", _NOW)
    _wait_terminal(qtbot, window)

    assert warnings == []
    # The worker refills from the boundary the rewind deleted, Friday's
    # open, not from the later history start its own clock gives.
    _assert_refill_reached(db_path, provider, timeframe, _FRIDAY_OPEN, rewinds)


# ----------------------------------------------------------------------
# 8 and 9: Settings > Data Quality > Refresh symbol from source
# ----------------------------------------------------------------------


def _all_rows(db_path: Path) -> dict[str, list[Any]]:
    tables = [
        name
        for (name,) in _table_rows(
            db_path, "SELECT name FROM sqlite_master WHERE type = 'table'", (),
        )
    ]
    return {
        table: sorted(_table_rows(db_path, f"SELECT * FROM {table}", ()), key=repr)
        for table in tables
    }


def _settings_refresh(
    window: MainWindow,
    monkeypatch: pytest.MonkeyPatch,
    namespace: str,
    confirm: bool,
    before_refresh: Callable[[DataQualityTab], None] = lambda _tab: None,
    during_settings: Callable[[DataQualityTab], None] = lambda _tab: None,
    symbol: str = _SYMBOL,
) -> list[Any]:
    """
    Open Application Settings through MainWindow, click Refresh symbol from
    source on the Data Quality tab and answer the confirmation. Returns the
    confirmation dialogs shown.
    """
    confirmations: list[Any] = []

    def answer(dialog: Any) -> QDialog.DialogCode:
        confirmations.append(dialog)
        return (
            QDialog.DialogCode.Accepted if confirm else QDialog.DialogCode.Rejected
        )

    def use_settings(dialog: Any) -> QDialog.DialogCode:
        tab = dialog.findChild(DataQualityTab)
        assert tab is not None
        _choose(tab, namespace, symbol)
        before_refresh(tab)
        button = tab.findChild(QPushButton, "refreshSymbolFromSource")
        assert button is not None and button.isEnabled()
        button.click()
        during_settings(tab)
        dialog.reject()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(data_quality_module.SymbolRefreshDialog, "exec", answer)
    monkeypatch.setattr(ApplicationSettingsDialog, "exec", use_settings)
    window._on_application_settings()
    return confirmations


def _choose(tab: DataQualityTab, namespace: str, symbol: str) -> None:
    source = tab.findChild(QComboBox, "dataQualitySource")
    assert source is not None
    source.setCurrentIndex(source.findData(namespace))
    symbol_edit = tab.findChild(QLineEdit, "dataQualitySymbol")
    assert symbol_edit is not None
    symbol_edit.setText(symbol.lower())


def _result_rows(tab: DataQualityTab) -> int:
    table = tab.findChild(QTableWidget, "dataQualityBars")
    assert table is not None
    return table.rowCount()


def _load_rows(tab: DataQualityTab, timeframe: Timeframe, day: QDate) -> None:
    timeframe_box = tab.findChild(QComboBox, "dataQualityTimeframe")
    date_edit = tab.findChild(QDateEdit, "dataQualityDate")
    load = tab.findChild(QPushButton, "loadBarsForDate")
    assert timeframe_box is not None and date_edit is not None and load is not None
    timeframe_box.setCurrentIndex(timeframe_box.findData(timeframe))
    date_edit.setDate(day)
    load.click()


def _correct_volume(
    db_path: Path,
    namespace: str,
    timeframe: Timeframe,
    timestamp: datetime,
) -> None:
    with Cache(str(db_path), registry=startup_registry()) as cache:
        cache.put_bar_correction(
            BarCorrection(namespace, _SYMBOL, timeframe, timestamp, volume=4_321)
        )


def _expected_message(source: str, days: int, limited: bool) -> str:
    hidden = (
        "Cached intraday data older than that will no longer be shown, "
        f"because {source} does not provide it. "
        if limited
        else ""
    )
    return (
        f"{days} days of intraday data and daily history since 2016 can "
        f"safely be refreshed from {source}. {hidden}Proceed?"
    )


@pytest.mark.parametrize(
    ("active", "namespace", "limit", "source", "days"),
    [
        # 55 days before Saturday 10:30 is Sunday 2026-08-30; the range starts
        # Monday 2026-08-31 09:30, 54 days and an hour ago.
        pytest.param(
            YFINANCE_CONNECTION_ID, _YAHOO, timedelta(days=55), "Yahoo Finance",
            54, id="yahoo",
        ),
        # 600 days back is Monday 2025-03-03 09:30 EST, a session open.
        pytest.param(
            ALPACA_PAPER_CONNECTION_ID, _ALPACA, None, "Alpaca / IEX",
            600, id="alpaca",
        ),
    ],
)
def test_cancelled_confirmation_shows_computed_ranges_and_changes_nothing(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active: str,
    namespace: str,
    limit: timedelta | None,
    source: str,
    days: int,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, Timeframe.MIN5, namespace=namespace)
    # With a 55-day limit B is 2026-08-31 09:30; with 600 days every seeded
    # intraday bar is inside the refreshed range.
    for timeframe, timestamp in (
        (Timeframe.MIN5, datetime(2026, 9, 1, 11, 0, tzinfo=ET)),
        (Timeframe.MIN39, datetime(2026, 9, 1, 9, 30, tzinfo=ET)),
        (Timeframe.DAILY, datetime(2016, 1, 4, tzinfo=ET)),
        (Timeframe.MIN5, datetime(2026, 8, 13, 11, 0, tzinfo=ET)),
        (Timeframe.DAILY, datetime(2015, 12, 31, tzinfo=ET)),
    ):
        _correct_volume(db_path, namespace, timeframe, timestamp)
    provider = _SourceProvider(Timeframe.MIN5, paged=False, intraday_limit=limit)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider, active)
    _show(qtbot, window, Timeframe.MIN5)
    shown = window._current_chart_request
    provider.calls.clear()
    before = _all_rows(db_path)

    confirmations = _settings_refresh(window, monkeypatch, namespace, confirm=False)
    qtbot.wait(controller_module._CHART_LOAD_DEBOUNCE_MS * 3)

    assert warnings == []
    assert len(confirmations) == 1
    dialog = confirmations[0]
    assert dialog.windowTitle() == f"Refresh {_SYMBOL}?"
    message = dialog.findChild(QLabel, "symbolRefreshMessage")
    assert message is not None
    assert message.text() == _expected_message(source, days, limit is not None)
    buttons = dialog.findChildren(QPushButton)
    assert [button.text() for button in buttons] == ["Yes", "No"]
    assert [button.isDefault() for button in buttons] == [False, True]
    # No changes nothing and loads nothing.
    assert _all_rows(db_path) == before
    assert window._current_chart_request is shown
    assert provider.calls == []


def test_confirmed_refresh_runs_while_settings_is_open_and_keeps_corrections(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, Timeframe.MIN5)
    in_range = [
        (Timeframe.MIN5, datetime(2026, 9, 1, 11, 0, tzinfo=ET)),
        (Timeframe.MIN39, datetime(2026, 9, 1, 9, 30, tzinfo=ET)),
        (Timeframe.WEEKLY, datetime(2026, 9, 7, tzinfo=ET)),
    ]
    for timeframe, timestamp in in_range:
        _correct_volume(db_path, _YAHOO, timeframe, timestamp)
    _correct_volume(
        db_path, _YAHOO, Timeframe.MIN5, datetime(2026, 8, 13, 11, 0, tzinfo=ET),
    )
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN5)
    shown = window._current_chart_request
    corrections_before = _all_rows(db_path)["bar_corrections"]
    rewound_during_settings: list[bool] = []

    def load_rows(tab: DataQualityTab) -> None:
        _load_rows(tab, Timeframe.MIN5, QDate(2026, 9, 1))
        assert _result_rows(tab) == 78

    def check_rewound(tab: DataQualityTab) -> None:
        rewound_during_settings.append(
            _coverage(db_path, Timeframe.MIN5) is None
            and window._current_chart_request is not shown
            and _result_rows(tab) == 0
        )

    confirmations = _settings_refresh(
        window,
        monkeypatch,
        _YAHOO,
        confirm=True,
        before_refresh=load_rows,
        during_settings=check_rewound,
    )
    _wait_terminal(qtbot, window)

    assert warnings == []
    assert len(confirmations) == 1
    assert rewound_during_settings == [True]
    assert _all_rows(db_path)["bar_corrections"] == corrections_before
    assert (Timeframe.MIN5, _HISTORY_START, _NOW) in provider.calls
    assert _cached_rows(db_path, Timeframe.MIN5) == _expected_native(
        Timeframe.MIN5, _SEED_START, _LAST_SESSION,
    )
    displayed = window._current_series
    assert displayed is not None
    corrected = datetime(2026, 9, 1, 11, 0, tzinfo=ET).astimezone(UTC)
    assert [bar.volume for bar in displayed.bars if bar.timestamp == corrected] == [
        4_321,
    ]


# ----------------------------------------------------------------------
# 6e: Settings reopened after the confirmation, before the rewind
# ----------------------------------------------------------------------


def test_settings_reopened_before_the_rewind_drops_its_pre_refresh_rows(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, Timeframe.MIN5, last_session=date(2026, 10, 22))
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    gate = _Gate()
    _hold_tail_requests(provider, {Timeframe.MIN5: gate})
    window._state.symbol = _SYMBOL
    window._state.timeframe = Timeframe.MIN5
    window._load()
    qtbot.waitUntil(gate.entered.is_set, timeout=10_000)
    # Confirm the refresh and close Settings while the load drains.
    _settings_refresh(window, monkeypatch, _YAHOO, confirm=True)
    assert _coverage(db_path, Timeframe.MIN5) is not None
    bad_bar = _BAD_BAR.astimezone(UTC)
    rewinds = _record_rewinds(window, monkeypatch)
    seen: dict[str, Any] = {}

    def select_bad_bar(tab: DataQualityTab) -> None:
        _load_rows(tab, Timeframe.MIN5, QDate(2026, 8, 17))
        table = tab.findChild(QTableWidget, "dataQualityBars")
        assert table is not None
        times = [table.item(row, 1) for row in range(table.rowCount())]
        table.setCurrentCell(
            [item.text() if item is not None else "" for item in times].index("10:00"),
            0,
        )

    def reopened(dialog: Any) -> QDialog.DialogCode:
        tab = dialog.findChild(DataQualityTab)
        assert tab is not None
        _choose(tab, _YAHOO, _SYMBOL)
        select_bad_bar(tab)
        volume = tab.findChild(QLineEdit, "dataQualityVolume")
        apply = tab.findChild(QPushButton, "applyBarCorrection")
        assert volume is not None and apply is not None
        seen["stale_volume"] = volume.text()

        gate.release.set()
        qtbot.waitUntil(lambda: bool(rewinds), timeout=10_000)
        seen["rows_after_rewind"] = _result_rows(tab)
        seen["apply_after_rewind"] = apply.isEnabled()
        _wait_terminal(qtbot, window)

        # After the refill, a volume-only edit of the same bar.
        select_bad_bar(tab)
        volume.setText("3,000")
        apply.click()
        dialog.reject()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ApplicationSettingsDialog, "exec", reopened)
    window._on_application_settings()

    assert warnings == []
    assert seen == {
        "stale_volume": str(_BAD_VOLUME),
        "rows_after_rewind": 0,
        "apply_after_rewind": False,
    }
    with Cache(str(db_path), registry=provider_registry) as cache:
        inspection = cache.get_bar_inspection(_YAHOO, _SYMBOL, Timeframe.MIN5, bad_bar)
    assert inspection is not None
    assert _rows([inspection.raw_bar]) == _rows(
        _source_bars(Timeframe.MIN5, _BAD_BAR, _BAD_BAR + timedelta(minutes=5))
    )
    correction = inspection.correction
    assert correction is not None
    assert (
        correction.open,
        correction.high,
        correction.low,
        correction.close,
        correction.volume,
    ) == (None, None, None, None, 3_000)


# ----------------------------------------------------------------------
# G2: the refresh status line in Data Quality
# ----------------------------------------------------------------------

_WAITING = "Waiting for the current chart load to finish…"
_REFRESHING = (f"Refreshing {_SYMBOL}…", False)

Status = tuple[str, bool]


def _clock(moment: datetime) -> str:
    return moment.astimezone().strftime("%I:%M %p").lstrip("0")


def _refreshed_on_screen(symbol: str = _SYMBOL) -> Status:
    return (
        f"{symbol} refreshed at {_clock(_NOW)}. "
        "Other timeframes update when you view them.",
        True,
    )


def _refreshed_off_screen(symbol: str = _SYMBOL) -> Status:
    return (
        f"{symbol} refreshed at {_clock(_NOW)}. It downloads when you open it.",
        True,
    )


def _status_tab(
    qtbot: Any,
    window: MainWindow,
    symbol: str = _SYMBOL,
) -> DataQualityTab:
    """A Data Quality tab on the Yahoo source, as Settings would open it."""
    tab = DataQualityTab(
        window._cache,
        initial_cache_namespace=_YAHOO,
        initial_symbol=symbol,
        symbol_refresh=window,
    )
    qtbot.addWidget(tab)
    return tab


def _status(tab: DataQualityTab) -> Status:
    label = tab.findChild(QLabel, "symbolRefreshStatus")
    button = tab.findChild(QPushButton, "refreshSymbolFromSource")
    assert label is not None and button is not None
    return (label.text(), button.isEnabled())


def _banner_state(tab: DataQualityTab) -> object:
    """The status banner's refresh state, or None while it is hidden."""
    label = tab.findChild(QLabel, "symbolRefreshStatus")
    assert label is not None
    return None if label.isHidden() else label.property("refreshState")


def _record_status(window: MainWindow, tab: DataQualityTab) -> list[Status]:
    """The tab's status line and button after each status change."""
    seen: list[Status] = []
    window.connect_symbol_refresh_status(
        lambda _namespace, _symbol: seen.append(_status(tab))
    )
    return seen


def _click_refresh(tab: DataQualityTab, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        data_quality_module.SymbolRefreshDialog,
        "exec",
        lambda _dialog: QDialog.DialogCode.Accepted,
    )
    button = tab.findChild(QPushButton, "refreshSymbolFromSource")
    assert button is not None and button.isEnabled()
    button.click()


def _hold_older_history(provider: _SourceProvider) -> threading.Event:
    """Hold the reload's older 5m pages until the returned event is set."""
    recent_start = controller_module._recent_history_start(
        Timeframe.MIN5, _HISTORY_START, _NOW,
    )
    release = threading.Event()

    def hold(fetched: Timeframe, start: datetime, end: datetime) -> None:
        if fetched == Timeframe.MIN5 and end <= recent_start:
            assert release.wait(20)

    provider.before_fetch = hold
    return release


def test_status_shows_refreshing_until_the_on_screen_reload_completes(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, Timeframe.MIN5)
    provider = _SourceProvider(Timeframe.MIN5, paged=True)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN5)
    tab = _status_tab(qtbot, window)
    seen = _record_status(window, tab)
    assert _status(tab) == ("", True)
    assert _banner_state(tab) is None
    release = _hold_older_history(provider)

    _click_refresh(tab, monkeypatch)
    request = window._current_chart_request

    # No load was running, so the rewind ran at once: no waiting state.
    assert seen == [_REFRESHING]
    assert _banner_state(tab) == "running"
    qtbot.waitUntil(lambda: window._partial_history_request == request, timeout=20_000)
    # The recent window on screen does not end the refresh.
    assert seen == [_REFRESHING]

    release.set()
    _wait_terminal(qtbot, window)

    assert warnings == []
    assert seen == [_REFRESHING, _refreshed_on_screen()]
    assert _banner_state(tab) == "done"


def test_status_waits_for_the_drain_and_a_tab_opened_meanwhile_shows_it(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, Timeframe.MIN5, last_session=date(2026, 10, 22))
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    gate = _Gate()
    _hold_tail_requests(provider, {Timeframe.MIN5: gate})
    window._state.symbol = _SYMBOL
    window._state.timeframe = Timeframe.MIN5
    window._load()
    qtbot.waitUntil(gate.entered.is_set, timeout=10_000)
    tab = _status_tab(qtbot, window)
    seen = _record_status(window, tab)

    _click_refresh(tab, monkeypatch)

    assert seen == [(_WAITING, False)]
    # Settings opened again during the drain shows the refresh in progress;
    # a tab on another symbol does not.
    reopened = _status_tab(qtbot, window)
    other = _status_tab(qtbot, window, _OTHER_SYMBOL)
    assert _status(reopened) == (_WAITING, False)
    assert _banner_state(reopened) == "running"
    assert _status(other) == ("", True)
    assert _banner_state(other) is None

    gate.release.set()
    qtbot.waitUntil(lambda: len(seen) >= 2, timeout=10_000)
    _wait_terminal(qtbot, window)

    assert warnings == []
    assert seen == [(_WAITING, False), _REFRESHING, _refreshed_on_screen()]
    assert _status(reopened) == _refreshed_on_screen()
    assert _status(other) == ("", True)


def test_status_of_a_symbol_not_on_screen_is_refreshed_right_after_the_rewind(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, Timeframe.MIN5, symbol=_OTHER_SYMBOL)
    _seed(db_path, Timeframe.MIN5)
    provider = _SourceProvider(Timeframe.MIN5, paged=True)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN5)
    shown = window._current_chart_request
    tab = _status_tab(qtbot, window, _OTHER_SYMBOL)
    on_screen = _status_tab(qtbot, window)
    seen = _record_status(window, tab)

    _click_refresh(tab, monkeypatch)

    assert warnings == []
    assert seen == [_refreshed_off_screen(_OTHER_SYMBOL)]
    assert _banner_state(tab) == "done"
    assert window._current_chart_request is shown
    # Another symbol's refresh leaves this tab's status alone.
    assert _status(on_screen) == ("", True)


_CREDENTIAL_MARKER = "PKSYNTHETICSECRET0123"


@pytest.mark.parametrize("paged", [True, False], ids=["after-recent", "whole-load"])
def test_status_of_a_failed_reload_never_shows_the_error_text(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paged: bool,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, Timeframe.MIN5)
    provider = _SourceProvider(Timeframe.MIN5, paged=paged)
    window, _warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN5)
    tab = _status_tab(qtbot, window)
    seen = _record_status(window, tab)

    def fail_oldest_refill_page(
        timeframe: Timeframe, start: datetime, end: datetime,
    ) -> None:
        if timeframe == Timeframe.MIN5 and start == _BOUNDARY:
            # Provider errors can echo request headers, credentials included.
            raise RuntimeError(
                f"Invalid header value b'APCA-API-KEY-ID: {_CREDENTIAL_MARKER}'"
            )

    provider.before_fetch = fail_oldest_refill_page

    _click_refresh(tab, monkeypatch)
    _wait_terminal(qtbot, window)

    stopped = (
        f"{_SYMBOL} refresh stopped: the download failed. "
        "The missing data downloads on the next load.",
        True,
    )
    assert seen == [_REFRESHING, stopped]
    assert _status(tab) == stopped
    assert _banner_state(tab) == "failed"
    status = window.symbol_refresh_status(_YAHOO, _SYMBOL)
    assert status is not None and not status.running
    assert _CREDENTIAL_MARKER not in status.text


def test_status_of_a_superseded_reload_says_it_downloads_when_opened(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, Timeframe.MIN5)
    provider = _SourceProvider(Timeframe.MIN5, paged=True)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN5)
    tab = _status_tab(qtbot, window)
    seen = _record_status(window, tab)
    release = _hold_older_history(provider)
    _click_refresh(tab, monkeypatch)
    request = window._current_chart_request
    qtbot.waitUntil(lambda: window._partial_history_request == request, timeout=20_000)

    # The user switches timeframe before the reload completes.
    window._state.timeframe = Timeframe.MIN15
    window._load()

    assert seen == [_REFRESHING, _refreshed_off_screen()]
    release.set()
    _wait_terminal(qtbot, window)

    assert warnings == []
    assert seen == [_REFRESHING, _refreshed_off_screen()]


# ----------------------------------------------------------------------
# Shutdown and rewind errors end the refresh status
# ----------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["before-dispatch", "during-refill", "during-drain"])
def test_shutdown_leaves_no_refresh_running(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    db_path = tmp_path / "test.db"
    if stage == "during-drain":
        _seed(db_path, Timeframe.MIN5, last_session=date(2026, 10, 22))
    else:
        _seed(db_path, Timeframe.MIN5)
    provider = _SourceProvider(Timeframe.MIN5, paged=True)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    gate = _Gate()
    release = threading.Event()
    if stage == "during-drain":
        _hold_tail_requests(provider, {Timeframe.MIN5: gate})
        window._state.symbol = _SYMBOL
        window._state.timeframe = Timeframe.MIN5
        window._load()
        qtbot.waitUntil(gate.entered.is_set, timeout=10_000)
    else:
        _show(qtbot, window, Timeframe.MIN5)
        release = _hold_older_history(provider)
    tab = _status_tab(qtbot, window)

    _click_refresh(tab, monkeypatch)
    if stage == "before-dispatch":
        assert window._pending_chart_load is not None
    elif stage == "during-refill":
        request = window._current_chart_request
        qtbot.waitUntil(
            lambda: window._partial_history_request == request, timeout=20_000,
        )
    else:
        assert _status(tab) == (_WAITING, False)

    window.close()
    gate.release.set()
    release.set()
    qtbot.waitUntil(lambda: not window._chart_load_tasks, timeout=10_000)
    qtbot.wait(controller_module._CHART_LOAD_DEBOUNCE_MS * 3)

    assert warnings == []
    assert window._refresh_reload is None
    assert not window._symbol_refreshes
    status = window.symbol_refresh_status(_YAHOO, _SYMBOL)
    assert status is not None and not status.running
    assert _status(tab) == _refreshed_off_screen()


@pytest.mark.parametrize("path", ["immediate", "after-drain"])
def test_rewind_error_stops_the_refresh_and_lifts_the_barrier(
    provider_registry: ProviderRegistry,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, Timeframe.MIN5, last_session=date(2026, 10, 22))
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    window, warnings = _window(provider_registry, qtbot, db_path, monkeypatch, provider)
    gate = _Gate()
    if path == "after-drain":
        _hold_tail_requests(provider, {Timeframe.MIN5: gate})
        window._state.symbol = _SYMBOL
        window._state.timeframe = Timeframe.MIN5
        window._load()
        qtbot.waitUntil(gate.entered.is_set, timeout=10_000)
    else:
        _show(qtbot, window, Timeframe.MIN5)
    shown = window._current_chart_request
    tab = _status_tab(qtbot, window)
    seen = _record_status(window, tab)
    rewinds: list[str] = []

    def locked(*_args: Any) -> None:
        rewinds.append("attempted")
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(window._cache, "rewind_symbol_history", locked)

    _click_refresh(tab, monkeypatch)
    if path == "after-drain":
        assert seen == [(_WAITING, False)]
        # A load queued during the drain waits behind the barrier.
        window._state.timeframe = Timeframe.MIN15
        window._load()
        gate.release.set()
        qtbot.waitUntil(lambda: len(seen) == 2, timeout=10_000)

    stopped = (
        f"{_SYMBOL} refresh stopped: the cached data could not be cleared. "
        "Try again.",
        True,
    )
    assert rewinds == ["attempted"]
    assert seen[-1] == stopped
    assert not window._symbol_refreshes
    assert window._refresh_reload is None
    if path == "immediate":
        assert window._current_chart_request is shown
        window._state.timeframe = Timeframe.MIN15
        window._load()
    # Normal loads continue.
    _wait_terminal(qtbot, window)
    request = window._current_chart_request
    assert request is not None and request.timeframe == Timeframe.MIN15
    assert warnings == []
    assert _status(tab) == stopped
    assert _banner_state(tab) == "failed"


def test_symbol_box_search_selects_a_listing_and_keeps_direct_ticker_entry(
    provider_registry: ProviderRegistry,
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    window, _ = _window(provider_registry, qtbot, tmp_path / "symbol-search.db", monkeypatch, provider)
    first_started = threading.Event()
    release_first = threading.Event()
    queries: list[str] = []

    def search(query: str) -> tuple[SymbolMatch, ...]:
        queries.append(query)
        if query in {"Ali", "Slow"}:
            first_started.set()
            release_first.wait(10)
            return (SymbolMatch("OLD", "Old result", "NYSE"),)
        return (
            SymbolMatch("BABA", "Alibaba Group", "NYSE"),
            SymbolMatch("9988.HK", "Alibaba Group", "Hong Kong"),
        )

    monkeypatch.setattr(provider, "search_symbols", search)
    window.show()
    _show(qtbot, window, Timeframe.DAILY)
    bar = window._symbol_bar
    field = bar.findChild(QLineEdit, "symbolInput")
    assert field is not None
    assert field.placeholderText() == "Symbol or company name"
    popup = bar._completer.popup()
    assert popup is not None
    generation = window._chart_load_generation
    field.setFocus()
    qtbot.waitUntil(field.hasSelectedText)
    try:
        qtbot.keyClicks(field, "Ali")
        qtbot.waitUntil(first_started.is_set)
        qtbot.keyClicks(field, "baba")
        release_first.set()
        qtbot.waitUntil(popup.isVisible)
        assert queries == ["Ali", "Alibaba"]
        model = popup.model()
        assert model is not None
        assert model.rowCount() == 2
        assert "Alibaba Group" in model.index(0, 0).data()
        assert "Hong Kong" in model.index(1, 0).data()
        assert window._chart_load_generation == generation
        qtbot.keyClick(popup, Qt.Key.Key_Down)
        qtbot.keyClick(popup, Qt.Key.Key_Down)
        qtbot.keyClick(popup, Qt.Key.Key_Return)
        _wait_terminal(qtbot, window)
        assert window._loaded_symbol == "9988.HK"
        assert window._chart_load_generation == generation + 1
        field.setFocus()
        field.selectAll()
        qtbot.keyClicks(field, "BABA")
        qtbot.keyClick(field, Qt.Key.Key_Return)
        _wait_terminal(qtbot, window)
        assert window._loaded_symbol == "BABA"
        assert window._chart_load_generation == generation + 2
        assert queries == ["Ali", "Alibaba"]
        field.setFocus()
        field.selectAll()
        qtbot.keyClicks(field, "Alibaba")
        qtbot.waitUntil(popup.isVisible)
        result_rect = popup.visualRect(model.index(0, 0))
        qtbot.mouseClick(popup.viewport(), Qt.MouseButton.LeftButton, pos=result_rect.center())
        _wait_terminal(qtbot, window)
        assert window._loaded_symbol == "BABA"
        assert window._chart_load_generation == generation + 3
        field.setFocus()
        field.selectAll()
        qtbot.keyClicks(field, "BABA")
        qtbot.waitUntil(popup.isVisible)
        qtbot.keyClick(popup, Qt.Key.Key_Down)
        qtbot.keyClick(popup, Qt.Key.Key_Down)
        qtbot.keyClick(popup, Qt.Key.Key_Return)
        _wait_terminal(qtbot, window)
        assert window._loaded_symbol == "9988.HK"
        assert window._chart_load_generation == generation + 4
        field.setFocus()
        field.selectAll()
        qtbot.keyClicks(field, "IBM")
        qtbot.waitUntil(popup.isVisible)
        qtbot.keyClick(popup, Qt.Key.Key_Return)
        _wait_terminal(qtbot, window)
        assert window._loaded_symbol == "IBM"
        assert window._chart_load_generation == generation + 5
        first_started.clear()
        release_first.clear()
        field.selectAll()
        qtbot.keyClicks(field, "Slow")
        qtbot.waitUntil(first_started.is_set)
        window.close()
        assert not window._close_finalized
        assert not popup.isVisible()
    finally:
        release_first.set()
        window.close()
        qtbot.waitUntil(lambda: window._close_finalized)


_SYNTHETIC_FETCH_ERROR = "synthetic reference fetch failure"


def _assert_fetch_failure_logged(caplog: pytest.LogCaptureFixture) -> None:
    record, = caplog.records
    assert record.levelno == logging.ERROR
    assert "RuntimeError" in record.getMessage()
    assert _SYNTHETIC_FETCH_ERROR not in record.getMessage()
    assert record.exc_info is None


def test_symbol_search_fetch_failure_keeps_main_window_running(
    provider_registry: ProviderRegistry,
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def search(_query: str) -> tuple[SymbolMatch, ...]:
        raise RuntimeError(_SYNTHETIC_FETCH_ERROR)

    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    monkeypatch.setattr(provider, "search_symbols", search)
    window, _ = _window(provider_registry, qtbot, tmp_path / "search-failure.db", monkeypatch, provider)
    window.show()
    _show(qtbot, window, Timeframe.DAILY)
    bar = window._symbol_bar
    field = bar.findChild(QLineEdit, "symbolInput")
    assert field is not None
    field.setFocus()
    qtbot.waitUntil(field.hasSelectedText)
    popup = bar._completer.popup()
    assert popup is not None
    caplog.clear()

    with qtbot.captureExceptions() as escaped:
        try:
            with qtbot.waitSignal(bar.search_idle, timeout=5_000):
                qtbot.keyClicks(field, "Failure")  # type: ignore[no-untyped-call]
            assert window.isVisible() and not window._close_finalized
            assert not popup.isVisible()
            assert bar._result_model.rowCount() == 0
        finally:
            window.close()
            qtbot.waitUntil(lambda: window._close_finalized, timeout=5_000)

    assert escaped == []
    _assert_fetch_failure_logged(caplog)


def test_typing_on_plot_reuses_symbol_field_and_loads_once(
    provider_registry: ProviderRegistry,
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    window, _ = _window(provider_registry, qtbot, tmp_path / "chart-typing.db", monkeypatch, provider)
    window.show()
    _show(qtbot, window, Timeframe.DAILY)
    plot = window._chart._master
    symbol_input = window._symbol_bar.findChild(QLineEdit, "symbolInput")
    assert symbol_input is not None
    qtbot.mouseClick(plot.viewport(), Qt.MouseButton.LeftButton)
    assert plot.hasFocus()
    window._on_timeframe_changed(Timeframe.WEEKLY)
    generation = window._chart_load_generation

    with monkeypatch.context() as patch:
        patch.setattr(type(window._extension_runtime), "drag_active", property(lambda _runtime: True))
        qtbot.keyClick(plot, Qt.Key.Key_Z)
        assert plot.hasFocus()
        assert symbol_input.text() == _SYMBOL

    qtbot.keyClick(plot, Qt.Key.Key_G, Qt.KeyboardModifier.ControlModifier)
    assert symbol_input.text() == _SYMBOL
    qtbot.keyClick(plot, Qt.Key.Key_G)
    assert symbol_input.hasFocus()
    assert symbol_input.text().upper() == "G"
    _wait_terminal(qtbot, window)
    assert symbol_input.text().upper() == "G"
    qtbot.keyClicks(symbol_input, "OOG")
    assert symbol_input.text().upper() == "GOOG"
    assert window._chart_load_generation == generation
    qtbot.keyClick(symbol_input, Qt.Key.Key_Return)
    _wait_terminal(qtbot, window)
    assert window._loaded_symbol == "GOOG"
    assert window._chart_load_generation == generation + 1


def test_watchlist_arrow_keys_load_the_selected_chart(
    provider_registry: ProviderRegistry,
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    window, _ = _window(provider_registry, qtbot, tmp_path / "watchlist.db", monkeypatch, provider)
    symbols = window._watchlist.findChild(SymbolListWidget)
    assert symbols is not None
    for symbol in (_SYMBOL, _OTHER_SYMBOL):
        window._watchlist.add_symbol(symbol)
    window.show()
    _show(qtbot, window, Timeframe.DAILY)
    symbols.setFocus()

    window._watchlist.set_active_symbol(_OTHER_SYMBOL)
    assert window._loaded_symbol == _SYMBOL
    assert window._pending_chart_load is None
    window._watchlist.set_active_symbol(_SYMBOL)
    qtbot.keyClick(symbols, Qt.Key.Key_Down)
    _wait_terminal(qtbot, window)
    assert window._loaded_symbol == _OTHER_SYMBOL
    qtbot.keyClick(symbols, Qt.Key.Key_Up)
    _wait_terminal(qtbot, window)
    assert window._loaded_symbol == _SYMBOL


def test_symbol_details_retargets_without_showing_late_news(
    provider_registry: ProviderRegistry,
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    window, _ = _window(provider_registry, qtbot, tmp_path / "details.db", monkeypatch, provider)
    slow_news = threading.Event()
    release_news = threading.Event()

    def news(symbol: str) -> tuple[NewsArticle, ...]:
        if symbol == _SYMBOL:
            slow_news.set()
            release_news.wait(10)
            return (NewsArticle("Old headline", None, None, None),)
        return (NewsArticle("QQQ headline", "Publisher", None, None),)

    def profile(symbol: str) -> SymbolProfile:
        if symbol == "QQQ":
            return SymbolProfile(
                is_etf=True, name="QQQ fund", expense_ratio=0.0018,
                holdings=(Holding("TEST", "Test holding", 0.12),),
            )
        return SymbolProfile(name="Test stock", float_shares=1234)

    monkeypatch.setattr(provider, "fetch_news", news)
    monkeypatch.setattr(provider, "fetch_profile", profile)
    window.show()
    _show(qtbot, window, Timeframe.DAILY)
    banner = window._app_header.findChild(QLabel, "level1Quote")
    assert banner is not None
    request = controller_module._Level1Request(
        _SYMBOL, window._level1_symbol_generation, window._quote_route_identity(_SYMBOL),
    )
    quote = controller_module.Level1Quote(
        _SYMBOL, "Name & Co", 100, 1, 1, 99, 2, 101, 3, 100, 102, 98, 550, 99,
    )
    window._on_level1(request, quote)
    assert 'href="symbol-details"' in banner.text()
    assert "Name &amp; Co" in banner.text()
    try:
        qtbot.mouseClick(banner, Qt.MouseButton.LeftButton, pos=QPoint(7, banner.height() // 2))
        dialog = window._symbol_details.dialog
        assert dialog is not None
        assert not dialog.isModal()
        assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
        title = dialog.findChild(QLabel, "dialogTitle")
        assert title is not None and title.text() == "Symbol Details"
        assert dialog.symbol == _SYMBOL
        assert dialog._quote_values[0].text() == "100.00"
        qtbot.waitUntil(slow_news.is_set)
        qtbot.waitUntil(lambda: "1,234" in dialog._profile.toPlainText())
        window._on_symbol_changed("QQQ")
        assert dialog.symbol == _SYMBOL
        assert dialog._quote_status.text() == "Last received Level 1 quote"
        banner.linkActivated.emit("symbol-details")
        assert dialog.symbol == "QQQ"
        assert dialog._quote_values[0].text() == "—"
        qtbot.waitUntil(lambda: "Top holdings (1)" in dialog._profile.toPlainText())
        assert "0.18%" in dialog._profile.toPlainText()
        assert "Next U.S. witching date" in dialog._profile.toPlainText()
        assert "Next scheduled effective date" in dialog._profile.toPlainText()
        release_news.set()
        qtbot.waitUntil(lambda: not window._symbol_details.busy)
        assert "QQQ headline" in dialog._news.toPlainText()
        assert "Old headline" not in dialog._news.toPlainText()
        dialog.close()
        slow_news.clear()
        release_news.clear()
        window._on_symbol_changed(_SYMBOL)
        banner.linkActivated.emit("symbol-details")
        qtbot.waitUntil(slow_news.is_set)
        window.close()
        assert not dialog.isVisible()
        assert not window._close_finalized
    finally:
        release_news.set()
        window.close()
        qtbot.waitUntil(lambda: window._close_finalized)


def _open_symbol_details(window: MainWindow) -> symbol_details_module.SymbolDetailsDialog:
    banner = window._app_header.findChild(QLabel, "level1Quote")
    assert banner is not None
    assert 'href="symbol-details"' in banner.text()
    banner.linkActivated.emit("symbol-details")
    dialog = window._symbol_details.dialog
    assert dialog is not None
    return dialog


@pytest.mark.parametrize("section", ["profile", "news"])
def test_symbol_details_fetch_failure_keeps_main_window_running(
    provider_registry: ProviderRegistry,
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    section: str,
) -> None:
    def no_data(_symbol: str) -> None:
        return None

    def fail(_symbol: str) -> None:
        raise RuntimeError(_SYNTHETIC_FETCH_ERROR)

    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    monkeypatch.setattr(
        provider, "fetch_profile", fail if section == "profile" else no_data,
    )
    monkeypatch.setattr(
        provider, "fetch_news", fail if section == "news" else no_data,
    )
    window, _ = _window(provider_registry, qtbot, tmp_path / "details-failure.db", monkeypatch, provider)
    window.show()
    _show(qtbot, window, Timeframe.DAILY)
    caplog.clear()

    with qtbot.captureExceptions() as escaped:
        try:
            dialog = _open_symbol_details(window)
            qtbot.waitUntil(lambda: not window._symbol_details.busy, timeout=5_000)
            assert window.isVisible() and not window._close_finalized
            assert dialog.isVisible()
            panel = dialog._profile if section == "profile" else dialog._news
            text = panel.toPlainText()
        finally:
            window.close()
            qtbot.waitUntil(lambda: window._close_finalized, timeout=5_000)

    assert escaped == []
    assert text == f"Yahoo Finance {section} unavailable."
    _assert_fetch_failure_logged(caplog)


def test_symbol_details_news_renders_market_time_and_omits_invalid_time(
    provider_registry: ProviderRegistry,
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def news(_symbol: str) -> tuple[NewsArticle, ...]:
        return (
            NewsArticle(
                "Current headline", "Publisher", datetime(2026, 8, 14, 1, 30, tzinfo=UTC), None,
            ),
            NewsArticle("Year-one headline", "Archive", datetime(1, 1, 1, tzinfo=UTC), None),
        )

    def profile(_symbol: str) -> None:
        return None

    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    monkeypatch.setattr(provider, "fetch_news", news)
    monkeypatch.setattr(provider, "fetch_profile", profile)
    window, _ = _window(provider_registry, qtbot, tmp_path / "news-timestamps.db", monkeypatch, provider)
    window.show()
    _show(qtbot, window, Timeframe.DAILY)

    with qtbot.captureExceptions() as escaped:
        try:
            dialog = _open_symbol_details(window)
            qtbot.waitUntil(lambda: not window._symbol_details.busy, timeout=5_000)
            lines = dialog._news.toPlainText().splitlines()
        finally:
            window.close()
            qtbot.waitUntil(lambda: window._close_finalized, timeout=5_000)

    assert escaped == []
    assert lines == [
        "Current headline", "Publisher · Aug 13, 2026 · 21:30 ET",
        "Year-one headline", "Archive",
    ]


def test_chart_event_settings_persist_and_update_markers_independently(
    provider_registry: ProviderRegistry,
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    window, _ = _window(provider_registry, qtbot, tmp_path / "events.db", monkeypatch, provider)
    window.show()
    _show(qtbot, window, Timeframe.DAILY)
    series = window._current_series
    assert series is not None
    event_day = series.bars[-2].timestamp.date()
    monkeypatch.setattr(
        provider, "fetch_past_earnings", lambda _symbol: (event_day,), raising=False,
    )
    monkeypatch.setattr(
        provider, "fetch_dividend_dates", lambda _symbol: (event_day,), raising=False,
    )
    plot = window._chart.plot_manager

    def set_options(earnings: bool, dividends: bool) -> None:
        def use_settings(dialog: ApplicationSettingsDialog) -> int:
            for name, checked in (
                ("showPastEarnings", earnings), ("showDividends", dividends),
            ):
                checkbox = dialog.findChild(QCheckBox, name)
                assert checkbox is not None
                checkbox.setChecked(checked)
            dialog.accept()
            return dialog.result()

        monkeypatch.setattr(ApplicationSettingsDialog, "exec", use_settings)
        window._on_application_settings()
        _wait_terminal(qtbot, window)
        qtbot.waitUntil(lambda: not window._earnings.busy)

    assert plot.active_marker_keys() == []
    set_options(True, True)
    assert window._cache.get_chart_event_visibility() == (True, True)
    assert len(plot.active_marker_keys()) == 1
    key = plot.active_marker_keys()[0]
    assert plot._markers[key].toPlainText() == "E D"
    window._reload_extensions(draw_bars=False)
    assert plot.active_marker_keys() == [key]
    window._on_timeframe_changed(Timeframe.WEEKLY)
    _wait_terminal(qtbot, window)
    weekly_key, = plot.active_marker_keys()
    assert plot._markers[weekly_key].toPlainText() == "E D"
    window._on_timeframe_changed(Timeframe.MIN5)
    _wait_terminal(qtbot, window)
    assert plot.active_marker_keys() == [key]
    set_options(False, True)
    assert window._cache.get_chart_event_visibility() == (False, True)
    assert plot._markers[key].toPlainText() == "D"
    set_options(False, False)
    assert plot.active_marker_keys() == []


def test_late_historical_event_lookup_cannot_mark_new_symbol(
    provider_registry: ProviderRegistry,
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    window, _ = _window(provider_registry, qtbot, tmp_path / "event-switch.db", monkeypatch, provider)
    window.show()
    _show(qtbot, window, Timeframe.DAILY)
    series = window._current_series
    assert series is not None
    event_day = series.bars[-2].timestamp.date()
    started = threading.Event()
    release = threading.Event()

    def fetch_events(symbol: str) -> tuple[date, ...]:
        if symbol == _SYMBOL:
            started.set()
            release.wait(10)
            return (event_day,)
        return ()

    monkeypatch.setattr(provider, "fetch_past_earnings", fetch_events, raising=False)
    window._cache.set_chart_event_visibility(True, False)
    try:
        window._on_symbol_changed(_SYMBOL)
        qtbot.waitUntil(started.is_set)
        window._on_symbol_changed(_OTHER_SYMBOL)
        _wait_terminal(qtbot, window)
        release.set()
        qtbot.waitUntil(lambda: not window._earnings.busy)
        assert window._loaded_symbol == _OTHER_SYMBOL
        assert window._chart.plot_manager.active_marker_keys() == []
    finally:
        release.set()


def test_market_clock_stays_visible_during_status_messages(
    provider_registry: ProviderRegistry,
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(market_status_bar_module, "datetime", _FixedDateTime)
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    window, _ = _window(provider_registry, qtbot, tmp_path / "clock.db", monkeypatch, provider)
    window.resize(1200, 800)
    window.show()
    status = window.statusBar()
    assert isinstance(status, market_status_bar_module.MarketStatusBar)
    clock = status.findChild(QLabel, "marketClock")
    shortcuts = status.findChild(QLabel, "chartShortcutHints")
    message = status.findChild(QLabel, "marketStatusMessage")
    assert clock is not None
    assert shortcuts is not None
    assert message is not None
    source = window._data_source_status
    assert clock.geometry().right() < shortcuts.geometry().left()
    assert shortcuts.geometry().right() < source.geometry().left()
    content = shortcuts.parentWidget()
    assert content is not None
    assert abs(shortcuts.geometry().center().x() - content.rect().center().x()) <= 4

    monkeypatch.setattr(_FixedDateTime, "frozen", datetime(2026, 12, 1, 19, 23, tzinfo=UTC))
    qtbot.waitUntil(lambda: clock.text() == "14:23 ET")
    status.showMessage("<b>Temporary status</b>", 200)
    assert message.isVisible() and message.text() == "<b>Temporary status</b>"
    assert message.textFormat() == Qt.TextFormat.PlainText
    assert clock.isVisible() and source.isVisible()
    assert not shortcuts.isVisible()
    assert clock.contentsRect().left() >= 6
    font = clock.font()
    assert font.pixelSize() == 12
    for label in (shortcuts, message, source._source_label):
        other = label.font()
        assert other.pixelSize() == font.pixelSize()
        assert other.family() == font.family()
        assert other.weight() == font.weight()
        assert other.style() == font.style()
    qtbot.waitUntil(shortcuts.isVisible)
    assert not message.isVisible()

    monkeypatch.setattr(_FixedDateTime, "frozen", datetime(2026, 7, 1, 18, 24, tzinfo=UTC))
    qtbot.waitUntil(lambda: clock.text() == "14:24 ET")
    window.close()
    assert not status._clock_timer.isActive()
    qtbot.waitUntil(lambda: window._close_finalized)


def test_upcoming_earnings_discards_old_selection_and_drains_on_close(
    provider_registry: ProviderRegistry,
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    first_started = threading.Event()
    first_release = threading.Event()
    second_started = threading.Event()
    second_release = threading.Event()
    calls: list[str] = []

    def earnings(symbol: str) -> tuple[date, date] | None:
        calls.append(symbol)
        if symbol == "AAPL":
            first_started.set()
            first_release.wait(15)
            return date(2030, 1, 2), date(2030, 1, 2)
        second_started.set()
        second_release.wait(15)
        return None

    monkeypatch.setattr(provider, "fetch_upcoming_earnings", earnings, raising=False)
    window, _ = _window(provider_registry, qtbot, tmp_path / "earnings.db", monkeypatch, provider)
    window.show()
    try:
        window._on_symbol_changed("AAPL")
        qtbot.waitUntil(first_started.is_set)
        window._on_watchlist_symbol_selected("MSFT")
        _wait_terminal(qtbot, window)
        assert not second_started.is_set()
        status = window.statusBar()
        assert status is not None
        shortcuts = status.findChild(QLabel, "chartShortcutHints")
        quote_label = window._app_header.findChild(QLabel, "level1Quote")
        assert shortcuts is not None
        assert quote_label is not None
        assert "Next earnings" in quote_label.text()
        source_label = window._data_source_status._source_label
        assert quote_label.font().pixelSize() == 12
        assert shortcuts.font().pixelSize() == quote_label.font().pixelSize()
        assert source_label.font().pixelSize() == quote_label.font().pixelSize()
        status.showMessage("Temporary status")
        assert window._data_source_status.isVisible()
        assert quote_label.isVisible()
        assert not shortcuts.isVisible()
        status.clearMessage()
        assert shortcuts.isVisible()
        assert shortcuts.geometry().right() < window._data_source_status.geometry().left()
        first_release.set()
        qtbot.waitUntil(second_started.is_set)
        assert quote_label.text().endswith(">—</span>")
        window._on_timeframe_changed(Timeframe.WEEKLY)
        _wait_terminal(qtbot, window)
        second_release.set()
        qtbot.waitUntil(lambda: not window._earnings.busy)
        assert calls == ["AAPL", "MSFT"]

        second_started.clear()
        second_release.clear()
        window._on_watchlist_symbol_selected("AMZN")
        qtbot.waitUntil(second_started.is_set)
        _wait_terminal(qtbot, window)
        window.close()
        assert not window._close_finalized
        second_release.set()
        qtbot.waitUntil(lambda: window._close_finalized)
        assert calls == ["AAPL", "MSFT", "AMZN"]
    finally:
        first_release.set()
        second_release.set()
        if not window._close_finalized:
            window.close()
            qtbot.waitUntil(lambda: window._close_finalized)


@pytest.mark.parametrize(("days_until", "highlighted"), [(-1, False), (0, True), (5, True), (6, False)])
def test_upcoming_earnings_highlights_dates_within_five_days(
    provider_registry: ProviderRegistry,
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    days_until: int, highlighted: bool,
) -> None:
    from app import earnings

    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    window, _ = _window(provider_registry, qtbot, tmp_path / "earnings.db", monkeypatch, provider)
    today = _NOW.astimezone(ZoneInfo("America/New_York")).date()
    first = today + timedelta(days=days_until)
    monkeypatch.setattr(provider, "fetch_upcoming_earnings", lambda _symbol: (first, first))
    monkeypatch.setattr(earnings, "datetime", _FixedDateTime)
    _show(qtbot, window, Timeframe.DAILY)
    label = window._app_header.findChild(QLabel, "level1Quote")
    assert label is not None
    expected = f"{first:%b} {first.day}"
    qtbot.waitUntil(lambda: f">{expected}</span>" in label.text())
    assert ("background-color: #ffdc58" in label.text()) is highlighted


@pytest.mark.parametrize("method", [
    "fetch_upcoming_earnings", "fetch_past_earnings", "fetch_dividend_dates",
])
def test_earnings_fetch_failure_keeps_main_window_running(
    provider_registry: ProviderRegistry,
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, method: str,
) -> None:
    def fail(_symbol: str) -> None:
        raise RuntimeError(_SYNTHETIC_FETCH_ERROR)

    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    event_day = date(2026, 10, 22)
    upcoming_day = date(2030, 1, 2)
    monkeypatch.setattr(provider, "fetch_upcoming_earnings", lambda _symbol: (upcoming_day, upcoming_day))
    monkeypatch.setattr(provider, "fetch_past_earnings", lambda _symbol: (event_day,))
    monkeypatch.setattr(provider, "fetch_dividend_dates", lambda _symbol: (event_day,))
    monkeypatch.setattr(provider, method, fail)
    window, _ = _window(provider_registry, qtbot, tmp_path / "earnings-failure.db", monkeypatch, provider)
    history_enabled = method != "fetch_upcoming_earnings"
    window._cache.set_chart_event_visibility(history_enabled, history_enabled)
    window.show()
    label = window._app_header.findChild(QLabel, "level1Quote")
    assert label is not None
    caplog.clear()

    with qtbot.captureExceptions() as escaped:
        try:
            _show(qtbot, window, Timeframe.DAILY)
            qtbot.waitUntil(lambda: bool(escaped or caplog.records), timeout=5_000)
            assert window.isVisible() and not window._close_finalized
            if history_enabled:
                qtbot.waitUntil(lambda: label.text().endswith(">Jan 2</span>"))
        finally:
            window.close()
            qtbot.waitUntil(lambda: window._close_finalized, timeout=5_000)

    assert escaped == []
    assert window._chart.plot_manager.active_marker_keys() == []
    expected = "Jan 2" if history_enabled else "—"
    assert label.text().endswith(f">{expected}</span>")
    _assert_fetch_failure_logged(caplog)


def test_blocked_earnings_history_does_not_delay_new_symbols_upcoming_date(
    provider_registry: ProviderRegistry,
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    old_history = _Gate()
    new_history = _Gate()

    def upcoming(symbol: str) -> tuple[date, date]:
        day = date(2030, 1, 2 if symbol == _SYMBOL else 3)
        return day, day

    def history(symbol: str) -> tuple[date, ...]:
        gate = old_history if symbol == _SYMBOL else new_history
        gate.entered.set()
        gate.release.wait(20)
        return (date(2026, 10, 22),) if symbol == _SYMBOL else ()

    monkeypatch.setattr(provider, "fetch_upcoming_earnings", upcoming)
    monkeypatch.setattr(provider, "fetch_past_earnings", history)
    window, _ = _window(provider_registry, qtbot, tmp_path / "earnings-overlap.db", monkeypatch, provider)
    window._cache.set_chart_event_visibility(True, False)
    label = window._app_header.findChild(QLabel, "level1Quote")
    assert label is not None
    plot = window._chart.plot_manager
    window.show()
    try:
        _show(qtbot, window, Timeframe.DAILY)
        qtbot.waitUntil(old_history.entered.is_set)
        qtbot.waitUntil(lambda: label.text().endswith(">Jan 2</span>"))
        window._on_symbol_changed(_OTHER_SYMBOL)
        _wait_terminal(qtbot, window)
        assert window._loaded_symbol == _OTHER_SYMBOL
        qtbot.waitUntil(lambda: label.text().endswith(">Jan 3</span>"))
        assert not new_history.entered.is_set()
        assert plot.active_marker_keys() == []

        old_history.release.set()
        qtbot.waitUntil(new_history.entered.is_set)
        assert plot.active_marker_keys() == []
    finally:
        old_history.release.set()
        new_history.release.set()
        window.close()
        qtbot.waitUntil(lambda: window._close_finalized)


def test_chart_event_settings_make_no_upcoming_earnings_requests(
    provider_registry: ProviderRegistry,
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    upcoming_calls: list[str] = []
    event_day = date(2026, 10, 22)
    upcoming_day = date(2030, 1, 2)

    def upcoming(symbol: str) -> tuple[date, date]:
        upcoming_calls.append(symbol)
        return upcoming_day, upcoming_day

    def set_options(earnings: bool, dividends: bool) -> None:
        def accept_settings(dialog: ApplicationSettingsDialog) -> int:
            for name, enabled in (("showPastEarnings", earnings), ("showDividends", dividends)):
                checkbox = dialog.findChild(QCheckBox, name)
                assert checkbox is not None
                checkbox.setChecked(enabled)
            dialog.accept()
            return dialog.result()

        monkeypatch.setattr(ApplicationSettingsDialog, "exec", accept_settings)
        window._on_application_settings()

    monkeypatch.setattr(provider, "fetch_upcoming_earnings", upcoming)
    monkeypatch.setattr(provider, "fetch_past_earnings", lambda _symbol: (event_day,))
    monkeypatch.setattr(provider, "fetch_dividend_dates", lambda _symbol: (event_day,))
    window, _ = _window(provider_registry, qtbot, tmp_path / "earnings-settings.db", monkeypatch, provider)
    window.show()
    label = window._app_header.findChild(QLabel, "level1Quote")
    assert label is not None
    plot = window._chart.plot_manager

    def has_marker(text: str) -> bool:
        return [cast(pg.TextItem, plot._markers[key]).toPlainText() for key in plot.active_marker_keys()] == [text]

    try:
        _show(qtbot, window, Timeframe.DAILY)
        qtbot.waitUntil(lambda: label.text().endswith(">Jan 2</span>"))
        assert upcoming_calls == [_SYMBOL]
        set_options(True, False)
        qtbot.waitUntil(lambda: has_marker("E"))
        set_options(False, True)
        qtbot.waitUntil(lambda: has_marker("D"))
        assert label.text().endswith(">Jan 2</span>")
        assert upcoming_calls == [_SYMBOL]
    finally:
        window.close()
        qtbot.waitUntil(lambda: window._close_finalized)


def test_close_dismisses_search_popup_and_waits_for_all_five_jobs(
    provider_registry: ProviderRegistry,
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    gates = {name: _Gate() for name in ("upcoming", "history", "profile", "news", "search")}
    threads: dict[str, QThread] = {}

    def hold(name: str) -> None:
        gate = gates[name]
        thread = QThread.currentThread()
        assert thread is not None
        threads[name] = thread
        gate.entered.set()
        gate.release.wait(20)

    def assert_all_started() -> None:
        assert [name for name, gate in gates.items() if not gate.entered.is_set()] == []

    def upcoming(_symbol: str) -> None:
        hold("upcoming")

    def history(_symbol: str) -> tuple[date, ...]:
        hold("history")
        return ()

    def profile(_symbol: str) -> None:
        hold("profile")

    def news(_symbol: str) -> None:
        hold("news")

    def search(query: str) -> tuple[SymbolMatch, ...]:
        if query == "Held":
            hold("search")
        return (SymbolMatch("TEST", "Test company", "NYSE"),)

    monkeypatch.setattr(provider, "fetch_upcoming_earnings", upcoming)
    monkeypatch.setattr(provider, "fetch_past_earnings", history)
    monkeypatch.setattr(provider, "fetch_profile", profile)
    monkeypatch.setattr(provider, "fetch_news", news)
    monkeypatch.setattr(provider, "search_symbols", search)
    window, _ = _window(provider_registry, qtbot, tmp_path / "all-jobs-close.db", monkeypatch, provider)
    window._cache.set_chart_event_visibility(True, False)
    window.show()
    bar = window._symbol_bar
    field = bar.findChild(QLineEdit, "symbolInput")
    popup = bar._completer.popup()
    assert field is not None and popup is not None
    try:
        _show(qtbot, window, Timeframe.DAILY)
        dialog = _open_symbol_details(window)
        window.activateWindow()
        field.setFocus()
        qtbot.waitUntil(field.hasSelectedText)
        qtbot.keyClicks(field, "Test")  # type: ignore[no-untyped-call]
        qtbot.waitUntil(popup.isVisible)
        # A normal edit clears suggestions. Submit at the search component to keep
        # the displayed popup open while its next real request is in flight.
        bar._search.edit("Held")
        qtbot.waitUntil(assert_all_started)

        assert popup.isVisible() and dialog.isVisible()
        window.close()
        assert not popup.isVisible()
        assert not dialog.isVisible()
        assert not window._close_finalized
        for name in ("upcoming", "profile", "news", "search"):
            with qtbot.waitSignal(threads[name].finished):
                gates[name].release.set()
            qtbot.wait(0)
            assert not window._close_finalized
        gates["history"].release.set()
        qtbot.waitUntil(lambda: window._close_finalized)
    finally:
        for gate in gates.values():
            gate.release.set()
        window.close()
        qtbot.waitUntil(lambda: window._close_finalized)


def test_main_window_has_no_earnings_worker_attributes(
    provider_registry: ProviderRegistry,
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SourceProvider(Timeframe.MIN5, paged=False)
    window, _ = _window(provider_registry, qtbot, tmp_path / "earnings-state.db", monkeypatch, provider)
    try:
        _show(qtbot, window, Timeframe.DAILY)
        assert [name for name in vars(window) if name.startswith("_earnings_")] == []
    finally:
        window.close()
        qtbot.waitUntil(lambda: window._close_finalized)


class _ReferenceProvider(_SourceProvider):
    def __init__(self) -> None:
        super().__init__(Timeframe.MIN5, paged=False)
        self.reference_calls: list[tuple[str, str]] = []

    def search_symbols(self, query: str) -> tuple[SymbolMatch, ...]:
        self.reference_calls.append(("search_symbols", query))
        return (SymbolMatch("XBI", "Reference listing", "NYSE"),)

    def fetch_profile(self, symbol: str) -> SymbolProfile:
        self.reference_calls.append(("fetch_profile", symbol))
        return SymbolProfile(description="Reference profile")

    def fetch_news(self, symbol: str) -> tuple[NewsArticle, ...]:
        self.reference_calls.append(("fetch_news", symbol))
        return (
            NewsArticle(
                "Reference headline", "Publisher", datetime(2026, 8, 14, 1, 30, tzinfo=UTC), None,
            ),
            NewsArticle("Year-one headline", "Archive", datetime(1, 1, 1, tzinfo=UTC), None),
        )

    def fetch_upcoming_earnings(self, symbol: str) -> tuple[date, date]:
        self.reference_calls.append(("fetch_upcoming_earnings", symbol))
        return date(2026, 10, 26), date(2026, 10, 26)

    def fetch_past_earnings(self, symbol: str) -> tuple[date, ...]:
        self.reference_calls.append(("fetch_past_earnings", symbol))
        return (_LAST_SESSION,)

    def fetch_dividend_dates(self, symbol: str) -> tuple[date, ...]:
        self.reference_calls.append(("fetch_dividend_dates", symbol))
        return (_LAST_SESSION,)


_REFERENCE_METHODS = (
    "search_symbols", "fetch_profile", "fetch_news", "fetch_upcoming_earnings",
    "fetch_past_earnings", "fetch_dividend_dates",
)


@pytest.fixture
def reference_routes(
    provider_registry: ProviderRegistry,
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[MainWindow, _SourceProvider, _ReferenceProvider, list[str]]]:
    market = _SourceProvider(Timeframe.MIN5, paged=False)
    reference = _ReferenceProvider()
    unexpected: list[str] = []

    def reject_call(name: str) -> Callable[..., None]:
        def reject(*args: object, **kwargs: object) -> None:
            unexpected.append(name)
            raise AssertionError(f"Unexpected reference call to {name}")

        return reject

    for method in _REFERENCE_METHODS:
        monkeypatch.setattr(market, method, reject_call(f"market.{method}"), raising=False)
    monkeypatch.setattr(yf, "Ticker", reject_call("yfinance.Ticker"))
    monkeypatch.setattr(yf, "Search", reject_call("yfinance.Search"))

    window, _ = _window(
        provider_registry,
        qtbot, tmp_path / "reference-routes.db", monkeypatch, market,
        ALPACA_PAPER_CONNECTION_ID, reference_provider=reference,
    )
    window.show()
    try:
        yield window, market, reference, unexpected
    finally:
        window.close()
        qtbot.waitUntil(lambda: window._close_finalized, timeout=5_000)


def test_main_window_uses_reference_provider_for_all_six_capabilities(
    qtbot: QtBot,
    reference_routes: tuple[MainWindow, _SourceProvider, _ReferenceProvider, list[str]],
) -> None:
    window, market, reference, unexpected = reference_routes
    window._cache.set_chart_event_visibility(True, True)
    _show(qtbot, window, Timeframe.DAILY)

    bar = window._symbol_bar
    field = bar.findChild(QLineEdit, "symbolInput")
    assert field is not None
    field.setFocus()
    qtbot.waitUntil(field.hasSelectedText)
    with qtbot.waitSignal(bar.search_idle, timeout=5_000):
        qtbot.keyClicks(field, "Biotech")
    match = bar._result_model.index(0, 0)
    suggestions = match.data()
    bar.stop_search()

    dialog = _open_symbol_details(window)
    qtbot.waitUntil(lambda: not window._symbol_details.busy and not window._earnings.busy)

    assert sorted(reference.reference_calls) == sorted(
        (method, "Biotech" if method == "search_symbols" else _SYMBOL)
        for method in _REFERENCE_METHODS
    )
    assert unexpected == []
    assert market.calls and reference.calls == []
    assert suggestions == "XBI  ·  Reference listing  ·  NYSE"
    assert "Reference profile" in dialog._profile.toPlainText()
    assert "Reference headline" in dialog._news.toPlainText()
    banner = window._app_header.findChild(QLabel, "level1Quote")
    assert banner is not None and ">Oct 26</span>" in banner.text()
    plot = window._chart.plot_manager
    marker_key, = plot.active_marker_keys()
    assert plot._markers[marker_key].toPlainText() == "E D"


def test_reference_provider_news_renders_utc_as_et_and_omits_year_one_time(
    qtbot: QtBot,
    reference_routes: tuple[MainWindow, _SourceProvider, _ReferenceProvider, list[str]],
) -> None:
    window, _, _, unexpected = reference_routes
    _show(qtbot, window, Timeframe.DAILY)
    dialog = _open_symbol_details(window)
    qtbot.waitUntil(lambda: not window._symbol_details.busy and not window._earnings.busy)

    assert dialog._news.toPlainText().splitlines() == [
        "Reference headline", "Publisher · Aug 13, 2026 · 21:30 ET",
        "Year-one headline", "Archive",
    ]
    assert unexpected == []
