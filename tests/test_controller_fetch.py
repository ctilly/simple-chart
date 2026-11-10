from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.controller import (
    _DataRoute,
    _Level1Worker,
    _Level1Request,
    _ProviderValidationWorker,
    _QuoteRouteIdentity,
    _SnapshotBatch,
    _SnapshotRequest,
    _SnapshotWorker,
    _asset_reference_refresh_due,
    _fetch_and_cache_bars,
    _fetch_series_with_references,
    _history_end,
    _history_start,
    _resolve_company_name,
    _route_for_symbol,
    _validate_provider_connection,
)
from app.header_bar import _level1_html
from data.aggregator import Aggregator
from data.cache import Cache
from data.errors import UserFacingError
from data.models import Bar, Level1Quote, MarketSnapshot, Timeframe
from data.provider.base import DataProvider, HistoryUnavailableError
from data.provider.config import alpaca_paper_connection, yfinance_connection
from data.session import (
    bucket_start as _bucket_start,
    expected_last_session_bar_start as _expected_last_session_bar_start,
)


class FakeProvider(DataProvider):

    def __init__(self) -> None:
        self.calls: list[tuple[str, Timeframe, datetime, datetime]] = []

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        self.calls.append((symbol, timeframe, start, end))
        if timeframe == Timeframe.DAILY:
            timestamp = end - timedelta(days=1)
        else:
            timestamp = end - timedelta(minutes=15)
        return [
            Bar(
                timestamp=timestamp,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000,
            )
        ]

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        return {}

    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        return None

    def fetch_company_name(self, symbol: str) -> str | None:
        return "Test Company"

    def native_timeframes(self) -> list[Timeframe]:
        return [
            Timeframe.MIN5,
            Timeframe.MIN15,
            Timeframe.DAILY,
            Timeframe.WEEKLY,
        ]


class SnapshotProvider(FakeProvider):
    def fetch_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        return {symbol: _snapshot(symbol) for symbol in symbols}


class ValidProvider(FakeProvider):
    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        return _quote(symbol)


class QuoteWithoutNameProvider(FakeProvider):
    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        return _quote(symbol, company_name=None)


class ReferenceProvider(FakeProvider):
    def __init__(self, company_name: str | None) -> None:
        super().__init__()
        self.company_name = company_name
        self.company_name_calls: list[str] = []

    def fetch_company_name(self, symbol: str) -> str | None:
        self.company_name_calls.append(symbol)
        return self.company_name


class YahooQuoteProvider(ReferenceProvider):
    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        return _quote(symbol, company_name=self.company_name)


class EmptyProvider(FakeProvider):
    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        self.calls.append((symbol, timeframe, start, end))
        return []


class FailingProvider(FakeProvider):
    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        self.calls.append((symbol, timeframe, start, end))
        raise RuntimeError("Provider request failed")


class LimitedHistoryProvider(FakeProvider):
    def earliest_history_start(
        self,
        timeframe: Timeframe,
        end: datetime,
    ) -> datetime | None:
        if timeframe.is_intraday:
            return end - timedelta(days=55)
        return None


class DelayedHistoryProvider(FakeProvider):
    def latest_history_end(
        self,
        timeframe: Timeframe,
        end: datetime,
    ) -> datetime:
        return end - timedelta(minutes=16)


class FiveMinuteProvider(FakeProvider):
    def native_timeframes(self) -> list[Timeframe]:
        return [Timeframe.MIN5, Timeframe.DAILY, Timeframe.WEEKLY]


class EmptyFiveMinuteProvider(FiveMinuteProvider):
    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        self.calls.append((symbol, timeframe, start, end))
        return []


class DelayedFiveMinuteProvider(FiveMinuteProvider):
    def latest_history_end(
        self,
        timeframe: Timeframe,
        end: datetime,
    ) -> datetime:
        return end - timedelta(minutes=16)


class StaticFiveMinuteProvider(FiveMinuteProvider):
    def __init__(self, bars: list[Bar]) -> None:
        super().__init__()
        self.bars = bars

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        self.calls.append((symbol, timeframe, start, end))
        if timeframe != Timeframe.MIN5:
            return []
        return [bar for bar in self.bars if start <= bar.timestamp < end]


class StaticDailyProvider(FiveMinuteProvider):
    def __init__(self, bar: Bar) -> None:
        super().__init__()
        self.bar = bar

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        self.calls.append((symbol, timeframe, start, end))
        return [self.bar] if timeframe == Timeframe.DAILY else []


class HistoryUnavailableFiveMinuteProvider(FiveMinuteProvider):
    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        self.calls.append((symbol, timeframe, start, end))
        raise HistoryUnavailableError(symbol, timeframe)


class RevisingFiveMinuteProvider(FiveMinuteProvider):
    def __init__(self, timestamp: datetime) -> None:
        super().__init__()
        self.timestamp = timestamp

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        self.calls.append((symbol, timeframe, start, end))
        revision = 100.0 if len(self.calls) == 1 else 110.0
        return [_priced_bar(self.timestamp, revision)]


def test_intraday_fetch_also_caches_daily_reference_bars(tmp_path: Path) -> None:
    now = datetime(2026, 1, 10, 20, 0, tzinfo=timezone.utc)
    provider = FakeProvider()
    aggregator = Aggregator(provider)

    with Cache(str(tmp_path / "test.db")) as cache:
        series = _fetch_series_with_references(
            aggregator,
            cache,
            "test-provider",
            "SPY",
            Timeframe.MIN15,
            now,
        )
        daily_bars = cache.get_bars(
            "test-provider",
            "SPY",
            Timeframe.DAILY,
            int((now - timedelta(days=600)).timestamp() * 1000),
            int(now.timestamp() * 1000),
        )

    assert [call[1] for call in provider.calls] == [Timeframe.DAILY, Timeframe.MIN15]
    assert series.timeframe == Timeframe.MIN15
    assert len(series.bars) == 1
    assert len(daily_bars) == 1


