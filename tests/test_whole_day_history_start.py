"""
Whole-day history start (amendment A1, criterion 6c): every intraday history
window starts at an ET 09:30 session open, so old and new data never meet
inside a 39m/65m period.
"""

import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, ClassVar, Self, cast
from zoneinfo import ZoneInfo

import pytest

import app.controller as controller_module
from app.controller import (
    _ChartLoadRequest,
    _FetchWorker,
    _history_end,
    _history_start,
)
from data.aggregator import Aggregator
from data.cache import Cache
from data.models import Bar, Level1Quote, MarketSnapshot, OHLCVSeries, Timeframe
from data.provider.alpaca_provider import AlpacaProvider
from data.provider.base import DataProvider
from data.provider.config import MarketDataFeed, alpaca_paper_connection
from data.provider.yfinance_provider import YFinanceProvider

ET = ZoneInfo("America/New_York")
_DAILY_HISTORY_START = datetime(2016, 1, 1, tzinfo=UTC)

Row = tuple[datetime, float, float, float, float, int]


def _et(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=ET)


def _end_for_cutoff(cutoff: datetime, lookback: timedelta) -> datetime:
    """The UTC endpoint whose lookback lands exactly on cutoff."""
    return cutoff.astimezone(UTC) + lookback


class _YahooWithoutFiveMinute(YFinanceProvider):
    """Yahoo limits with 1m as the only intraday base for 39m and 65m."""

    def native_timeframes(self) -> list[Timeframe]:
        return [
            timeframe
            for timeframe in super().native_timeframes()
            if timeframe != Timeframe.MIN5
        ]


def _alpaca(feed: MarketDataFeed) -> Aggregator:
    return Aggregator(AlpacaProvider("key", "secret", feed, cast(Any, object())))


# ----------------------------------------------------------------------
# Exact values
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cutoff", "expected"),
    [
        pytest.param(_et(2026, 8, 26, 9, 29), _et(2026, 8, 26, 9, 30), id="just-before-open"),
        pytest.param(_et(2026, 8, 26, 9, 30), _et(2026, 8, 26, 9, 30), id="at-open"),
        pytest.param(_et(2026, 8, 26, 9, 30, 1), _et(2026, 8, 27, 9, 30), id="just-after-open"),
        pytest.param(_et(2026, 8, 25, 20, 0), _et(2026, 8, 26, 9, 30), id="weekday-evening"),
        pytest.param(_et(2026, 8, 28, 11, 0), _et(2026, 8, 31, 9, 30), id="friday-session"),
        pytest.param(_et(2026, 8, 29, 12, 0), _et(2026, 8, 31, 9, 30), id="saturday"),
        pytest.param(_et(2026, 8, 30, 23, 59), _et(2026, 8, 31, 9, 30), id="sunday-night"),
        # 09:30 EST is 14:30 UTC; 09:30 EDT is 13:30 UTC.
        pytest.param(_et(2026, 3, 6, 10, 0), _et(2026, 3, 9, 9, 30), id="into-daylight-time"),
        pytest.param(_et(2026, 3, 8, 3, 0), _et(2026, 3, 9, 9, 30), id="spring-forward-day"),
        pytest.param(_et(2026, 10, 30, 10, 0), _et(2026, 11, 2, 9, 30), id="into-standard-time"),
        pytest.param(_et(2026, 11, 1, 1, 30), _et(2026, 11, 2, 9, 30), id="fall-back-day"),
        # No holiday calendar: a weekday holiday is a session start; its first
        # bars arrive on the next session, as before.
        pytest.param(_et(2026, 11, 26, 8, 0), _et(2026, 11, 26, 9, 30), id="holiday-morning"),
        pytest.param(_et(2026, 11, 25, 16, 30), _et(2026, 11, 26, 9, 30), id="eve-of-holiday"),
    ],
)
@pytest.mark.parametrize(
    "timeframe",
    [Timeframe.MIN5, Timeframe.MIN15, Timeframe.MIN30, Timeframe.MIN39, Timeframe.MIN65],
)
def test_yahoo_history_start_is_the_first_session_open_at_or_after_the_55_day_limit(
    cutoff: datetime,
    expected: datetime,
    timeframe: Timeframe,
) -> None:
    aggregator = Aggregator(YFinanceProvider())
    end = _end_for_cutoff(cutoff, timedelta(days=55))

    start = _history_start(timeframe, aggregator, end)

    assert start == expected
    assert start.astimezone(ET).time() == time(9, 30)
    assert start >= cast(datetime, aggregator.earliest_history_start(timeframe, end))


