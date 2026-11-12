"""
Wave 3a: a chart shows contiguous cached history older than the provider
still returns (docs/agentic-projects/codebase-review-fixes-2026-11-10.md).

The clock is frozen on Saturday 2026-10-24 10:30 ET. The fake source has a
Yahoo-like 55-day intraday limit, so the fetch start is the next session open
after Sunday 2026-08-30 10:30 ET: Monday 2026-08-31 09:30 ET. The 600-day
lookback floor is Monday 2025-03-03 09:30 EST.
"""

import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any, ClassVar, Self
from zoneinfo import ZoneInfo

import finplot as fplt
import pytest

import app.controller as controller_module
import app.extension_runtime as extension_runtime_module
from app.controller import MainWindow
from app.data_quality import SymbolRefreshDialog
from data.aggregator import Aggregator
from data.cache import Cache
from data.models import Bar, Level1Quote, MarketSnapshot, Timeframe
from data.provider import ProviderAvailability
from data.provider.base import DataProvider
from data.provider.config import (
    ALPACA_PAPER_CONNECTION_ID,
    ProviderConnection,
    YFINANCE_CONNECTION_ID,
)
from data.provider.credentials import (
    CredentialStoreAccess,
    UnavailableCredentialStore,
)
from PyQt6.QtWidgets import QLabel

ET = ZoneInfo("America/New_York")
_SYMBOL = "XBI"
_YAHOO = "yfinance"
_ALPACA = "alpaca:iex"
_NOW = datetime(2026, 10, 24, 10, 30, tzinfo=ET)
_YAHOO_LIMIT = timedelta(days=55)
_FETCH_START = datetime(2026, 8, 31, 9, 30, tzinfo=ET)
_FLOOR = datetime(2025, 3, 3, 9, 30, tzinfo=ET)
_OLD_START = date(2026, 6, 26)
_LAST_SESSION = date(2026, 10, 23)
# Seeded coverage ends at Thursday's close, so each load makes a tail request
# for Friday and the "no request before the limit" checks are not vacuous.
_THURSDAY = date(2026, 10, 22)
_THURSDAY_CLOSE = datetime(2026, 10, 22, 16, 0, tzinfo=ET)

Call = tuple[Timeframe, datetime, datetime]


class _FixedDateTime(datetime):
    frozen: ClassVar[datetime] = datetime.fromtimestamp(0, UTC)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(cls.frozen.timestamp(), tz=tz)


class _SourceProvider(DataProvider):
    """Every regular-session bar of a weekday calendar, single requests."""

    def __init__(self, intraday_limit: timedelta | None) -> None:
        self._intraday_limit = intraday_limit
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
        return _source_bars(timeframe, start, end)

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

    def native_timeframes(self) -> list[Timeframe]:
        return [
            Timeframe.MIN5,
            Timeframe.MIN15,
            Timeframe.MIN30,
            Timeframe.DAILY,
            Timeframe.WEEKLY,
        ]


@pytest.fixture(autouse=True)
def _isolated_finplot_windows() -> Iterator[None]:
    fplt.windows.clear()
    yield
    fplt.windows.clear()


def _weekdays(first: date, last: date) -> Iterator[date]:
    day = first
    while day <= last:
        if day.weekday() < 5:
            yield day
        day += timedelta(days=1)


def _bar(timestamp: datetime, volume: int) -> Bar:
    price = 100.0 + (int(timestamp.timestamp()) // 60) % 997 / 10.0
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
            datetime.combine(day, time(0), tzinfo=ET)
            for day in _weekdays(first, last)
        ]
    elif timeframe == Timeframe.WEEKLY:
        stamps = [
            datetime.combine(day, time(0), tzinfo=ET)
            for day in _weekdays(first, last)
            if day.weekday() == 0
        ]
    else:
        minutes = timeframe.minutes
        assert minutes is not None
        stamps = [
            datetime.combine(day, time(9, 30), tzinfo=ET)
            + timedelta(minutes=offset)
            for day in _weekdays(first, last)
            for offset in range(0, 390, minutes)
        ]
    return [_bar(stamp, 100) for stamp in stamps if start <= stamp < end]


def _session_bars(first: date, last: date) -> list[Bar]:
    return _source_bars(
        Timeframe.MIN5,
        datetime.combine(first, time(0), tzinfo=ET),
        datetime.combine(last + timedelta(days=1), time(0), tzinfo=ET),
    )


def _ms(timestamp: datetime) -> int:
    return int(timestamp.timestamp() * 1000)


def _seed(
    db_path: Path,
    bars: list[Bar],
    coverage_start: datetime,
    coverage_end: datetime = _NOW,
    namespace: str = _YAHOO,
    synthesized: bool = False,
) -> None:
    """5m bars and their coverage; optionally the 39m bars built from them."""
    with Cache(str(db_path)) as cache:
        cache.put_bars(namespace, _SYMBOL, Timeframe.MIN5, bars)
        cache.replace_bar_fetch_coverage(
            namespace,
            _SYMBOL,
            Timeframe.MIN5,
            _ms(coverage_start),
            _ms(coverage_end),
        )
        if synthesized:
            aggregator = Aggregator(_SourceProvider(None))
            cache.put_bars(
                namespace,
                _SYMBOL,
                Timeframe.MIN39,
                aggregator.synthesize_cached_bars(bars, Timeframe.MIN39),
            )
            cache.replace_bar_fetch_coverage(
                namespace,
                _SYMBOL,
                Timeframe.MIN39,
                _ms(coverage_start),
                _ms(coverage_end),
            )