def test_daily_fetch_does_not_request_extra_reference_bars(tmp_path: Path) -> None:
    now = datetime(2026, 1, 10, 20, 0, tzinfo=timezone.utc)
    provider = FakeProvider()
    aggregator = Aggregator(provider)

    with Cache(str(tmp_path / "test.db")) as cache:
        series = _fetch_series_with_references(
            aggregator,
            cache,
            "test-provider",
            "SPY",
            Timeframe.DAILY,
            now,
        )

    assert [call[1] for call in provider.calls] == [Timeframe.DAILY]
    assert series.timeframe == Timeframe.DAILY
    assert len(series.bars) == 1


def test_futures_symbols_always_route_to_yahoo() -> None:
    selected = _DataRoute(Aggregator(FakeProvider()), alpaca_paper_connection())
    yahoo = _DataRoute(Aggregator(FakeProvider()), yfinance_connection())

    assert _route_for_symbol(" ES=F ", selected, yahoo) is yahoo
    assert _route_for_symbol("gc=f", selected, yahoo) is yahoo
    assert _route_for_symbol("AAPL", selected, yahoo) is selected


def test_history_start_requests_deep_daily_data() -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    assert _history_start(Timeframe.DAILY, Aggregator(FakeProvider()), now) == datetime(
        2016, 1, 1, tzinfo=timezone.utc
    )
    assert _history_start(Timeframe.WEEKLY, Aggregator(FakeProvider()), now) == datetime(
        2016, 1, 1, tzinfo=timezone.utc
    )
    # 55 days before now is Monday 2026-07-06 08:00 EDT; the window opens at
    # that session's 09:30 open.
    assert _history_start(
        Timeframe.MIN5,
        Aggregator(LimitedHistoryProvider()),
        now,
    ) == datetime(2026, 7, 6, 13, 30, tzinfo=timezone.utc)


def test_history_end_honors_provider_delay() -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    assert _history_end(Timeframe.MIN5, Aggregator(FakeProvider()), now) == now
    assert _history_end(
        Timeframe.MIN5,
        Aggregator(DelayedHistoryProvider()),
        now,
    ) == now - timedelta(minutes=16)


