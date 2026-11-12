"""
Wave 3: keeping cached history on the provider's adjustment basis
(docs/agentic-projects/codebase-review-fixes-2026-11-10.md, "Wave 3").

The clock is frozen on Saturday 2026-10-24 10:30 ET, as in
test_cached_history_display.py. The fake Yahoo source has a 55-day intraday
limit, so the 5m fetch start is Monday 2026-08-31 09:30 ET. The cache was
filled before Thursday 2026-10-22, the ex-date: its bars are on the old
basis and its coverage ends at Wednesday's close.
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
from chart.window import ChartWidget
from app.application_settings import ApplicationSettingsDialog
from app.dialogs import BasisChangeDialog
from data.adjustment import BasisChange, BasisChangeChoice, detect_basis_change
from data.aggregator import Aggregator
from data.cache import Cache
from data.models import Bar, BarCorrection, Level1Quote, MarketSnapshot, OHLCVSeries, Timeframe
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
from PyQt6.QtWidgets import QCheckBox, QComboBox, QPushButton, QWidget

ET = ZoneInfo("America/New_York")
_SYMBOL = "XBI"
_YAHOO = "yfinance"
_ALPACA = "alpaca:iex"
_NOW = datetime(2026, 10, 24, 10, 30, tzinfo=ET)
_YAHOO_LIMIT = timedelta(days=55)
_FETCH_START = datetime(2026, 8, 31, 9, 30, tzinfo=ET)
_ALPACA_FETCH_START = datetime(2025, 3, 3, 9, 30, tzinfo=ET)
_OLD_START = date(2026, 6, 26)
_EX_DATE = date(2026, 10, 22)
_WEDNESDAY = date(2026, 10, 21)
_WEDNESDAY_CLOSE = datetime(2026, 10, 21, 16, 0, tzinfo=ET)
_LAST_SESSION = date(2026, 10, 23)
_DAILY_START = date(2026, 1, 2)

Call = tuple[Timeframe, datetime, datetime]


# ------------------------------------------------------------------
# Detection (pure)
# ------------------------------------------------------------------


def _price_bar(index: int, price: float, volume: int = 1000) -> Bar:
    return Bar(
        datetime(2026, 10, 20, 13, 30, tzinfo=UTC) + timedelta(minutes=5 * index),
        price,
        price * 1.01,
        price * 0.99,
        price * 1.002,
        volume,
    )


def _scaled(bar: Bar, ratio: float, volume_factor: float = 1.0) -> Bar:
    return Bar(
        bar.timestamp,
        bar.open * ratio,
        bar.high * ratio,
        bar.low * ratio,
        bar.close * ratio,
        round(bar.volume * volume_factor),
        None if bar.vwap is None else bar.vwap * ratio,
    )


def _cents(bar: Bar) -> Bar:
    return Bar(
        bar.timestamp,
        round(bar.open, 2),
        round(bar.high, 2),
        round(bar.low, 2),
        round(bar.close, 2),
        bar.volume,
        bar.vwap,
    )


_CACHED = [_price_bar(index, 120.0 + index * 0.37) for index in range(20)]


def test_unchanged_bars_are_not_a_basis_change() -> None:
    assert detect_basis_change(_CACHED, list(_CACHED)) is None


def test_a_two_for_one_split_is_detected_with_its_volume_factor() -> None:
    fetched = [_scaled(bar, 0.5, 2.0) for bar in _CACHED]

    change = detect_basis_change(_CACHED, fetched)

    assert change == BasisChange(0.5, 2.0, None)


def test_a_dividend_adjustment_changes_prices_only() -> None:
    fetched = [_scaled(bar, 0.99) for bar in _CACHED]

    change = detect_basis_change(_CACHED, fetched)

    assert change is not None
    assert change.ratio == pytest.approx(0.99)
    assert change.volume_factor == 1.0
    assert change.new_basis_from_ms is None


def test_a_single_bar_revision_is_not_a_basis_change() -> None:
    fetched = list(_CACHED)
    revised = fetched[7]
    fetched[7] = Bar(
        revised.timestamp,
        revised.open,
        revised.high,
        revised.low,
        revised.close * 0.97,
        revised.volume,
    )

    assert detect_basis_change(_CACHED, fetched) is None


def test_inconsistent_differences_are_revisions() -> None:
    fetched = [
        _scaled(bar, 0.99 if index % 2 else 0.98)
        for index, bar in enumerate(_CACHED)
    ]

    assert detect_basis_change(_CACHED, fetched) is None


@pytest.mark.parametrize("ratio", [100.0, 0.01])
def test_an_implausible_block_ratio_is_rejected(ratio: float) -> None:
    fetched = [_scaled(bar, ratio) for bar in _CACHED]

    assert detect_basis_change(_CACHED, fetched) is None


def test_a_provider_lag_step_is_detected_from_the_older_bars() -> None:
    # The newest cached bars were written after the split took effect; the
    # provider has since adjusted the older ones.
    fetched = [_scaled(bar, 0.5, 2.0) for bar in _CACHED[:14]] + _CACHED[14:]

    change = detect_basis_change(_CACHED, fetched)

    assert change == BasisChange(
        0.5,
        2.0,
        int(_CACHED[14].timestamp.timestamp() * 1000),
    )


def test_newer_bars_changing_after_unchanged_older_ones_are_revisions() -> None:
    fetched = _CACHED[:14] + [_scaled(bar, 0.5, 2.0) for bar in _CACHED[14:]]

    assert detect_basis_change(_CACHED, fetched) is None


def test_one_changed_bar_is_not_enough_evidence() -> None:
    fetched = [_scaled(_CACHED[0], 0.5, 2.0)]

    assert detect_basis_change(_CACHED[:1], fetched) is None


def test_cent_rounding_on_a_low_priced_stock_stays_inside_the_tolerance() -> None:
    # Adjusted prices are rounded to the cent; on a $10 stock that is up to
    # ~1e-3 of relative noise per price, well above a fixed 1e-4 spread.
    cached = [_cents(_price_bar(index, 10.0 + index * 0.013)) for index in range(20)]
    fetched = [_cents(_scaled(bar, 0.9917)) for bar in cached]

    change = detect_basis_change(cached, fetched)

    assert change is not None
    assert change.ratio == pytest.approx(0.9917, rel=1e-3)
    assert change.volume_factor == 1.0


def test_cent_rounding_noise_alone_is_not_a_basis_change() -> None:
    cached = [_cents(_price_bar(index, 10.0 + index * 0.013)) for index in range(20)]
    fetched = [
        Bar(bar.timestamp, bar.open + 0.01, bar.high, bar.low, bar.close - 0.01, bar.volume)
        for bar in cached
    ]

    assert detect_basis_change(cached, fetched) is None


def test_bars_are_paired_by_timestamp() -> None:
    fetched = [_scaled(bar, 0.5, 2.0) for bar in _CACHED[5:]]

    assert detect_basis_change(_CACHED, fetched) == BasisChange(0.5, 2.0, None)


# ------------------------------------------------------------------
# Production path
# ------------------------------------------------------------------


class _FixedDateTime(datetime):
    frozen: ClassVar[datetime] = datetime.fromtimestamp(0, UTC)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(cls.frozen.timestamp(), tz=tz)


class _AdjustedSource(DataProvider):
    """
    Every regular-session bar of a weekday calendar. Bars dated before the
    ex-date are returned on the adjusted basis: prices times ratio, volumes
    times volume_factor, for the timeframes in adjusted.
    """

    def __init__(
        self,
        intraday_limit: timedelta | None,
        ratio: float = 1.0,
        volume_factor: float = 1.0,
        adjusted: frozenset[Timeframe] = frozenset(Timeframe),
        page_span: timedelta | None = None,
    ) -> None:
        self._intraday_limit = intraday_limit
        self._page_span = page_span
        self._ratio = ratio
        self._volume_factor = volume_factor
        self._adjusted = adjusted
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
        bars = _source_bars(timeframe, start, end)
        if timeframe not in self._adjusted:
            return bars
        ex_date = datetime.combine(_EX_DATE, time(0), tzinfo=ET)
        return [
            _scaled(bar, self._ratio, self._volume_factor)
            if bar.timestamp < ex_date
            else bar
            for bar in bars
        ]

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


def _bar(timestamp: datetime) -> Bar:
    price = 100.0 + (int(timestamp.timestamp()) // 60) % 997 / 10.0
    return Bar(
        timestamp.astimezone(UTC),
        price,
        price + 1.0,
        price - 1.0,
        price + 0.5,
        1000 + (int(timestamp.timestamp()) // 300) % 37,
        price + 0.25,
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
    return [_bar(stamp) for stamp in stamps if start <= stamp < end]


def _bars(timeframe: Timeframe, first: date, last: date) -> list[Bar]:
    return _source_bars(
        timeframe,
        datetime.combine(first, time(0), tzinfo=ET),
        datetime.combine(last + timedelta(days=1), time(0), tzinfo=ET),
    )


def _ms(timestamp: datetime) -> int:
    return int(timestamp.timestamp() * 1000)


def _at(day: date, hour: int = 9, minute: int = 30) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=ET)


def _seed(
    db_path: Path,
    timeframe: Timeframe,
    bars: list[Bar],
    coverage_start: datetime,
    coverage_end: datetime,
    namespace: str = _YAHOO,
) -> None:
    with Cache(str(db_path)) as cache:
        cache.put_bars(namespace, _SYMBOL, timeframe, bars)
        cache.replace_bar_fetch_coverage(
            namespace,
            _SYMBOL,
            timeframe,
            _ms(coverage_start),
            _ms(coverage_end),
        )


def _seed_old_basis_5m(db_path: Path, namespace: str = _YAHOO) -> list[Bar]:
    """Old-basis 5m and 39m history from 06-26 through Wednesday's close."""
    bars = _bars(Timeframe.MIN5, _OLD_START, _WEDNESDAY)
    _seed(db_path, Timeframe.MIN5, bars, _at(_OLD_START), _WEDNESDAY_CLOSE, namespace)
    synthesized = Aggregator(_AdjustedSource(None)).synthesize_cached_bars(
        bars,
        Timeframe.MIN39,
    )
    _seed(
        db_path,
        Timeframe.MIN39,
        synthesized,
        _at(_OLD_START),
        _WEDNESDAY_CLOSE,
        namespace,
    )
    return bars