def test_dst_session_opens_are_built_in_eastern_local_time() -> None:
    aggregator = Aggregator(YFinanceProvider())

    spring = _history_start(
        Timeframe.MIN5,
        aggregator,
        _end_for_cutoff(_et(2026, 3, 6, 10, 0), timedelta(days=55)),
    )
    fall = _history_start(
        Timeframe.MIN5,
        aggregator,
        _end_for_cutoff(_et(2026, 10, 30, 10, 0), timedelta(days=55)),
    )

    assert spring == datetime(2026, 3, 9, 13, 30, tzinfo=UTC)
    assert fall == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("provider", "timeframe"),
    [
        pytest.param(YFinanceProvider(), Timeframe.MIN1, id="1m-chart"),
        pytest.param(_YahooWithoutFiveMinute(), Timeframe.MIN39, id="39m-from-1m"),
        pytest.param(_YahooWithoutFiveMinute(), Timeframe.MIN65, id="65m-from-1m"),
    ],
)
def test_yahoo_one_minute_base_uses_the_6_day_limit(
    provider: DataProvider,
    timeframe: Timeframe,
) -> None:
    aggregator = Aggregator(provider)
    end = _end_for_cutoff(_et(2026, 10, 20, 12, 0), timedelta(days=6))

    assert _history_start(timeframe, aggregator, end) == _et(2026, 10, 21, 9, 30)


def test_yahoo_39m_from_five_minute_base_uses_the_55_day_limit() -> None:
    aggregator = Aggregator(YFinanceProvider())
    end = _end_for_cutoff(_et(2026, 10, 20, 12, 0), timedelta(days=6))

    # 55 days before Monday 2026-10-26 12:00 is Tuesday 2026-09-01 12:00.
    assert _history_start(Timeframe.MIN39, aggregator, end) == _et(2026, 9, 2, 9, 30)


@pytest.mark.parametrize(
    ("feed", "expected"),
    [
        # The provider-safe endpoint is 16 minutes earlier, moving the
        # 600-day cutoff from 09:40 to 09:24: the same session is kept.
        pytest.param(MarketDataFeed.DELAYED_SIP, _et(2025, 3, 4, 9, 30), id="delayed-sip"),
        pytest.param(MarketDataFeed.SIP, _et(2025, 3, 5, 9, 30), id="sip"),
        pytest.param(MarketDataFeed.IEX, _et(2025, 3, 5, 9, 30), id="iex"),
    ],
)
@pytest.mark.parametrize("timeframe", [Timeframe.MIN5, Timeframe.MIN39])
def test_alpaca_history_start_uses_600_days_from_the_provider_safe_endpoint(
    feed: MarketDataFeed,
    expected: datetime,
    timeframe: Timeframe,
) -> None:
    aggregator = _alpaca(feed)
    now = _end_for_cutoff(_et(2025, 3, 4, 9, 40), timedelta(days=600))

    start = _history_start(timeframe, aggregator, _history_end(timeframe, aggregator, now))

    assert start == expected