def test_delayed_provider_fetch_and_coverage_stop_at_delayed_endpoint(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    delayed_end = now - timedelta(minutes=16)
    provider = DelayedHistoryProvider()

    with Cache(str(tmp_path / "test.db")) as cache:
        _fetch_series_with_references(
            Aggregator(provider),
            cache,
            "alpaca:delayed_sip",
            "AAPL",
            Timeframe.DAILY,
            now,
        )
        coverage = cache.get_bar_fetch_coverage(
            "alpaca:delayed_sip",
            "AAPL",
            Timeframe.DAILY,
        )

    assert provider.calls[0][3] == delayed_end
    assert coverage is not None
    assert coverage[1] == int(
        datetime(2026, 8, 30, 4, 0, tzinfo=timezone.utc).timestamp() * 1000
    )


def test_delayed_intraday_coverage_floors_provider_safe_endpoint(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 11, 4, 15, 7, 12, tzinfo=timezone.utc)
    safe_end = now - timedelta(minutes=16)
    provider = DelayedFiveMinuteProvider()
    aggregator = Aggregator(provider)
    history_start = now - timedelta(days=10)

    with Cache(str(tmp_path / "test.db")) as cache:
        _fetch_and_cache_bars(
            aggregator,
            cache,
            "alpaca:delayed_sip",
            "AAPL",
            Timeframe.MIN5,
            history_start,
            _history_end(Timeframe.MIN5, aggregator, now),
        )
        coverage = cache.get_bar_fetch_coverage(
            "alpaca:delayed_sip",
            "AAPL",
            Timeframe.MIN5,
        )

    assert provider.calls[0][3] == safe_end
    assert coverage is not None
    assert coverage[1] == int(
        datetime(2026, 11, 4, 14, 50, tzinfo=timezone.utc).timestamp() * 1000
    )
    assert coverage[1] <= int(safe_end.timestamp() * 1000)


def test_complete_premarket_equity_cache_skips_daily_and_intraday_tails(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 11, 4, 14, 8, 54, tzinfo=timezone.utc)
    history_start = now - timedelta(days=600)
    prior_daily = datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc)
    prior_daily_settled = datetime(2026, 11, 4, 5, 0, tzinfo=timezone.utc)
    prior_last_65m = datetime(2026, 11, 3, 19, 55, tzinfo=timezone.utc)
    prior_settled = datetime(2026, 11, 3, 21, 5, tzinfo=timezone.utc)
    provider = EmptyFiveMinuteProvider()

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "IWM",
            Timeframe.DAILY,
            [_bar_at(prior_daily)],
        )
        cache.extend_bar_fetch_coverage(
            "alpaca:delayed_sip",
            "IWM",
            Timeframe.DAILY,
            int(datetime(2016, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
            int(prior_daily_settled.timestamp() * 1000),
        )
        cache.put_bars(
            "alpaca:delayed_sip",
            "IWM",
            Timeframe.MIN65,
            [_bar_at(prior_last_65m)],
        )
        cache.extend_bar_fetch_coverage(
            "alpaca:delayed_sip",
            "IWM",
            Timeframe.MIN65,
            int(history_start.timestamp() * 1000),
            int(prior_settled.timestamp() * 1000),
        )

        series = _fetch_series_with_references(
            Aggregator(provider),
            cache,
            "alpaca:delayed_sip",
            "IWM",
            Timeframe.MIN65,
            now,
        )

    assert provider.calls == []
    assert series.bars == [_bar_at(prior_last_65m)]


def test_delayed_endpoint_controls_premarket_cache_eligibility(
    tmp_path: Path,
) -> None:
    wall_clock = datetime(2026, 11, 4, 14, 40, tzinfo=timezone.utc)
    safe_endpoint = wall_clock - timedelta(minutes=16)
    history_start = safe_endpoint - timedelta(days=600)
    prior_daily = datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc)
    prior_daily_settled = datetime(2026, 11, 4, 5, 0, tzinfo=timezone.utc)
    prior_last_5m = datetime(2026, 11, 3, 20, 55, tzinfo=timezone.utc)
    prior_settled = datetime(2026, 11, 3, 21, 5, tzinfo=timezone.utc)
    provider = DelayedFiveMinuteProvider()

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "AAPL",
            Timeframe.DAILY,
            [_bar_at(prior_daily)],
        )
        cache.extend_bar_fetch_coverage(
            "alpaca:delayed_sip",
            "AAPL",
            Timeframe.DAILY,
            int(datetime(2016, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
            int(prior_daily_settled.timestamp() * 1000),
        )
        cache.put_bars(
            "alpaca:delayed_sip",
            "AAPL",
            Timeframe.MIN5,
            [_bar_at(prior_last_5m)],
        )
        cache.extend_bar_fetch_coverage(
            "alpaca:delayed_sip",
            "AAPL",
            Timeframe.MIN5,
            int(history_start.timestamp() * 1000),
            int(prior_settled.timestamp() * 1000),
        )

        series = _fetch_series_with_references(
            Aggregator(provider),
            cache,
            "alpaca:delayed_sip",
            "AAPL",
            Timeframe.MIN5,
            wall_clock,
        )

    assert provider.calls == []
    assert series.bars == [_bar_at(prior_last_5m)]


def test_premarket_equity_cache_missing_prior_session_fetches_from_newest_bars(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 11, 4, 14, 8, 54, tzinfo=timezone.utc)
    history_start = now - timedelta(days=600)
    stale_daily = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
    stale_last_65m = datetime(2026, 11, 2, 19, 55, tzinfo=timezone.utc)
    misleading_coverage_end = datetime(
        2026,
        11,
        3,
        21,
        0,
        tzinfo=timezone.utc,
    )
    provider = FiveMinuteProvider()

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:delayed_sip",
            "IWM",
            Timeframe.DAILY,
            [_bar_at(stale_daily)],
        )
        cache.extend_bar_fetch_coverage(
            "alpaca:delayed_sip",
            "IWM",
            Timeframe.DAILY,
            int(datetime(2016, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
            int(datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc).timestamp() * 1000),
        )
        cache.put_bars(
            "alpaca:delayed_sip",
            "IWM",
            Timeframe.MIN65,
            [_bar_at(stale_last_65m)],
        )
        cache.extend_bar_fetch_coverage(
            "alpaca:delayed_sip",
            "IWM",
            Timeframe.MIN65,
            int(history_start.timestamp() * 1000),
            int(misleading_coverage_end.timestamp() * 1000),
        )

        _fetch_series_with_references(
            Aggregator(provider),
            cache,
            "alpaca:delayed_sip",
            "IWM",
            Timeframe.MIN65,
            now,
        )

    assert [call[1] for call in provider.calls] == [
        Timeframe.DAILY,
        Timeframe.MIN5,
    ]
    assert provider.calls[0][2] == stale_daily
    assert provider.calls[1][2] == stale_last_65m


@pytest.mark.parametrize(
    "symbol",
    [
        "BTC-USD",
        "BTCUSD",
        "^GSPC",
        "EURUSD=X",
        "GC=F",
        "BP.L",
        "BRK.B",
    ],
)
def test_unknown_or_non_equity_symbols_retain_tail_refresh(
    tmp_path: Path,
    symbol: str,
) -> None:
    request_end = datetime(2026, 11, 4, 14, 8, 54, tzinfo=timezone.utc)
    prior_last_bar = datetime(2026, 11, 3, 20, 55, tzinfo=timezone.utc)
    prior_close = datetime(2026, 11, 3, 21, 0, tzinfo=timezone.utc)
    provider = EmptyFiveMinuteProvider()

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "yfinance",
            symbol,
            Timeframe.MIN5,
            [_bar_at(prior_last_bar)],
        )
        cache.extend_bar_fetch_coverage(
            "yfinance",
            symbol,
            Timeframe.MIN5,
            int((request_end - timedelta(days=30)).timestamp() * 1000),
            int(prior_close.timestamp() * 1000),
        )

        _fetch_and_cache_bars(
            Aggregator(provider),
            cache,
            "yfinance",
            symbol,
            Timeframe.MIN5,
            request_end - timedelta(days=30),
            request_end,
        )

    assert provider.calls == [
        (symbol, Timeframe.MIN5, prior_close, request_end)
    ]


def test_weekly_tail_refresh_is_not_session_suppressed(tmp_path: Path) -> None:
    request_end = datetime(2026, 11, 4, 14, 8, 54, tzinfo=timezone.utc)
    week_start = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
    provider = EmptyProvider()

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "yfinance",
            "AAPL",
            Timeframe.WEEKLY,
            [_bar_at(week_start)],
        )
        cache.extend_bar_fetch_coverage(
            "yfinance",
            "AAPL",
            Timeframe.WEEKLY,
            int(datetime(2016, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
            int(week_start.timestamp() * 1000),
        )

        _fetch_and_cache_bars(
            Aggregator(provider),
            cache,
            "yfinance",
            "AAPL",
            Timeframe.WEEKLY,
            datetime(2016, 1, 1, tzinfo=timezone.utc),
            request_end,
        )

    assert provider.calls == [
        ("AAPL", Timeframe.WEEKLY, week_start, request_end)
    ]


def test_empty_existing_cache_tail_preserves_native_coverage(
    tmp_path: Path,
) -> None:
    history_start = datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc)
    stale_bar = datetime(2026, 11, 3, 19, 55, tzinfo=timezone.utc)
    coverage_end = datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc)
    request_end = datetime(2026, 11, 3, 22, 0, tzinfo=timezone.utc)
    provider = EmptyFiveMinuteProvider()
    original_coverage = (
        int(history_start.timestamp() * 1000),
        int(coverage_end.timestamp() * 1000),
    )

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            [_bar_at(stale_bar)],
        )
        cache.extend_bar_fetch_coverage(
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            *original_coverage,
        )

        _fetch_and_cache_bars(
            Aggregator(provider),
            cache,
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            history_start,
            request_end,
        )
        coverage = cache.get_bar_fetch_coverage(
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
        )

    assert provider.calls == [
        ("AAPL", Timeframe.MIN5, stale_bar, request_end)
    ]
    assert coverage == original_coverage


