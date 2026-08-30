from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.controller import (
    _DataRoute,
    _Level1Worker,
    _SnapshotBatch,
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
from data.models import Bar, Level1Quote, MarketSnapshot, Timeframe
from data.provider.base import DataProvider
from data.provider.config import alpaca_paper_connection, yfinance_connection


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
        return [Timeframe.MIN15, Timeframe.DAILY]


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
    assert _history_start(
        Timeframe.MIN5,
        Aggregator(LimitedHistoryProvider()),
        now,
    ) == now - timedelta(days=55)


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
    assert coverage[1] == int(delayed_end.timestamp() * 1000)


def test_existing_cache_is_backfilled_once_and_then_only_extended(
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
            cached_at,
        ),
        ("AAPL", Timeframe.DAILY, cached_at, now),
    ]


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
        ]
    )
    worker.finished.connect(received.append)

    worker.run()

    assert set(received[0]) == {"AAPL", "ES=F"}


def test_provider_validation_exercises_market_data_snapshot() -> None:
    _validate_provider_connection(ValidProvider())


def test_provider_validation_rejects_missing_market_data() -> None:
    with pytest.raises(RuntimeError, match="market-data snapshot"):
        _validate_provider_connection(FakeProvider())


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
            cache,
            "AAPL",
            True,
            now,
        )
        worker.finished.connect(received.append)

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
            cache,
            "AAPL",
            True,
            now,
        )
        worker.finished.connect(received.append)

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