class _Prompts:
    """Stands in for the basis-change question; records each message."""

    def __init__(self, answer: tuple[BasisChangeChoice, bool] | None = None) -> None:
        self.answer = answer
        self.messages: list[str] = []

    def __call__(
        self,
        _parent: QWidget | None,
        message: str,
    ) -> tuple[BasisChangeChoice, bool] | None:
        self.messages.append(message)
        return self.answer


def _window(
    qtbot: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: _AdjustedSource,
    active_connection_id: str = YFINANCE_CONNECTION_ID,
    prompts: _Prompts | None = None,
) -> MainWindow:
    monkeypatch.setattr(
        controller_module,
        "ask_basis_change_choice",
        _Prompts() if prompts is None else prompts,
    )
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
    # Older history loads on its own when a short test chart's view is near
    # its oldest bar; tests/test_older_history.py covers that path.
    monkeypatch.setattr(ChartWidget, "older_history_bars_wanted", lambda _chart: 0)
    window = MainWindow(str(db_path))
    qtbot.addWidget(window)
    monkeypatch.setattr(MainWindow, "_load", production_load)
    return window


def _show(qtbot: Any, window: MainWindow, timeframe: Timeframe) -> list[Bar]:
    window._state.symbol = _SYMBOL
    window._state.timeframe = timeframe
    window._load()
    return _settled(qtbot, window)


def _settled(qtbot: Any, window: MainWindow) -> list[Bar]:
    """The displayed bars once no chart load is pending or running."""

    def idle() -> bool:
        request = window._current_chart_request
        return (
            request is not None
            and window._terminal_chart_generation == request.generation
            and window._pending_chart_load is None
            and not window._chart_load_tasks
        )

    qtbot.waitUntil(idle, timeout=30_000)
    series = window._current_series
    assert series is not None
    return series.bars


def _adjusted(bars: list[Bar], ratio: float, volume_factor: float = 1.0) -> list[Bar]:
    ex_date = datetime.combine(_EX_DATE, time(0), tzinfo=ET)
    return [
        _scaled(bar, ratio, volume_factor) if bar.timestamp < ex_date else bar
        for bar in bars
    ]


def _cached(
    db_path: Path,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    namespace: str = _YAHOO,
) -> list[Bar]:
    with Cache(str(db_path)) as cache:
        return cache.get_raw_bars(namespace, _SYMBOL, timeframe, _ms(start), _ms(end))