def test_postclose_load_finalizes_partial_last_intraday_bar(
    tmp_path: Path,
) -> None:
    history_start = datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc)
    last_bar_start = datetime(2026, 11, 3, 20, 55, tzinfo=timezone.utc)
    request_end = datetime(2026, 11, 3, 21, 30, tzinfo=timezone.utc)
    provider = StaticFiveMinuteProvider([_priced_bar(last_bar_start, 110.0)])

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            [_priced_bar(last_bar_start, 100.0)],
        )
        cache.extend_bar_fetch_coverage(
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            int(history_start.timestamp() * 1000),
            int(last_bar_start.timestamp() * 1000),
        )

        _fetch_and_cache_bars(
            Aggregator(provider),
            cache,
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            history_start,
            request_end,
        )
        bars = cache.get_bars(
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            int(history_start.timestamp() * 1000),
            int(request_end.timestamp() * 1000),
        )

    assert provider.calls == [
        ("AAPL", Timeframe.MIN5, last_bar_start, request_end)
    ]
    assert bars[-1] == _priced_bar(last_bar_start, 110.0)


def test_postclose_load_finalizes_partial_daily_reference(
    tmp_path: Path,
) -> None:
    history_start = datetime(2016, 1, 1, tzinfo=timezone.utc)
    daily_start = datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc)
    next_day = datetime(2026, 11, 4, 5, 0, tzinfo=timezone.utc)
    request_end = datetime(2026, 11, 3, 22, 0, tzinfo=timezone.utc)
    provider = StaticDailyProvider(_priced_bar(daily_start, 110.0))

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
            [_priced_bar(daily_start, 100.0)],
        )
        cache.extend_bar_fetch_coverage(
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
            int(history_start.timestamp() * 1000),
            int(daily_start.timestamp() * 1000),
        )

        _fetch_and_cache_bars(
            Aggregator(provider),
            cache,
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
            history_start,
            request_end,
        )
        coverage = cache.get_bar_fetch_coverage(
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
        )
        bars = cache.get_bars(
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
            int(history_start.timestamp() * 1000),
            int(request_end.timestamp() * 1000),
        )

    assert provider.calls == [
        ("AAPL", Timeframe.DAILY, daily_start, request_end)
    ]
    assert coverage is not None
    assert coverage[1] == int(next_day.timestamp() * 1000)
    assert bars[-1] == _priced_bar(daily_start, 110.0)


def test_empty_cold_daily_postclose_load_retries_from_session_start(
    tmp_path: Path,
) -> None:
    history_start = datetime(2016, 1, 1, tzinfo=timezone.utc)
    daily_start = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
    first_end = datetime(2026, 11, 2, 21, 30, tzinfo=timezone.utc)
    second_end = datetime(2026, 11, 2, 22, 0, tzinfo=timezone.utc)
    provider = EmptyFiveMinuteProvider()

    with Cache(str(tmp_path / "test.db")) as cache:
        _fetch_and_cache_bars(
            Aggregator(provider),
            cache,
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
            history_start,
            first_end,
        )
        _fetch_and_cache_bars(
            Aggregator(provider),
            cache,
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
            history_start,
            second_end,
        )

    assert provider.calls == [
        ("AAPL", Timeframe.DAILY, history_start, first_end),
        ("AAPL", Timeframe.DAILY, daily_start, second_end),
    ]


def test_lagging_daily_postclose_load_retries_missing_session_bar(
    tmp_path: Path,
) -> None:
    history_start = datetime(2016, 1, 1, tzinfo=timezone.utc)
    friday_start = datetime(2026, 10, 30, 4, 0, tzinfo=timezone.utc)
    first_end = datetime(2026, 11, 2, 21, 30, tzinfo=timezone.utc)
    second_end = datetime(2026, 11, 2, 22, 0, tzinfo=timezone.utc)
    provider = StaticDailyProvider(_bar_at(friday_start))

    with Cache(str(tmp_path / "test.db")) as cache:
        for request_end in (first_end, second_end):
            _fetch_and_cache_bars(
                Aggregator(provider),
                cache,
                "alpaca:iex",
                "AAPL",
                Timeframe.DAILY,
                history_start,
                request_end,
            )

    assert provider.calls == [
        ("AAPL", Timeframe.DAILY, history_start, first_end),
        ("AAPL", Timeframe.DAILY, friday_start, second_end),
    ]


