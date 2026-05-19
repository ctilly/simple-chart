from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.controller import _fetch_series_with_references
from data.aggregator import Aggregator
from data.cache import Cache
from data.models import Bar, Timeframe
from data.provider.base import DataProvider


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

    def native_timeframes(self) -> list[Timeframe]:
        return [Timeframe.MIN15, Timeframe.DAILY]


def test_intraday_fetch_also_caches_daily_reference_bars(tmp_path: Path) -> None:
    now = datetime(2026, 1, 10, 20, 0, tzinfo=timezone.utc)
    provider = FakeProvider()
    aggregator = Aggregator(provider)

    with Cache(str(tmp_path / "test.db")) as cache:
        series = _fetch_series_with_references(
            aggregator,
            cache,
            "SPY",
            Timeframe.MIN15,
            55,
            now,
        )
        daily_bars = cache.get_bars(
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
            "SPY",
            Timeframe.DAILY,
            600,
            now,
        )

    assert [call[1] for call in provider.calls] == [Timeframe.DAILY]
    assert series.timeframe == Timeframe.DAILY
    assert len(series.bars) == 1