def _window(
    qtbot: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: _SourceProvider,
    active_connection_id: str = YFINANCE_CONNECTION_ID,
) -> MainWindow:
    with Cache(str(db_path)) as cache:
        cache.set_active_provider_connection_id(active_connection_id)
    monkeypatch.setattr(
        controller_module,
        "initialize_keyring_credential_store",
        lambda: CredentialStoreAccess(
            UnavailableCredentialStore("test"), False, "test",
        ),
    )
    monkeypatch.setattr(
        controller_module,
        "_provider_availability_by_name",
        lambda connections, _access: {
            connection.provider_name: ProviderAvailability(True, None)
            for connection in connections
        },
    )

    def create_provider(
        _connection: ProviderConnection,
        _store: object,
    ) -> DataProvider:
        return provider

    monkeypatch.setattr(controller_module, "create_provider", create_provider)
    monkeypatch.setattr(_FixedDateTime, "frozen", _NOW)
    monkeypatch.setattr(controller_module, "datetime", _FixedDateTime)
    monkeypatch.setattr(extension_runtime_module, "datetime", _FixedDateTime)
    monkeypatch.setattr(controller_module, "INITIAL_EXTENSIONS", [])
    monkeypatch.setattr(
        controller_module,
        "show_warning",
        lambda _parent, title, message: pytest.fail(f"{title}: {message}"),
    )
    production_load = MainWindow._load
    monkeypatch.setattr(MainWindow, "_load", lambda window, **_kwargs: None)
    monkeypatch.setattr(MainWindow, "_refresh_watchlist_snapshots", lambda window: None)
    monkeypatch.setattr(MainWindow, "_refresh_level1", lambda window: None)
    window = MainWindow(str(db_path))
    qtbot.addWidget(window)
    monkeypatch.setattr(MainWindow, "_load", production_load)
    return window


def _show(qtbot: Any, window: MainWindow, timeframe: Timeframe) -> list[Bar]:
    window._state.symbol = _SYMBOL
    window._state.timeframe = timeframe
    window._load()
    request = window._current_chart_request
    assert request is not None
    qtbot.waitUntil(
        lambda: window._terminal_chart_generation == request.generation
        and window._pending_chart_load is None
        and not window._chart_load_tasks,
        timeout=30_000,
    )
    series = window._current_series
    assert series is not None
    return series.bars


def _intraday_requests(provider: _SourceProvider) -> list[Call]:
    return [call for call in provider.calls if call[0].is_intraday]


def _requests_start_at_or_after(provider: _SourceProvider, start: datetime) -> bool:
    return all(call[1] >= start for call in _intraday_requests(provider))


def _at(day: date, hour: int = 9, minute: int = 30) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=ET)