def test_close_boundary_does_not_settle_partial_intraday_bar(
    tmp_path: Path,
) -> None:
    history_start = datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc)
    last_bar_start = datetime(2026, 11, 3, 20, 55, tzinfo=timezone.utc)
    session_close = datetime(2026, 11, 3, 21, 0, tzinfo=timezone.utc)
    request_end = datetime(2026, 11, 3, 21, 2, tzinfo=timezone.utc)
    provider = StaticFiveMinuteProvider([_priced_bar(last_bar_start, 110.0)])

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            [_priced_bar(last_bar_start, 100.0)],
        )
        cache.extend_bar_fetch_coverage(
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            int(history_start.timestamp() * 1000),
            int(session_close.timestamp() * 1000),
        )

        _fetch_and_cache_bars(
            Aggregator(provider),
            cache,
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            history_start,
            request_end,
        )

    assert provider.calls == [
        ("AAPL", Timeframe.MIN5, last_bar_start, request_end)
    ]


def test_daily_close_boundary_does_not_write_settled_watermark(
    tmp_path: Path,
) -> None:
    history_start = datetime(2016, 1, 1, tzinfo=timezone.utc)
    daily_start = datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc)
    session_close = datetime(2026, 11, 3, 21, 0, tzinfo=timezone.utc)
    provider = StaticDailyProvider(_priced_bar(daily_start, 110.0))

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
            [_priced_bar(daily_start, 100.0)],
        )
        cache.extend_bar_fetch_coverage(
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
            int(history_start.timestamp() * 1000),
            int(daily_start.timestamp() * 1000),
        )

        _fetch_and_cache_bars(
            Aggregator(provider),
            cache,
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
            history_start,
            session_close,
        )
        coverage = cache.get_bar_fetch_coverage(
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
        )

    assert coverage is not None
    assert coverage[1] == int(daily_start.timestamp() * 1000)


def test_daily_bar_refreshes_again_after_settlement_delay(
    tmp_path: Path,
) -> None:
    history_start = datetime(2016, 1, 1, tzinfo=timezone.utc)
    daily_start = datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc)
    first_end = datetime(2026, 11, 3, 21, 0, 30, tzinfo=timezone.utc)
    second_end = datetime(2026, 11, 3, 21, 20, tzinfo=timezone.utc)
    premarket_end = datetime(2026, 11, 4, 13, 0, tzinfo=timezone.utc)
    next_day = datetime(2026, 11, 4, 5, 0, tzinfo=timezone.utc)
    provider = RevisingFiveMinuteProvider(daily_start)

    with Cache(str(tmp_path / "test.db")) as cache:
        for request_end in (first_end, second_end):
            _fetch_and_cache_bars(
                Aggregator(provider),
                cache,
                "alpaca:iex",
                "AAPL",
                Timeframe.DAILY,
                history_start,
                request_end,
            )
        _fetch_and_cache_bars(
            Aggregator(provider),
            cache,
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
            history_start,
            premarket_end,
        )
        coverage = cache.get_bar_fetch_coverage(
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
        )
        bars = cache.get_bars(
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
            int(history_start.timestamp() * 1000),
            int(second_end.timestamp() * 1000),
        )

    assert provider.calls == [
        ("AAPL", Timeframe.DAILY, history_start, first_end),
        ("AAPL", Timeframe.DAILY, daily_start, second_end),
    ]
    assert bars[-1] == _priced_bar(daily_start, 110.0)
    assert coverage is not None
    assert coverage[1] == int(next_day.timestamp() * 1000)


def test_existing_coverage_without_bars_uses_bounded_tail_start(
    tmp_path: Path,
) -> None:
    history_start = datetime(2025, 3, 14, 14, 0, tzinfo=timezone.utc)
    coverage_end = datetime(2026, 11, 3, 21, 0, tzinfo=timezone.utc)
    request_end = datetime(2026, 11, 4, 14, 8, 54, tzinfo=timezone.utc)
    provider = EmptyFiveMinuteProvider()

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.extend_bar_fetch_coverage(
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            int(history_start.timestamp() * 1000),
            int(coverage_end.timestamp() * 1000),
        )

        for _ in range(2):
            _fetch_and_cache_bars(
                Aggregator(provider),
                cache,
                "alpaca:iex",
                "AAPL",
                Timeframe.MIN5,
                history_start,
                request_end,
            )

    assert provider.calls == [
        ("AAPL", Timeframe.MIN5, coverage_end, request_end),
        ("AAPL", Timeframe.MIN5, coverage_end, request_end),
    ]


@pytest.mark.parametrize(
    "timeframe",
    [
        Timeframe.MIN1,
        Timeframe.MIN5,
        Timeframe.MIN15,
        Timeframe.MIN30,
        Timeframe.MIN39,
        Timeframe.MIN65,
    ],
)
@pytest.mark.parametrize(
    "session_date",
    [date(2026, 1, 14), date(2026, 7, 14)],
)
def test_expected_last_session_bar_matches_aggregator_bucket(
    timeframe: Timeframe,
    session_date: date,
) -> None:
    final_session_minute = datetime.combine(
        session_date,
        time(15, 59),
        tzinfo=ZoneInfo("America/New_York"),
    )
    expected = _expected_last_session_bar_start(
        timeframe,
        session_date,
        timezone.utc,
    )

    assert expected == _bucket_start(
        final_session_minute.astimezone(timezone.utc),
        timeframe.minutes or 1,
    )