@pytest.mark.parametrize("timeframe", [Timeframe.DAILY, Timeframe.WEEKLY])
@pytest.mark.parametrize(
    "aggregator",
    [
        pytest.param(Aggregator(YFinanceProvider()), id="yahoo"),
        pytest.param(_alpaca(MarketDataFeed.IEX), id="alpaca"),
    ],
)
def test_daily_and_weekly_keep_the_fixed_2016_start(
    aggregator: Aggregator,
    timeframe: Timeframe,
) -> None:
    end = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)

    assert _history_start(timeframe, aggregator, end) == _DAILY_HISTORY_START


# ----------------------------------------------------------------------
# Through the worker: the terminal cache starts at the open
# ----------------------------------------------------------------------


class _FixedDateTime(datetime):
    frozen: ClassVar[datetime] = datetime.fromtimestamp(0, UTC)

    @classmethod
    def now(cls, tz: Any = None) -> Self:
        return cls.fromtimestamp(cls.frozen.timestamp(), tz=tz)


def _weekdays(first: date, last: date) -> Iterator[date]:
    day = first
    while day <= last:
        if day.weekday() < 5:
            yield day
        day += timedelta(days=1)


def _source_bar(timestamp: datetime, volume: int) -> Bar:
    price = 100.0 + (int(timestamp.timestamp()) // 60) % 997 / 10.0
    return Bar(timestamp.astimezone(UTC), price, price + 1.0, price - 1.0, price + 0.5, volume)


def _session_bars(timeframe: Timeframe, start: datetime, end: datetime) -> list[Bar]:
    first = start.astimezone(ET).date() - timedelta(days=7)
    last = end.astimezone(ET).date()
    if timeframe == Timeframe.DAILY:
        stamps = [(datetime.combine(day, time(0), tzinfo=ET), 390) for day in _weekdays(first, last)]
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
            (datetime.combine(day, time(9, 30), tzinfo=ET) + timedelta(minutes=offset), minutes)
            for day in _weekdays(first, last)
            for offset in range(0, 390, minutes)
        ]
    return [_source_bar(stamp, volume) for stamp, volume in stamps if start <= stamp < end]


class _Source(DataProvider):
    """Every regular-session bar of a weekday calendar; volume is the bar's minutes."""

    def __init__(self, limit: timedelta | None, page_span: timedelta | None) -> None:
        self._limit = limit
        self._page_span = page_span
        self.calls: list[tuple[Timeframe, datetime, datetime]] = []
        self._lock = threading.Lock()

    def fetch_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime,
    ) -> list[Bar]:
        with self._lock:
            self.calls.append((timeframe, start, end))
        return _session_bars(timeframe, start, end)

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        return {}

    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        return None

    def earliest_history_start(self, timeframe: Timeframe, end: datetime) -> datetime | None:
        if not timeframe.is_intraday or self._limit is None:
            return None
        return end - self._limit

    def history_page_span(self, timeframe: Timeframe) -> timedelta | None:
        return self._page_span if timeframe.is_intraday else None

    def native_timeframes(self) -> list[Timeframe]:
        return [Timeframe.MIN5, Timeframe.MIN15, Timeframe.MIN30, Timeframe.DAILY, Timeframe.WEEKLY]


# Thursday 11:00 ET: Yahoo's 55-day limit falls in the Friday 2026-08-28
# session at 11:00, so the window opens Monday 2026-08-31.
_YAHOO_NOW = _et(2026, 10, 22, 11, 0)
_YAHOO_OPEN = _et(2026, 8, 31, 9, 30)
# Monday 12:00 EDT: 600 days back is Wednesday 2025-03-05 11:00 EST, so the
# window opens Thursday 2025-03-06.
_ALPACA_NOW = _et(2026, 10, 26, 12, 0)
_ALPACA_OPEN = _et(2025, 3, 6, 9, 30)

_SHAPES = pytest.mark.parametrize(
    ("limit", "page_span", "now", "session_open"),
    [
        pytest.param(timedelta(days=55), None, _YAHOO_NOW, _YAHOO_OPEN, id="yahoo-single"),
        pytest.param(None, timedelta(days=30), _ALPACA_NOW, _ALPACA_OPEN, id="alpaca-paged"),
    ],
)