def test_yahoo_5m_split_redownloads_the_provider_window_and_records_older_history(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    old_bars = _seed_old_basis_5m(db_path)
    window_bar = _at(date(2026, 9, 15), 10, 0)
    older_bar = _at(date(2026, 7, 15), 10, 0)
    corrected_low = {
        bar.timestamp: bar.low - 0.25
        for bar in old_bars
        if bar.timestamp in (window_bar, older_bar)
    }
    with Cache(str(db_path)) as cache:
        for stamp, low in corrected_low.items():
            cache.put_bar_correction(
                BarCorrection(_YAHOO, _SYMBOL, Timeframe.MIN5, stamp, low=low, volume=777)
            )
    provider = _AdjustedSource(_YAHOO_LIMIT, ratio=0.5, volume_factor=2.0)
    window = _window(qtbot, db_path, monkeypatch, provider)

    bars = _show(qtbot, window, Timeframe.MIN5)

    # The provider window was fetched again from its start, on the new basis.
    assert any(
        call[0] == Timeframe.MIN5 and call[1] <= _FETCH_START
        for call in provider.calls
    )
    expected = _adjusted(_bars(Timeframe.MIN5, date(2026, 8, 31), _LAST_SESSION), 0.5, 2.0)
    assert [bar.timestamp for bar in bars] == [bar.timestamp for bar in expected]
    assert _cached(db_path, Timeframe.MIN5, _FETCH_START, _NOW) == expected
    # Older cached history is untouched, on the old basis, and hidden.
    assert _cached(db_path, Timeframe.MIN5, _at(_OLD_START), _FETCH_START - timedelta(minutes=1)) == [
        bar for bar in old_bars if bar.timestamp < _FETCH_START
    ]
    with Cache(str(db_path)) as cache:
        assert cache.get_bar_fetch_coverage(_YAHOO, _SYMBOL, Timeframe.MIN5) == (
            _ms(_FETCH_START),
            _ms(_NOW),
        )
        window_inspection = cache.get_bar_inspection(_YAHOO, _SYMBOL, Timeframe.MIN5, window_bar)
        older_inspection = cache.get_bar_inspection(_YAHOO, _SYMBOL, Timeframe.MIN5, older_bar)
        records = cache.get_basis_changes(_YAHOO, _SYMBOL)
    # Corrections inside the re-downloaded window move to the new basis.
    assert window_inspection is not None and window_inspection.correction is not None
    assert window_inspection.correction.low == pytest.approx(corrected_low[window_bar] * 0.5)
    assert window_inspection.correction.volume == 1554
    assert older_inspection is not None and older_inspection.correction is not None
    assert older_inspection.correction.low == corrected_low[older_bar]
    assert older_inspection.correction.volume == 777
    assert [
        (record.timeframe, record.old_coverage_start_ms, record.boundary_ms, record.ratio, record.volume_factor)
        for record in records
    ] == [(Timeframe.MIN5, _ms(_at(_OLD_START)), _ms(_FETCH_START), 0.5, 2.0)]


def test_after_a_yahoo_split_39m_starts_at_the_fetch_start_until_rescaled(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # W3a-RQC-1: the dependent 39m history is reset with its base, and the
    # rebuild from older cached 5m bars does not restore old-basis 39m bars.
    db_path = tmp_path / "test.db"
    _seed_old_basis_5m(db_path)
    with Cache(str(db_path)) as cache:
        old_39m = cache.get_raw_bars(_YAHOO, _SYMBOL, Timeframe.MIN39, 0, _ms(_FETCH_START) - 1)
    provider = _AdjustedSource(_YAHOO_LIMIT, ratio=0.5, volume_factor=2.0)
    window = _window(qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN5)

    bars = _show(qtbot, window, Timeframe.MIN39)

    expected = Aggregator(provider).synthesize_cached_bars(
        _adjusted(_bars(Timeframe.MIN5, date(2026, 8, 31), _LAST_SESSION), 0.5, 2.0),
        Timeframe.MIN39,
    )
    assert bars == expected
    with Cache(str(db_path)) as cache:
        assert cache.get_raw_bars(_YAHOO, _SYMBOL, Timeframe.MIN39, 0, _ms(_FETCH_START) - 1) == old_39m


def test_yahoo_intraday_is_unchanged_by_a_dividend(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    old_bars = _seed_old_basis_5m(db_path)
    # Yahoo adjusts only daily and weekly history for dividends.
    provider = _AdjustedSource(
        _YAHOO_LIMIT,
        ratio=0.99,
        adjusted=frozenset({Timeframe.DAILY, Timeframe.WEEKLY}),
    )
    window = _window(qtbot, db_path, monkeypatch, provider)

    bars = _show(qtbot, window, Timeframe.MIN5)

    assert bars[0].timestamp == _at(_OLD_START)
    # Only the tail and its validation window (the last two cached
    # sessions) were requested.
    assert all(
        call[1] >= _at(date(2026, 10, 20)) for call in provider.calls if call[0] == Timeframe.MIN5
    )
    assert _cached(db_path, Timeframe.MIN5, _at(_OLD_START), _WEDNESDAY_CLOSE) == old_bars
    with Cache(str(db_path)) as cache:
        assert cache.get_basis_changes(_YAHOO, _SYMBOL) == []


def test_daily_dividend_redownloads_daily_prices_and_resets_weekly(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    daily_start = datetime.combine(_DAILY_START, time(0), tzinfo=ET)
    _seed(
        db_path,
        Timeframe.DAILY,
        _bars(Timeframe.DAILY, _DAILY_START, _WEDNESDAY),
        daily_start,
        datetime.combine(_EX_DATE, time(0), tzinfo=ET),
    )
    weekly_bars = _bars(Timeframe.WEEKLY, _DAILY_START, _WEDNESDAY)
    _seed(
        db_path,
        Timeframe.WEEKLY,
        weekly_bars,
        daily_start,
        datetime.combine(date(2026, 10, 19), time(0), tzinfo=ET),
    )
    provider = _AdjustedSource(None, ratio=0.99)
    window = _window(qtbot, db_path, monkeypatch, provider)
    monkeypatch.setattr(controller_module, "_DAILY_HISTORY_START", daily_start.astimezone(UTC))

    daily = _show(qtbot, window, Timeframe.DAILY)

    expected = _adjusted(_bars(Timeframe.DAILY, _DAILY_START, _LAST_SESSION), 0.99)
    assert daily == expected
    assert [bar.volume for bar in daily] == [
        bar.volume for bar in _bars(Timeframe.DAILY, _DAILY_START, _LAST_SESSION)
    ]
    with Cache(str(db_path)) as cache:
        assert cache.get_basis_changes(_YAHOO, _SYMBOL) == []
        assert cache.get_bar_fetch_coverage(_YAHOO, _SYMBOL, Timeframe.WEEKLY) is None
        assert cache.get_raw_bars(_YAHOO, _SYMBOL, Timeframe.WEEKLY, 0, _ms(_NOW)) == []

    provider.calls.clear()
    weekly = _show(qtbot, window, Timeframe.WEEKLY)

    assert [call[0] for call in provider.calls] == [Timeframe.WEEKLY]
    assert weekly == _adjusted(_bars(Timeframe.WEEKLY, _DAILY_START, _LAST_SESSION), 0.99)


def test_alpaca_split_redownloads_without_a_pending_record(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Alpaca returns the whole 600-day window, so nothing older stays shown.
    db_path = tmp_path / "test.db"
    bars = _bars(Timeframe.MIN5, date(2026, 10, 5), _WEDNESDAY)
    _seed(db_path, Timeframe.MIN5, bars, _ALPACA_FETCH_START, _WEDNESDAY_CLOSE, _ALPACA)
    provider = _AdjustedSource(None, ratio=0.5, volume_factor=2.0)
    prompts = _Prompts()
    window = _window(
        qtbot, db_path, monkeypatch, provider, ALPACA_PAPER_CONNECTION_ID, prompts,
    )

    shown = _show(qtbot, window, Timeframe.MIN5)

    assert any(
        call[0] == Timeframe.MIN5 and call[1] <= _ALPACA_FETCH_START
        for call in provider.calls
    )
    assert shown[0].timestamp == _ALPACA_FETCH_START
    assert shown[-1] == _adjusted(_bars(Timeframe.MIN5, _LAST_SESSION, _LAST_SESSION), 0.5, 2.0)[-1]
    with Cache(str(db_path)) as cache:
        assert cache.get_basis_changes(_ALPACA, _SYMBOL) == []
    assert prompts.messages == []


# ------------------------------------------------------------------
# Rescale or discard older history (W3-6)
# ------------------------------------------------------------------


def _split_load(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prompts: _Prompts,
    choice: BasisChangeChoice | None = None,
) -> tuple[Path, MainWindow, list[Bar], dict[datetime, float]]:
    """
    A Yahoo 5m load that detects a 2-for-1 split, with an older and a newer
    corrected bar. Returns the old-basis 5m bars and the corrected lows.
    """
    db_path = tmp_path / "test.db"
    old_bars = _seed_old_basis_5m(db_path)
    corrected_low = {
        bar.timestamp: bar.low - 0.25
        for bar in old_bars
        if bar.timestamp in (_at(date(2026, 9, 15), 10), _at(date(2026, 7, 15), 10))
    }
    with Cache(str(db_path)) as cache:
        for stamp, low in corrected_low.items():
            cache.put_bar_correction(
                BarCorrection(_YAHOO, _SYMBOL, Timeframe.MIN5, stamp, low=low, volume=777)
            )
        if choice is not None:
            cache.set_basis_change_choice(choice)
    provider = _AdjustedSource(_YAHOO_LIMIT, ratio=0.5, volume_factor=2.0)
    window = _window(qtbot, db_path, monkeypatch, provider, prompts=prompts)
    _show(qtbot, window, Timeframe.MIN5)
    return db_path, window, old_bars, corrected_low


def test_rescale_restores_older_history_on_the_new_basis(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _Prompts((BasisChangeChoice.RESCALE, False))
    db_path, window, old_bars, corrected_low = _split_load(
        qtbot, tmp_path, monkeypatch, prompts,
    )
    older_bar = _at(date(2026, 7, 15), 10)

    bars = _settled(qtbot, window)

    assert len(prompts.messages) == 1
    assert "XBI" in prompts.messages[0] and "2-for-1" in prompts.messages[0]
    # The chart reloads with the older history rescaled onto the new basis.
    all_5m = _adjusted(_bars(Timeframe.MIN5, _OLD_START, _LAST_SESSION), 0.5, 2.0)
    assert [bar.timestamp for bar in bars] == [bar.timestamp for bar in all_5m]
    assert _cached(db_path, Timeframe.MIN5, _at(_OLD_START), _NOW) == all_5m
    with Cache(str(db_path)) as cache:
        assert cache.get_bar_fetch_coverage(_YAHOO, _SYMBOL, Timeframe.MIN5) == (
            _ms(_at(_OLD_START)),
            _ms(_NOW),
        )
        assert cache.get_raw_bars(
            _YAHOO, _SYMBOL, Timeframe.MIN39, 0, _ms(_FETCH_START) - 1,
        ) == [
            _scaled(bar, 0.5, 2.0)
            for bar in Aggregator(_AdjustedSource(None)).synthesize_cached_bars(
                [bar for bar in old_bars if bar.timestamp < _FETCH_START],
                Timeframe.MIN39,
            )
        ]
        inspection = cache.get_bar_inspection(_YAHOO, _SYMBOL, Timeframe.MIN5, older_bar)
        assert cache.get_basis_changes(_YAHOO, _SYMBOL) == []
        assert cache.get_basis_change_choice() is None
    assert inspection is not None and inspection.correction is not None
    assert inspection.correction.low == corrected_low[older_bar] * 0.5
    assert inspection.correction.volume == 1554
    assert isinstance(inspection.raw_bar.volume, int)

    bars_39m = _show(qtbot, window, Timeframe.MIN39)

    assert bars_39m == Aggregator(_AdjustedSource(None)).synthesize_cached_bars(
        all_5m,
        Timeframe.MIN39,
    )


def test_discard_deletes_the_older_old_basis_history(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _Prompts((BasisChangeChoice.DISCARD, False))
    db_path, window, _old_bars, corrected_low = _split_load(
        qtbot, tmp_path, monkeypatch, prompts,
    )

    bars = _settled(qtbot, window)

    assert len(prompts.messages) == 1
    assert bars[0].timestamp == _FETCH_START
    with Cache(str(db_path)) as cache:
        for timeframe in (Timeframe.MIN5, Timeframe.MIN39):
            assert cache.get_raw_bars(
                _YAHOO, _SYMBOL, timeframe, 0, _ms(_FETCH_START) - 1,
            ) == []
        assert cache.get_bar_fetch_coverage(_YAHOO, _SYMBOL, Timeframe.MIN5) == (
            _ms(_FETCH_START),
            _ms(_NOW),
        )
        assert cache.get_basis_changes(_YAHOO, _SYMBOL) == []
        newer = cache.get_bar_inspection(
            _YAHOO, _SYMBOL, Timeframe.MIN5, _at(date(2026, 9, 15), 10),
        )
        corrections_before_boundary = cache._conn.execute(
            "SELECT COUNT(*) FROM bar_corrections WHERE timestamp < ?",
            (_ms(_FETCH_START),),
        ).fetchone()[0]
    assert corrections_before_boundary == 0
    assert newer is not None and newer.correction is not None
    assert newer.correction.low == pytest.approx(
        corrected_low[_at(date(2026, 9, 15), 10)] * 0.5
    )


def test_a_remembered_choice_is_applied_without_a_prompt(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _Prompts()
    _db_path, window, _old_bars, _lows = _split_load(
        qtbot, tmp_path, monkeypatch, prompts, BasisChangeChoice.RESCALE,
    )

    bars = _settled(qtbot, window)

    assert prompts.messages == []
    assert bars[0].timestamp == _at(_OLD_START)


def test_remember_my_choice_stores_it(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _Prompts((BasisChangeChoice.DISCARD, True))
    db_path, window, _old_bars, _lows = _split_load(qtbot, tmp_path, monkeypatch, prompts)
    _settled(qtbot, window)

    with Cache(str(db_path)) as cache:
        assert cache.get_basis_change_choice() == BasisChangeChoice.DISCARD


def test_a_dismissed_prompt_keeps_the_record_and_is_not_repeated(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = _Prompts(None)
    db_path, window, _old_bars, _lows = _split_load(qtbot, tmp_path, monkeypatch, prompts)

    _show(qtbot, window, Timeframe.MIN15)
    bars = _show(qtbot, window, Timeframe.MIN5)

    assert len(prompts.messages) == 1
    assert bars[0].timestamp == _FETCH_START
    with Cache(str(db_path)) as cache:
        assert len(cache.get_basis_changes(_YAHOO, _SYMBOL)) == 1


def test_a_pending_record_prompts_on_load(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A record left from an earlier session (dismissed, or the app closed)
    # prompts on the next load of the symbol, with no new detection.
    db_path = tmp_path / "test.db"
    _seed_old_basis_5m(db_path)
    with Cache(str(db_path)) as cache:
        cache.begin_basis_change(
            _YAHOO,
            _SYMBOL,
            Timeframe.MIN5,
            [Timeframe.MIN39, Timeframe.MIN65],
            BasisChange(0.5, 2.0, None),
            _ms(_FETCH_START),
            _ms(_at(date(2025, 3, 3))),
        )
        cache.put_bars(
            _YAHOO,
            _SYMBOL,
            Timeframe.MIN5,
            _adjusted(_bars(Timeframe.MIN5, date(2026, 8, 31), _LAST_SESSION), 0.5, 2.0),
        )
        cache.replace_bar_fetch_coverage(
            _YAHOO, _SYMBOL, Timeframe.MIN5, _ms(_FETCH_START), _ms(_NOW),
        )
    prompts = _Prompts((BasisChangeChoice.RESCALE, False))
    provider = _AdjustedSource(_YAHOO_LIMIT, ratio=0.5, volume_factor=2.0)
    window = _window(qtbot, db_path, monkeypatch, provider, prompts=prompts)

    _show(qtbot, window, Timeframe.MIN5)
    bars = _settled(qtbot, window)

    assert len(prompts.messages) == 1
    assert bars == _adjusted(_bars(Timeframe.MIN5, _OLD_START, _LAST_SESSION), 0.5, 2.0)


def _seed_two_day_history(cache: Cache) -> list[Bar]:
    bars = _bars(Timeframe.MIN5, date(2026, 10, 19), date(2026, 10, 21))
    cache.put_bars(_YAHOO, _SYMBOL, Timeframe.MIN5, bars)
    cache.replace_bar_fetch_coverage(
        _YAHOO, _SYMBOL, Timeframe.MIN5, _ms(_at(date(2026, 10, 19))), _ms(_WEDNESDAY_CLOSE),
    )
    return bars


def test_rescale_is_dropped_when_coverage_no_longer_starts_at_the_boundary(
    tmp_path: Path,
) -> None:
    # V2-3: something else (a later reset or refresh) moved the coverage.
    boundary = _at(date(2026, 10, 20))
    with Cache(str(tmp_path / "test.db")) as cache:
        bars = _seed_two_day_history(cache)
        cache.begin_basis_change(
            _YAHOO, _SYMBOL, Timeframe.MIN5, [], BasisChange(0.5, 2.0, None),
            _ms(boundary), 0,
        )
        cache.replace_bar_fetch_coverage(
            _YAHOO, _SYMBOL, Timeframe.MIN5, _ms(_at(date(2026, 10, 21))), _ms(_NOW),
        )

        cache.rescale_basis_changes(_YAHOO, _SYMBOL, Timeframe.MIN5, [])

        assert cache.get_basis_changes(_YAHOO, _SYMBOL) == []
        assert cache.get_raw_bars(_YAHOO, _SYMBOL, Timeframe.MIN5, 0, _ms(boundary) - 1) == [
            bar for bar in bars if bar.timestamp < boundary
        ]
        assert cache.get_bar_fetch_coverage(_YAHOO, _SYMBOL, Timeframe.MIN5) == (
            _ms(_at(date(2026, 10, 21))),
            _ms(_NOW),
        )


def test_stacked_basis_changes_multiply_ratios(tmp_path: Path) -> None:
    first_boundary = _at(date(2026, 10, 20))
    second_boundary = _at(date(2026, 10, 21))
    with Cache(str(tmp_path / "test.db")) as cache:
        bars = _seed_two_day_history(cache)
        cache.begin_basis_change(
            _YAHOO, _SYMBOL, Timeframe.MIN5, [], BasisChange(0.5, 2.0, None),
            _ms(first_boundary), 0,
        )
        # The re-download from the first boundary, on the first new basis.
        redownloaded = [
            _scaled(bar, 0.5, 2.0) for bar in bars if bar.timestamp >= first_boundary
        ]
        cache.put_bars(_YAHOO, _SYMBOL, Timeframe.MIN5, redownloaded)
        cache.replace_bar_fetch_coverage(
            _YAHOO, _SYMBOL, Timeframe.MIN5, _ms(first_boundary), _ms(_WEDNESDAY_CLOSE),
        )
        # A dividend before the user answered.
        cache.begin_basis_change(
            _YAHOO, _SYMBOL, Timeframe.MIN5, [], BasisChange(0.98, 1.0, None),
            _ms(second_boundary), 0,
        )
        records = cache.get_basis_changes(_YAHOO, _SYMBOL)
        cache.replace_bar_fetch_coverage(
            _YAHOO, _SYMBOL, Timeframe.MIN5, _ms(second_boundary), _ms(_NOW),
        )

        cache.rescale_basis_changes(_YAHOO, _SYMBOL, Timeframe.MIN5, [])

        rescaled = cache.get_raw_bars(
            _YAHOO, _SYMBOL, Timeframe.MIN5, 0, _ms(second_boundary) - 1,
        )
        coverage = cache.get_bar_fetch_coverage(_YAHOO, _SYMBOL, Timeframe.MIN5)
    assert [(record.boundary_ms, record.ratio, record.volume_factor) for record in records] == [
        (_ms(first_boundary), 0.5 * 0.98, 2.0),
        (_ms(second_boundary), 0.98, 1.0),
    ]
    assert rescaled == [
        _scaled(bar, 0.5 * 0.98, 2.0) for bar in bars if bar.timestamp < first_boundary
    ] + [_scaled(bar, 0.98) for bar in redownloaded if bar.timestamp < second_boundary]
    assert coverage == (_ms(_at(date(2026, 10, 19))), _ms(_NOW))


def test_settings_change_the_remembered_choice(qtbot: Any, tmp_path: Path) -> None:
    store = UnavailableCredentialStore("test")
    available = {
        "yfinance": ProviderAvailability(True, None),
        "alpaca": ProviderAvailability(True, None),
    }
    with Cache(str(tmp_path / "test.db")) as cache:
        dialog = ApplicationSettingsDialog(cache, store, available)
        qtbot.addWidget(dialog)
        combo = dialog.findChild(QComboBox, "basisChangeChoice")
        assert combo is not None
        assert combo.currentData() is None
        combo.setCurrentIndex(combo.findData(BasisChangeChoice.DISCARD.value))
        dialog.accept()
        assert cache.get_basis_change_choice() == BasisChangeChoice.DISCARD

        reopened = ApplicationSettingsDialog(cache, store, available)
        qtbot.addWidget(reopened)
        combo = reopened.findChild(QComboBox, "basisChangeChoice")
        assert combo is not None
        assert combo.currentData() == BasisChangeChoice.DISCARD.value
        combo.setCurrentIndex(0)
        reopened.reject()
        assert cache.get_basis_change_choice() == BasisChangeChoice.DISCARD


def test_the_question_returns_the_clicked_choice_and_remember_flag(qtbot: Any) -> None:
    dialog = BasisChangeDialog("XBI's price history was adjusted.")
    qtbot.addWidget(dialog)
    remember = dialog.findChild(QCheckBox, "basisChangeRemember")
    discard = dialog.findChild(QPushButton, "basisChangeDiscard")
    assert remember is not None and discard is not None
    remember.setChecked(True)

    discard.click()

    assert dialog.answer() == (BasisChangeChoice.DISCARD, True)

    dismissed = BasisChangeDialog("XBI's price history was adjusted.")
    qtbot.addWidget(dismissed)
    dismissed.reject()
    assert dismissed.answer() is None


# ------------------------------------------------------------------
# Wave 3 code QC regressions
# ------------------------------------------------------------------


def test_a_39m_first_load_after_a_yahoo_split_shows_only_new_basis_bars(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # W3-QC-1: the synthesized load detects the split through its base.
    db_path = tmp_path / "test.db"
    _seed_old_basis_5m(db_path)
    provider = _AdjustedSource(_YAHOO_LIMIT, ratio=0.5, volume_factor=2.0)
    window = _window(qtbot, db_path, monkeypatch, provider)

    bars = _show(qtbot, window, Timeframe.MIN39)

    assert bars == Aggregator(provider).synthesize_cached_bars(
        _adjusted(_bars(Timeframe.MIN5, date(2026, 8, 31), _LAST_SESSION), 0.5, 2.0),
        Timeframe.MIN39,
    )
    with Cache(str(db_path)) as cache:
        assert cache.get_bar_fetch_coverage(_YAHOO, _SYMBOL, Timeframe.MIN5) == (
            _ms(_FETCH_START),
            _ms(_NOW),
        )
        assert [record.boundary_ms for record in cache.get_basis_changes(_YAHOO, _SYMBOL)] == [
            _ms(_FETCH_START)
        ]


def test_a_39m_first_load_after_an_alpaca_split_rebuilds_the_whole_window(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    bars_5m = _bars(Timeframe.MIN5, date(2026, 10, 5), _WEDNESDAY)
    _seed(db_path, Timeframe.MIN5, bars_5m, _ALPACA_FETCH_START, _WEDNESDAY_CLOSE, _ALPACA)
    _seed(
        db_path,
        Timeframe.MIN39,
        Aggregator(_AdjustedSource(None)).synthesize_cached_bars(bars_5m, Timeframe.MIN39),
        _ALPACA_FETCH_START,
        _WEDNESDAY_CLOSE,
        _ALPACA,
    )
    provider = _AdjustedSource(None, ratio=0.5, volume_factor=2.0)
    window = _window(qtbot, db_path, monkeypatch, provider, ALPACA_PAPER_CONNECTION_ID)

    bars = _show(qtbot, window, Timeframe.MIN39)

    assert bars == Aggregator(provider).synthesize_cached_bars(
        _adjusted(_bars(Timeframe.MIN5, _ALPACA_FETCH_START.date(), _LAST_SESSION), 0.5, 2.0),
        Timeframe.MIN39,
    )


def test_a_record_waits_until_its_timeframe_is_downloaded_again(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # W3-QC-2: the reset was interrupted before the 5m re-download. Another
    # timeframe's load must not resolve (and so drop) the 5m record.
    db_path = tmp_path / "test.db"
    _seed_old_basis_5m(db_path)
    with Cache(str(db_path)) as cache:
        cache.begin_basis_change(
            _YAHOO,
            _SYMBOL,
            Timeframe.MIN5,
            [Timeframe.MIN39, Timeframe.MIN65],
            BasisChange(0.5, 2.0, None),
            _ms(_FETCH_START),
            _ms(_at(date(2025, 3, 3))),
        )
    prompts = _Prompts((BasisChangeChoice.RESCALE, False))
    provider = _AdjustedSource(_YAHOO_LIMIT, ratio=0.5, volume_factor=2.0)
    window = _window(qtbot, db_path, monkeypatch, provider, prompts=prompts)

    _show(qtbot, window, Timeframe.MIN15)

    assert prompts.messages == []
    with Cache(str(db_path)) as cache:
        assert len(cache.get_basis_changes(_YAHOO, _SYMBOL)) == 1

    _show(qtbot, window, Timeframe.MIN5)
    bars = _settled(qtbot, window)

    assert len(prompts.messages) == 1
    assert bars[0].timestamp == _at(_OLD_START)


def test_one_split_prompts_once_across_timeframes(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # W3-QC-3: each native timeframe detects the split on its own load; the
    # answer applies to the whole event.
    db_path = tmp_path / "test.db"
    _seed_old_basis_5m(db_path)
    for timeframe in (Timeframe.MIN15, Timeframe.MIN30):
        _seed(
            db_path,
            timeframe,
            _bars(timeframe, _OLD_START, _WEDNESDAY),
            _at(_OLD_START),
            _WEDNESDAY_CLOSE,
        )
    provider = _AdjustedSource(_YAHOO_LIMIT, ratio=0.5, volume_factor=2.0)
    prompts = _Prompts((BasisChangeChoice.RESCALE, False))
    window = _window(qtbot, db_path, monkeypatch, provider, prompts=prompts)

    for timeframe in (Timeframe.MIN5, Timeframe.MIN15, Timeframe.MIN30):
        _show(qtbot, window, timeframe)
        bars = _settled(qtbot, window)
        assert bars[0].timestamp == _at(_OLD_START)

    assert len(prompts.messages) == 1
    with Cache(str(db_path)) as cache:
        assert cache.get_basis_changes(_YAHOO, _SYMBOL) == []


def test_a_provider_lag_step_is_reset_from_the_older_bars(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Wednesday was cached after the provider served it on the new basis;
    # the older bars were adjusted later.
    db_path = tmp_path / "test.db"
    wednesday = _at(_WEDNESDAY)
    seeded = [
        bar if bar.timestamp < wednesday else _scaled(bar, 0.5, 2.0)
        for bar in _bars(Timeframe.MIN5, _OLD_START, _WEDNESDAY)
    ]
    # The provider's ex-date is Thursday; this cache saw the new basis from
    # Wednesday, as if the split took effect a day earlier for it.
    _seed(db_path, Timeframe.MIN5, seeded, _at(_OLD_START), _WEDNESDAY_CLOSE)
    tuesday_bar = _at(date(2026, 10, 20), 10)
    wednesday_bar = _at(_WEDNESDAY, 10)
    lows = {
        bar.timestamp: bar.low - 0.25
        for bar in seeded
        if bar.timestamp in (tuesday_bar, wednesday_bar)
    }
    with Cache(str(db_path)) as cache:
        for stamp, low in lows.items():
            cache.put_bar_correction(
                BarCorrection(_YAHOO, _SYMBOL, Timeframe.MIN5, stamp, low=low)
            )
    provider = _AdjustedSource(_YAHOO_LIMIT, ratio=0.5, volume_factor=2.0)
    window = _window(qtbot, db_path, monkeypatch, provider)

    bars = _show(qtbot, window, Timeframe.MIN5)

    assert bars[0].timestamp == _FETCH_START
    with Cache(str(db_path)) as cache:
        records = cache.get_basis_changes(_YAHOO, _SYMBOL)
        tuesday = cache.get_bar_inspection(_YAHOO, _SYMBOL, Timeframe.MIN5, tuesday_bar)
        wednesday_inspection = cache.get_bar_inspection(
            _YAHOO, _SYMBOL, Timeframe.MIN5, wednesday_bar,
        )
    assert [(record.ratio, record.volume_factor) for record in records] == [(0.5, 2.0)]
    assert tuesday is not None and tuesday.correction is not None
    assert tuesday.correction.low == lows[tuesday_bar] * 0.5
    assert wednesday_inspection is not None and wednesday_inspection.correction is not None
    assert wednesday_inspection.correction.low == lows[wednesday_bar]


def test_the_tail_request_never_starts_before_the_provider_window(tmp_path: Path) -> None:
    # W3-QC-4: validation bars older than the provider window start are not
    # requested; the provider may refuse the whole range.
    now = datetime(2026, 10, 22, 12, 0, tzinfo=ET)
    provider = _AdjustedSource(_YAHOO_LIMIT)
    aggregator = Aggregator(provider)
    window_start = controller_module._history_start(Timeframe.MIN5, aggregator, now)
    assert window_start == _FETCH_START
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            _YAHOO, _SYMBOL, Timeframe.MIN5, _bars(Timeframe.MIN5, date(2026, 8, 27), date(2026, 8, 28)),
        )
        cache.replace_bar_fetch_coverage(
            _YAHOO, _SYMBOL, Timeframe.MIN5, _ms(_at(date(2026, 8, 27))), _ms(_FETCH_START),
        )

        controller_module._fetch_and_cache_bars(
            aggregator,
            cache,
            _YAHOO,
            _SYMBOL,
            Timeframe.MIN5,
            window_start,
            now,
        )

    assert [call[1] for call in provider.calls] == [_FETCH_START]


@pytest.mark.parametrize("timeframe", [Timeframe.MIN39, Timeframe.MIN65])
def test_a_weekday_synthesized_first_load_detects_a_split(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeframe: Timeframe,
) -> None:
    # W3-RQC-1: a synthesized load fetches its base from a late start (its
    # own refresh start); the validation bars must still be requested.
    db_path = tmp_path / "test.db"
    old_bars = _seed_old_basis_5m(db_path)
    _seed(
        db_path,
        Timeframe.MIN65,
        Aggregator(_AdjustedSource(None)).synthesize_cached_bars(old_bars, Timeframe.MIN65),
        _at(_OLD_START),
        _WEDNESDAY_CLOSE,
    )
    provider = _AdjustedSource(_YAHOO_LIMIT, ratio=0.5, volume_factor=2.0)
    window = _window(qtbot, db_path, monkeypatch, provider)
    monkeypatch.setattr(_FixedDateTime, "frozen", datetime(2026, 10, 23, 12, 0, tzinfo=ET))

    _show(qtbot, window, timeframe)
    bars = _show(qtbot, window, Timeframe.MIN5)

    assert bars[0].timestamp == _FETCH_START
    with Cache(str(db_path)) as cache:
        assert len(cache.get_basis_changes(_YAHOO, _SYMBOL)) == 1


def test_a_provider_lag_step_is_detected_by_a_39m_load(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    wednesday = _at(_WEDNESDAY)
    seeded = [
        bar if bar.timestamp < wednesday else _scaled(bar, 0.5, 2.0)
        for bar in _bars(Timeframe.MIN5, _OLD_START, _WEDNESDAY)
    ]
    _seed(db_path, Timeframe.MIN5, seeded, _at(_OLD_START), _WEDNESDAY_CLOSE)
    _seed(
        db_path,
        Timeframe.MIN39,
        Aggregator(_AdjustedSource(None)).synthesize_cached_bars(seeded, Timeframe.MIN39),
        _at(_OLD_START),
        _WEDNESDAY_CLOSE,
    )
    provider = _AdjustedSource(_YAHOO_LIMIT, ratio=0.5, volume_factor=2.0)
    window = _window(qtbot, db_path, monkeypatch, provider)

    _show(qtbot, window, Timeframe.MIN39)

    with Cache(str(db_path)) as cache:
        assert len(cache.get_basis_changes(_YAHOO, _SYMBOL)) == 1


def test_a_resolve_during_the_open_question_does_not_ask_again(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # W3-RQC-2: the modal question runs a nested event loop, where another
    # completed load of the symbol can resolve again.
    db_path = tmp_path / "test.db"
    _seed_old_basis_5m(db_path)
    provider = _AdjustedSource(_YAHOO_LIMIT, ratio=0.5, volume_factor=2.0)
    window = _window(qtbot, db_path, monkeypatch, provider)
    messages: list[str] = []

    def ask(
        _parent: QWidget | None,
        message: str,
    ) -> tuple[BasisChangeChoice, bool] | None:
        messages.append(message)
        if len(messages) == 1:
            window._resolve_basis_changes(window._route_for_symbol(_SYMBOL), _SYMBOL)
        return (BasisChangeChoice.RESCALE, False)

    monkeypatch.setattr(controller_module, "ask_basis_change_choice", ask)

    _show(qtbot, window, Timeframe.MIN5)
    bars = _settled(qtbot, window)

    assert len(messages) == 1
    assert bars[0].timestamp == _at(_OLD_START)


# ------------------------------------------------------------------
# History wave H-S5: a reset on a paged source renders recent bars first
# ------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", [Timeframe.MIN5, Timeframe.MIN30, Timeframe.MIN39])
def test_a_reset_on_a_paged_source_renders_the_recent_window_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeframe: Timeframe,
) -> None:
    # The user saw ~10 s on Alpaca 30m: the reset re-downloaded the whole
    # 600-day window before the chart rendered.
    db_path = tmp_path / "test.db"
    native = Timeframe.MIN5 if timeframe == Timeframe.MIN39 else timeframe
    _seed(
        db_path,
        native,
        _bars(native, date(2026, 10, 5), _WEDNESDAY),
        _ALPACA_FETCH_START,
        _WEDNESDAY_CLOSE,
        _ALPACA,
    )
    provider = _AdjustedSource(
        None, ratio=0.5, volume_factor=2.0, page_span=timedelta(days=30),
    )
    order: list[str] = []
    original_fetch = provider.fetch_bars

    def fetch_bars(symbol: str, tf: Timeframe, start: datetime, end: datetime) -> list[Bar]:
        if tf == native:
            order.append(f"request {start.isoformat()}")
        return original_fetch(symbol, tf, start, end)

    monkeypatch.setattr(provider, "fetch_bars", fetch_bars)
    monkeypatch.setattr(controller_module, "create_provider", lambda _c, _s: provider)
    monkeypatch.setattr(_FixedDateTime, "frozen", _NOW)
    monkeypatch.setattr(controller_module, "datetime", _FixedDateTime)
    with Cache(str(db_path)) as cache:
        connection = cache.get_provider_connection(ALPACA_PAPER_CONNECTION_ID)
    assert connection is not None
    request = controller_module._ChartLoadRequest(1, _SYMBOL, timeframe, connection)
    worker = controller_module._FetchWorker(str(db_path), None, request)  # type: ignore[arg-type]
    recent: list[OHLCVSeries] = []
    finished: list[OHLCVSeries] = []
    errors: list[str] = []
    def on_recent(_request: object, series: OHLCVSeries) -> None:
        order.append("recent_ready")
        recent.append(series)

    worker.recent_ready.connect(on_recent)
    worker.finished.connect(lambda _request, series: finished.append(series))
    worker.error.connect(lambda _request, message: errors.append(message))

    worker.run()

    assert errors == []
    assert len(recent) == 1 and len(finished) == 1
    end = controller_module._history_end(timeframe, Aggregator(provider), _NOW)
    recent_start = controller_module._recent_history_start(
        timeframe,
        controller_module._history_start(timeframe, Aggregator(provider), end),
        end,
    )
    before_recent = order[: order.index("recent_ready")]
    assert all(
        datetime.fromisoformat(entry.removeprefix("request ")) >= recent_start
        for entry in before_recent
        if entry.startswith("request ")
    )
    assert finished[0].bars[0].timestamp == _ALPACA_FETCH_START
    expected_last = _adjusted(_bars(native, _LAST_SESSION, _LAST_SESSION), 0.5, 2.0)
    if timeframe == Timeframe.MIN39:
        expected_last = Aggregator(provider).synthesize_cached_bars(expected_last, timeframe)
    assert finished[0].bars[-1] == expected_last[-1]


# ------------------------------------------------------------------
# History wave H-S2: unlimited sources re-download, never record (D1)
# ------------------------------------------------------------------


def test_an_unlimited_source_drops_all_old_basis_history_after_a_split(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # History older than the initial window (reached by panning) is still
    # returned by the provider, so it is re-downloaded on demand, not kept.
    db_path = tmp_path / "test.db"
    deep_start = date(2025, 1, 2)
    bars = _bars(Timeframe.MIN5, deep_start, date(2025, 3, 31)) + _bars(
        Timeframe.MIN5, date(2026, 10, 5), _WEDNESDAY,
    )
    _seed(db_path, Timeframe.MIN5, bars, _at(deep_start), _WEDNESDAY_CLOSE, _ALPACA)
    provider = _AdjustedSource(
        None, ratio=0.5, volume_factor=2.0, page_span=timedelta(days=30),
    )
    prompts = _Prompts()
    window = _window(
        qtbot, db_path, monkeypatch, provider, ALPACA_PAPER_CONNECTION_ID, prompts,
    )

    shown = _show(qtbot, window, Timeframe.MIN5)

    assert shown[0].timestamp == _ALPACA_FETCH_START
    assert _cached(
        db_path, Timeframe.MIN5, _at(deep_start), _ALPACA_FETCH_START - timedelta(minutes=1), _ALPACA,
    ) == []
    with Cache(str(db_path)) as cache:
        assert cache.get_basis_changes(_ALPACA, _SYMBOL) == []
    assert prompts.messages == []


def test_a_daily_reset_redownloads_from_the_oldest_cached_day(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Daily is one request, so its whole cached depth (including days before
    # the initial window) is downloaded again on the new basis.
    db_path = tmp_path / "test.db"
    deep_start = date(2025, 6, 2)
    daily_start = datetime.combine(_DAILY_START, time(0), tzinfo=ET)
    _seed(
        db_path,
        Timeframe.DAILY,
        _bars(Timeframe.DAILY, deep_start, _WEDNESDAY),
        datetime.combine(deep_start, time(0), tzinfo=ET),
        datetime.combine(_EX_DATE, time(0), tzinfo=ET),
    )
    provider = _AdjustedSource(None, ratio=0.99)
    window = _window(qtbot, db_path, monkeypatch, provider)
    monkeypatch.setattr(controller_module, "_DAILY_HISTORY_START", daily_start.astimezone(UTC))

    _show(qtbot, window, Timeframe.DAILY)

    assert _cached(
        db_path, Timeframe.DAILY, datetime.combine(deep_start, time(0), tzinfo=ET), _NOW,
    ) == _adjusted(_bars(Timeframe.DAILY, deep_start, _LAST_SESSION), 0.99)
    with Cache(str(db_path)) as cache:
        coverage = cache.get_bar_fetch_coverage(_YAHOO, _SYMBOL, Timeframe.DAILY)
    assert coverage is not None
    assert coverage[0] == _ms(datetime.combine(deep_start, time(0), tzinfo=ET))