def test_weekday_closure_empty_tail_is_safe_and_retryable(
    tmp_path: Path,
) -> None:
    history_start = datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc)
    prior_last_bar = datetime(2026, 11, 3, 20, 55, tzinfo=timezone.utc)
    prior_close = datetime(2026, 11, 3, 21, 0, tzinfo=timezone.utc)
    holiday_endpoint = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)
    provider = EmptyFiveMinuteProvider()
    original_coverage = (
        int(history_start.timestamp() * 1000),
        int(prior_close.timestamp() * 1000),
    )

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            [_bar_at(prior_last_bar)],
        )
        cache.extend_bar_fetch_coverage(
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            *original_coverage,
        )

        for _ in range(2):
            _fetch_and_cache_bars(
                Aggregator(provider),
                cache,
                "alpaca:iex",
                "AAPL",
                Timeframe.MIN5,
                history_start,
                holiday_endpoint,
            )
        coverage = cache.get_bar_fetch_coverage(
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
        )

    assert provider.calls == [
        ("AAPL", Timeframe.MIN5, prior_close, holiday_endpoint),
        ("AAPL", Timeframe.MIN5, prior_close, holiday_endpoint),
    ]
    assert coverage == original_coverage


def test_typed_history_unavailable_is_suppressed_only_for_cached_tail(
    tmp_path: Path,
) -> None:
    history_start = datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc)
    stale_bar = datetime(2026, 11, 3, 19, 55, tzinfo=timezone.utc)
    coverage_end = datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc)
    request_end = datetime(2026, 11, 3, 22, 0, tzinfo=timezone.utc)
    provider = HistoryUnavailableFiveMinuteProvider()
    original_coverage = (
        int(history_start.timestamp() * 1000),
        int(coverage_end.timestamp() * 1000),
    )

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "yfinance",
            "AAPL",
            Timeframe.MIN5,
            [_bar_at(stale_bar)],
        )
        cache.extend_bar_fetch_coverage(
            "yfinance",
            "AAPL",
            Timeframe.MIN5,
            *original_coverage,
        )

        _fetch_and_cache_bars(
            Aggregator(provider),
            cache,
            "yfinance",
            "AAPL",
            Timeframe.MIN5,
            history_start,
            request_end,
        )
        coverage = cache.get_bar_fetch_coverage(
            "yfinance",
            "AAPL",
            Timeframe.MIN5,
        )

    assert provider.calls == [
        ("AAPL", Timeframe.MIN5, stale_bar, request_end)
    ]
    assert coverage == original_coverage

    with Cache(str(tmp_path / "cold.db")) as cache:
        with pytest.raises(HistoryUnavailableError):
            _fetch_and_cache_bars(
                Aggregator(HistoryUnavailableFiveMinuteProvider()),
                cache,
                "yfinance",
                "AAPL",
                Timeframe.MIN5,
                history_start,
                request_end,
            )


def test_repeat_empty_intraday_refresh_starts_at_completed_boundary(
    tmp_path: Path,
) -> None:
    first_end = datetime(2026, 11, 4, 15, 3, 4, tzinfo=timezone.utc)
    second_end = first_end + timedelta(seconds=4)
    history_start = first_end - timedelta(days=10)
    provider = FiveMinuteProvider()
    aggregator = Aggregator(provider)

    with Cache(str(tmp_path / "test.db")) as cache:
        _fetch_and_cache_bars(
            aggregator,
            cache,
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            history_start,
            first_end,
        )
        _fetch_and_cache_bars(
            aggregator,
            cache,
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            history_start,
            second_end,
        )
        coverage = cache.get_bar_fetch_coverage(
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
        )

    assert provider.calls[1] == (
        "AAPL",
        Timeframe.MIN5,
        datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc),
        second_end,
    )
    assert coverage is not None
    assert coverage[1] == int(
        datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc).timestamp() * 1000
    )


def test_preopen_subminute_endpoint_persists_only_completed_coverage(
    tmp_path: Path,
) -> None:
    request_end = datetime(2026, 11, 4, 8, 59, 50, tzinfo=timezone.utc)
    history_start = request_end - timedelta(days=10)

    with Cache(str(tmp_path / "test.db")) as cache:
        _fetch_and_cache_bars(
            Aggregator(EmptyFiveMinuteProvider()),
            cache,
            "yfinance",
            "GC=F",
            Timeframe.MIN5,
            history_start,
            request_end,
        )
        coverage = cache.get_bar_fetch_coverage(
            "yfinance",
            "GC=F",
            Timeframe.MIN5,
        )

    assert coverage is not None
    assert coverage[1] == int(
        datetime(2026, 11, 4, 8, 55, tzinfo=timezone.utc).timestamp() * 1000
    )
    assert coverage[1] <= int(request_end.timestamp() * 1000)