def _load(
    db_path: Path,
    provider: _Source,
    timeframe: Timeframe,
    now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[OHLCVSeries], list[str]]:
    monkeypatch.setattr(_FixedDateTime, "frozen", now)
    monkeypatch.setattr(controller_module, "datetime", _FixedDateTime)
    monkeypatch.setattr(controller_module, "create_provider", lambda _c, _s: provider)
    request = _ChartLoadRequest(1, "XBI", timeframe, alpaca_paper_connection())
    worker = _FetchWorker(str(db_path), cast(Any, None), request)
    finished: list[OHLCVSeries] = []
    errors: list[str] = []
    worker.finished.connect(lambda _request, series: finished.append(series))
    worker.error.connect(lambda _request, message: errors.append(message))
    worker.run()
    return finished, errors


def _rows(bars: list[Bar]) -> list[Row]:
    return [(bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume) for bar in bars]


def _cached(db_path: Path, timeframe: Timeframe) -> list[Bar]:
    with Cache(str(db_path)) as cache:
        return cache.get_raw_bars(alpaca_paper_connection().cache_namespace, "XBI", timeframe, 0, 2**62)


def _expected_group(session_open: datetime, period: int, index: int) -> Row:
    start = session_open + timedelta(minutes=period * index)
    group = _session_bars(Timeframe.MIN5, start, start + timedelta(minutes=period))
    return (
        group[0].timestamp,
        group[0].open,
        max(bar.high for bar in group),
        min(bar.low for bar in group),
        group[-1].close,
        sum(bar.volume for bar in group),
    )


def _period_key(timestamp: datetime, period: int) -> tuple[date, int]:
    local = timestamp.astimezone(ET)
    session_open = datetime.combine(local.date(), time(9, 30), tzinfo=ET)
    return local.date(), int((local - session_open).total_seconds() // 60) // period


def _assert_one_bar_per_period(bars: list[Bar], period: int) -> None:
    keys = [_period_key(bar.timestamp, period) for bar in bars]
    assert len(keys) == len(set(keys))


@_SHAPES
def test_cold_five_minute_load_starts_at_the_session_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: timedelta | None,
    page_span: timedelta | None,
    now: datetime,
    session_open: datetime,
) -> None:
    db_path = tmp_path / "test.db"
    provider = _Source(limit, page_span)

    finished, errors = _load(db_path, provider, Timeframe.MIN5, now, monkeypatch)

    assert errors == []
    assert min(call[1] for call in provider.calls if call[0] == Timeframe.MIN5) == session_open
    cached = _cached(db_path, Timeframe.MIN5)
    assert cached[0].timestamp == session_open
    assert finished[0].bars[0].timestamp == session_open
    with Cache(str(db_path)) as cache:
        coverage = cache.get_bar_fetch_coverage(
            alpaca_paper_connection().cache_namespace, "XBI", Timeframe.MIN5,
        )
    assert coverage is not None and coverage[0] == int(session_open.timestamp() * 1000)


@_SHAPES
@pytest.mark.parametrize("target", [Timeframe.MIN39, Timeframe.MIN65], ids=["39m", "65m"])
def test_cold_synthesized_load_starts_with_a_complete_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: timedelta | None,
    page_span: timedelta | None,
    now: datetime,
    session_open: datetime,
    target: Timeframe,
) -> None:
    db_path = tmp_path / "test.db"
    period = cast(int, target.minutes)

    finished, errors = _load(db_path, _Source(limit, page_span), target, now, monkeypatch)

    assert errors == []
    cached = _cached(db_path, target)
    # 39m: eight 5m bars, volume 40; 65m: thirteen, volume 65.
    assert _rows(cached[:2]) == [
        _expected_group(session_open, period, 0),
        _expected_group(session_open, period, 1),
    ]
    assert cached[0].volume == (40 if target == Timeframe.MIN39 else 65)
    assert finished[0].bars[0].timestamp == session_open
    _assert_one_bar_per_period(cached, period)