def test_yahoo_5m_shows_contiguous_cached_history_beyond_the_provider_limit(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(
        db_path,
        _session_bars(_OLD_START, _THURSDAY),
        _at(_OLD_START),
        coverage_end=_THURSDAY_CLOSE,
    )
    provider = _SourceProvider(_YAHOO_LIMIT)
    window = _window(qtbot, db_path, monkeypatch, provider)

    bars = _show(qtbot, window, Timeframe.MIN5)

    assert bars[0].timestamp == _at(_OLD_START)
    assert [bar.timestamp for bar in bars] == [
        bar.timestamp for bar in _session_bars(_OLD_START, _LAST_SESSION)
    ]
    assert _intraday_requests(provider)
    assert _requests_start_at_or_after(provider, _FETCH_START)


def test_redraw_after_a_config_change_keeps_the_displayed_history(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path, _session_bars(_OLD_START, _LAST_SESSION), _at(_OLD_START))
    window = _window(qtbot, db_path, monkeypatch, _SourceProvider(_YAHOO_LIMIT))
    shown = _show(qtbot, window, Timeframe.MIN5)
    assert shown[0].timestamp == _at(_OLD_START)

    window._reload_extensions()

    series = window._current_series
    assert series is not None
    assert len(series.bars) == len(shown)
    assert series.bars[0].timestamp == shown[0].timestamp


def test_stale_coverage_starts_at_the_fetch_start(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    stale_end = date(2026, 8, 14)
    _seed(
        db_path,
        _session_bars(_OLD_START, stale_end),
        _at(_OLD_START),
        coverage_end=_at(stale_end, 16, 0),
    )
    provider = _SourceProvider(_YAHOO_LIMIT)
    window = _window(qtbot, db_path, monkeypatch, provider)

    bars = _show(qtbot, window, Timeframe.MIN5)

    assert bars[0].timestamp == _FETCH_START
    assert _requests_start_at_or_after(provider, _FETCH_START)


def test_cached_bars_before_a_gap_are_not_shown(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    after_gap = date(2026, 8, 10)
    with Cache(str(db_path)) as cache:
        cache.put_bars(
            _YAHOO, _SYMBOL, Timeframe.MIN5, _session_bars(_OLD_START, date(2026, 7, 31)),
        )
    _seed(db_path, _session_bars(after_gap, _LAST_SESSION), _at(after_gap))
    provider = _SourceProvider(_YAHOO_LIMIT)
    window = _window(qtbot, db_path, monkeypatch, provider)

    bars = _show(qtbot, window, Timeframe.MIN5)

    assert bars[0].timestamp == _at(after_gap)
    assert _requests_start_at_or_after(provider, _FETCH_START)


def test_displayed_history_is_floored_at_the_lookback_cutoff(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    before_floor = date(2025, 2, 28)
    seeded = (
        _session_bars(before_floor, before_floor)
        + _session_bars(_FLOOR.date(), _FLOOR.date())
        + _session_bars(_OLD_START, _LAST_SESSION)
    )
    _seed(db_path, seeded, _at(before_floor))
    window = _window(qtbot, db_path, monkeypatch, _SourceProvider(_YAHOO_LIMIT))

    bars = _show(qtbot, window, Timeframe.MIN5)

    assert bars[0].timestamp == _FLOOR


def test_synthesized_39m_shows_contiguous_cached_history(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(
        db_path,
        _session_bars(_OLD_START, _THURSDAY),
        _at(_OLD_START),
        coverage_end=_THURSDAY_CLOSE,
        synthesized=True,
    )
    provider = _SourceProvider(_YAHOO_LIMIT)
    window = _window(qtbot, db_path, monkeypatch, provider)

    bars = _show(qtbot, window, Timeframe.MIN39)

    assert bars[0].timestamp == _at(_OLD_START)
    assert bars[-1].timestamp.astimezone(ET).date() == _LAST_SESSION
    assert _intraday_requests(provider)
    assert _requests_start_at_or_after(provider, _FETCH_START)


def test_alpaca_route_is_unchanged(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    before_floor = date(2025, 2, 28)
    seeded = _session_bars(before_floor, before_floor) + _session_bars(
        _OLD_START, _LAST_SESSION,
    )
    _seed(db_path, seeded, _at(before_floor), namespace=_ALPACA)
    provider = _SourceProvider(None)
    window = _window(
        qtbot, db_path, monkeypatch, provider, ALPACA_PAPER_CONNECTION_ID,
    )

    bars = _show(qtbot, window, Timeframe.MIN5)

    # The 600-day window starts at the floor; nothing before it is shown or
    # requested, and the cache already covers it.
    assert bars[0].timestamp == _at(_OLD_START)
    assert _requests_start_at_or_after(provider, _FLOOR)


@pytest.mark.parametrize(
    ("active", "namespace", "limit", "hidden"),
    [
        pytest.param(YFINANCE_CONNECTION_ID, _YAHOO, _YAHOO_LIMIT, True, id="yahoo"),
        pytest.param(ALPACA_PAPER_CONNECTION_ID, _ALPACA, None, False, id="alpaca"),
    ],
)
def test_refresh_confirmation_says_when_older_cached_history_will_be_hidden(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active: str,
    namespace: str,
    limit: timedelta | None,
    hidden: bool,
) -> None:
    db_path = tmp_path / "test.db"
    window = _window(qtbot, db_path, monkeypatch, _SourceProvider(limit), active)

    plan = window.plan_symbol_refresh(namespace, _SYMBOL)
    assert plan is not None
    dialog = SymbolRefreshDialog(namespace, _SYMBOL, plan)
    qtbot.addWidget(dialog)
    message = dialog.findChild(QLabel, "symbolRefreshMessage")
    assert message is not None

    note = "Cached intraday data older than that will no longer be shown"
    assert (note in message.text()) is hidden


@pytest.mark.parametrize("target", [Timeframe.MIN39, Timeframe.MIN65], ids=["39m", "65m"])
def test_synthesized_history_is_built_from_older_cached_base_bars(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: Timeframe,
) -> None:
    db_path = tmp_path / "test.db"
    # Only the 5m history reaches back; the target was never viewed.
    _seed(
        db_path,
        _session_bars(_OLD_START, _THURSDAY),
        _at(_OLD_START),
        coverage_end=_THURSDAY_CLOSE,
    )
    provider = _SourceProvider(_YAHOO_LIMIT)
    window = _window(qtbot, db_path, monkeypatch, provider)
    expected = Aggregator(_SourceProvider(None)).synthesize_cached_bars(
        _session_bars(_OLD_START, _LAST_SESSION), target,
    )

    first = _show(qtbot, window, target)
    second = _show(qtbot, window, target)

    for bars in (first, second):
        assert bars[0].timestamp == _at(_OLD_START)
        assert bars == expected
    assert _intraday_requests(provider)
    assert _requests_start_at_or_after(provider, _FETCH_START)