def test_boundary_crossing_finalizes_previous_forming_bar(
    tmp_path: Path,
) -> None:
    bar_start = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)
    first_end = datetime(2026, 11, 4, 15, 3, tzinfo=timezone.utc)
    second_end = datetime(2026, 11, 4, 15, 6, tzinfo=timezone.utc)
    history_start = first_end - timedelta(days=10)
    provider = RevisingFiveMinuteProvider(bar_start)

    with Cache(str(tmp_path / "test.db")) as cache:
        _fetch_and_cache_bars(
            Aggregator(provider),
            cache,
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            history_start,
            first_end,
        )
        _fetch_and_cache_bars(
            Aggregator(provider),
            cache,
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            history_start,
            second_end,
        )
        bars = cache.get_bars(
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
            int(history_start.timestamp() * 1000),
            int(second_end.timestamp() * 1000),
        )
        coverage = cache.get_bar_fetch_coverage(
            "alpaca:iex",
            "AAPL",
            Timeframe.MIN5,
        )

    assert provider.calls[1][2] == bar_start
    assert bars == [_priced_bar(bar_start, 110.0)]
    assert coverage is not None
    assert coverage[1] == int(
        datetime(2026, 11, 4, 15, 5, tzinfo=timezone.utc).timestamp() * 1000
    )


def test_repeat_intraday_load_refreshes_daily_from_day_boundary(
    tmp_path: Path,
) -> None:
    first_now = datetime(2026, 11, 4, 15, 3, 4, tzinfo=timezone.utc)
    second_now = first_now + timedelta(seconds=4)
    provider = FakeProvider()
    aggregator = Aggregator(provider)

    with Cache(str(tmp_path / "test.db")) as cache:
        _fetch_series_with_references(
            aggregator,
            cache,
            "test-provider",
            "AAPL",
            Timeframe.MIN15,
            first_now,
        )
        _fetch_series_with_references(
            aggregator,
            cache,
            "test-provider",
            "AAPL",
            Timeframe.MIN15,
            second_now,
        )

    assert provider.calls[2] == (
        "AAPL",
        Timeframe.DAILY,
        datetime(2026, 11, 4, 5, 0, tzinfo=timezone.utc),
        second_now,
    )
    assert provider.calls[3] == (
        "AAPL",
        Timeframe.MIN15,
        datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc),
        second_now,
    )


def test_existing_bars_without_coverage_are_refetched_then_refreshed(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    cached_at = datetime(2025, 1, 8, 5, 0, tzinfo=timezone.utc)
    provider = EmptyProvider()

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
            [_bar_at(cached_at)],
        )
        _fetch_and_cache_bars(
            Aggregator(provider),
            cache,
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
            datetime(2016, 1, 1, tzinfo=timezone.utc),
            now,
        )
        _fetch_and_cache_bars(
            Aggregator(provider),
            cache,
            "alpaca:iex",
            "AAPL",
            Timeframe.DAILY,
            datetime(2016, 1, 1, tzinfo=timezone.utc),
            now,
        )

    assert provider.calls == [
        (
            "AAPL",
            Timeframe.DAILY,
            datetime(2016, 1, 1, tzinfo=timezone.utc),
            now,
        ),
        (
            "AAPL",
            Timeframe.DAILY,
            cached_at,
            now,
        ),
    ]


def test_stale_cache_refetches_only_provider_safe_window(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    safe_start = now - timedelta(days=55)
    stale_at = now - timedelta(days=100)
    provider = EmptyProvider()

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            "yfinance",
            "AAPL",
            Timeframe.MIN15,
            [_bar_at(stale_at)],
        )
        cache.extend_bar_fetch_coverage(
            "yfinance",
            "AAPL",
            Timeframe.MIN15,
            int(stale_at.timestamp() * 1000),
            int(stale_at.timestamp() * 1000),
        )

        _fetch_and_cache_bars(
            Aggregator(provider),
            cache,
            "yfinance",
            "AAPL",
            Timeframe.MIN15,
            safe_start,
            now,
        )
        coverage = cache.get_bar_fetch_coverage(
            "yfinance",
            "AAPL",
            Timeframe.MIN15,
        )

    assert provider.calls == [("AAPL", Timeframe.MIN15, safe_start, now)]
    assert coverage == (
        int(safe_start.timestamp() * 1000),
        int(now.timestamp() * 1000),
    )