# ----------------------------------------------------------------------
# Caches written before A1, with mid-session coverage starts
# ----------------------------------------------------------------------


def _pre_a1_history_start(
    timeframe: Timeframe,
    aggregator: Aggregator,
    end: datetime,
) -> datetime:
    """_history_start as it was before A1: the raw lookback cutoff."""
    desired = (
        end - timedelta(days=controller_module._DEFAULT_LOOKBACK_DAYS)
        if timeframe.is_intraday
        else _DAILY_HISTORY_START
    )
    limit = aggregator.earliest_history_start(timeframe, end)
    return desired if limit is None else max(desired, limit)


@_SHAPES
@pytest.mark.parametrize("target", [Timeframe.MIN39, Timeframe.MIN65], ids=["39m", "65m"])
@pytest.mark.parametrize("watermark", ["settled", "legacy"])
def test_pre_a1_cache_with_mid_session_start_loads_without_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: timedelta | None,
    page_span: timedelta | None,
    now: datetime,
    session_open: datetime,
    target: Timeframe,
    watermark: str,
) -> None:
    db_path = tmp_path / "test.db"
    period = cast(int, target.minutes)
    namespace = alpaca_paper_connection().cache_namespace
    # The earlier load ran the previous afternoon at 13:17, before A1: its
    # cutoff, and so its coverage start, sits inside a session.
    earlier = now - timedelta(days=1) + timedelta(hours=2, minutes=17)
    with monkeypatch.context() as patch:
        patch.setattr(controller_module, "_history_start", _pre_a1_history_start)
        _finished, errors = _load(db_path, _Source(limit, page_span), target, earlier, monkeypatch)
    assert errors == []
    with Cache(str(db_path)) as cache:
        target_coverage = cache.get_bar_fetch_coverage(namespace, "XBI", target)
        assert target_coverage is not None
        assert datetime.fromtimestamp(target_coverage[0] / 1000, UTC).astimezone(ET).time() == time(13, 17)
        if watermark == "legacy":
            cache.replace_bar_fetch_coverage(
                namespace, "XBI", target, target_coverage[0], target_coverage[1] - 29_000,
            )
            assert controller_module._is_legacy_target_coverage(
                Aggregator(_Source(limit, page_span)),
                target,
                (target_coverage[0], target_coverage[1] - 29_000),
            )
    before = _cached(db_path, target)

    finished, errors = _load(db_path, _Source(limit, page_span), target, now, monkeypatch)

    assert errors == []
    cached = _cached(db_path, target)
    _assert_one_bar_per_period(cached, period)
    # The partial group at the old mid-session start is outside the window
    # and untouched.
    assert [bar for bar in cached if bar.timestamp < session_open] == [
        bar for bar in before if bar.timestamp < session_open
    ]
    displayed = finished[0].bars
    # With a provider limit the contiguous cached history before the window
    # is shown too, from the first whole session after the old mid-session
    # start; the partial group at that start is not.
    first_shown = (
        session_open
        if limit is None
        else controller_module._first_session_open_at_or_after(
            datetime.fromtimestamp(target_coverage[0] / 1000, UTC)
        )
    )
    assert displayed[0].timestamp == first_shown
    assert _rows(displayed[:2]) == [
        _expected_group(first_shown, period, 0),
        _expected_group(first_shown, period, 1),
    ]
    last_session = now.astimezone(ET).date()
    last_open = datetime.combine(last_session, time(9, 30), tzinfo=ET)
    complete_today = [
        _expected_group(last_open, period, index)
        for index in range(int((now - last_open).total_seconds() // 60) // period)
    ]
    assert [row for row in _rows(cached) if row[0] >= last_open][: len(complete_today)] == complete_today