def test_failed_fetch_does_not_advance_coverage(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    start = now - timedelta(days=55)

    with Cache(str(tmp_path / "test.db")) as cache:
        with pytest.raises(RuntimeError, match="Provider request failed"):
            _fetch_and_cache_bars(
                Aggregator(FailingProvider()),
                cache,
                "yfinance",
                "AAPL",
                Timeframe.MIN15,
                start,
                now,
            )
        coverage = cache.get_bar_fetch_coverage(
            "yfinance",
            "AAPL",
            Timeframe.MIN15,
        )

    assert coverage is None


def test_fetch_coverage_is_independent_for_each_provider_namespace(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    start = datetime(2016, 1, 1, tzinfo=timezone.utc)
    provider = EmptyProvider()
    aggregator = Aggregator(provider)

    with Cache(str(tmp_path / "test.db")) as cache:
        for namespace in ("yfinance", "alpaca:iex"):
            _fetch_and_cache_bars(
                aggregator,
                cache,
                namespace,
                "AAPL",
                Timeframe.DAILY,
                start,
                now,
            )

    assert provider.calls == [
        ("AAPL", Timeframe.DAILY, start, now),
        ("AAPL", Timeframe.DAILY, start, now),
    ]


def test_snapshot_worker_merges_provider_batches() -> None:
    equity_provider = SnapshotProvider()
    futures_provider = SnapshotProvider()
    received: list[dict[str, MarketSnapshot]] = []
    worker = _SnapshotWorker(
        [
            _SnapshotBatch(Aggregator(equity_provider), ["AAPL"]),
            _SnapshotBatch(Aggregator(futures_provider), ["ES=F"]),
        ],
        _SnapshotRequest(0, ()),
    )
    worker.finished.connect(lambda request, snapshots: received.append(snapshots))

    worker.run()

    assert set(received[0]) == {"AAPL", "ES=F"}


def test_provider_validation_exercises_market_data_snapshot() -> None:
    _validate_provider_connection(ValidProvider())


def test_provider_validation_rejects_missing_market_data() -> None:
    with pytest.raises(UserFacingError, match="market-data snapshot"):
        _validate_provider_connection(FakeProvider())


def test_provider_validation_shows_the_missing_snapshot_message() -> None:
    errors: list[str] = []
    worker = _ProviderValidationWorker(FakeProvider(), "Alpaca Paper")
    worker.error.connect(errors.append)

    worker.run()

    assert errors == ["The provider returned no SPY market-data snapshot."]


def test_company_name_resolution_uses_fresh_provider_neutral_cache(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    provider = ReferenceProvider("New Name")
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_asset_reference(
            "AAPL",
            "Cached Name",
            now - timedelta(days=10),
        )

        company_name = _resolve_company_name(
            cache,
            Aggregator(provider),
            "AAPL",
            now,
            True,
        )

    assert company_name == "Cached Name"
    assert provider.company_name_calls == []


def test_company_name_resolution_refreshes_stale_name_from_yahoo(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    provider = ReferenceProvider(" Meta Platforms, Inc. ")
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_asset_reference(
            "META",
            "Facebook, Inc.",
            now - timedelta(days=31),
        )

        company_name = _resolve_company_name(
            cache,
            Aggregator(provider),
            "META",
            now,
            True,
        )
        refreshed = cache.get_asset_reference("META")

    assert company_name == "Meta Platforms, Inc."
    assert provider.company_name_calls == ["META"]
    assert refreshed is not None
    assert refreshed.company_name == "Meta Platforms, Inc."
    assert refreshed.refreshed_at == now


def test_company_name_resolution_keeps_stale_name_when_refresh_fails(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    provider = ReferenceProvider(None)
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_asset_reference(
            "AAPL",
            "Apple Inc.",
            now - timedelta(days=31),
        )

        company_name = _resolve_company_name(
            cache,
            Aggregator(provider),
            "AAPL",
            now,
            True,
        )

    assert company_name == "Apple Inc."
    assert provider.company_name_calls == ["AAPL"]


def test_asset_reference_refresh_retries_missing_name_after_one_hour(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    with Cache(str(tmp_path / "test.db")) as cache:
        assert not _asset_reference_refresh_due(
            cache,
            "UNKNOWN",
            now,
            now - timedelta(minutes=30),
        )
        assert _asset_reference_refresh_due(
            cache,
            "UNKNOWN",
            now,
            now - timedelta(hours=1),
        )


def test_level1_worker_merges_yahoo_name_into_alpaca_quote(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    market_provider = QuoteWithoutNameProvider()
    reference_provider = ReferenceProvider("Apple Inc.")
    received: list[Level1Quote | None] = []
    with Cache(str(tmp_path / "test.db")) as cache:
        worker = _Level1Worker(
            Aggregator(market_provider),
            Aggregator(reference_provider),
            str(tmp_path / "test.db"),
            _Level1Request("AAPL", 0, _QuoteRouteIdentity(yfinance_connection(), 0)),
            True,
            now,
        )
        worker.finished.connect(lambda request, quote: received.append(quote))

        worker.run()
        cached = cache.get_asset_reference("AAPL")

    quote = received[0]
    assert quote is not None
    assert quote.company_name == "Apple Inc."
    assert "Apple Inc." in _level1_html(quote)
    assert reference_provider.company_name_calls == ["AAPL"]
    assert cached is not None
    assert cached.company_name == "Apple Inc."


def test_level1_worker_reuses_name_already_returned_by_yahoo_quote(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)
    provider = YahooQuoteProvider("Apple Inc.")
    aggregator = Aggregator(provider)
    received: list[Level1Quote | None] = []
    with Cache(str(tmp_path / "test.db")) as cache:
        worker = _Level1Worker(
            aggregator,
            aggregator,
            str(tmp_path / "test.db"),
            _Level1Request("AAPL", 0, _QuoteRouteIdentity(yfinance_connection(), 0)),
            True,
            now,
        )
        worker.finished.connect(lambda request, quote: received.append(quote))

        worker.run()
        cached = cache.get_asset_reference("AAPL")

    assert received[0] is not None
    assert received[0].company_name == "Apple Inc."
    assert provider.company_name_calls == []
    assert cached is not None
    assert cached.company_name == "Apple Inc."


def _snapshot(symbol: str) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        last_price=100.0,
        change=1.0,
        change_percent=1.0,
        previous_close=99.0,
        timestamp=None,
    )


def _quote(
    symbol: str,
    company_name: str | None = "Test Company",
) -> Level1Quote:
    return Level1Quote(
        symbol=symbol,
        company_name=company_name,
        last_price=100.0,
        change=1.0,
        change_percent=1.0,
        bid=99.0,
        bid_size=10,
        ask=101.0,
        ask_size=10,
        open=99.0,
        high=101.0,
        low=98.0,
        volume=1_000,
        previous_close=99.0,
    )


def _bar_at(timestamp: datetime) -> Bar:
    return Bar(
        timestamp=timestamp,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1_000,
    )


def _priced_bar(timestamp: datetime, price: float) -> Bar:
    return Bar(
        timestamp=timestamp,
        open=price,
        high=price + 1.0,
        low=price - 1.0,
        close=price + 0.5,
        volume=1_000,
    )
