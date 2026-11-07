from datetime import UTC, datetime, timedelta

import pytest

from data.aggregator import Aggregator
from data.models import Bar, Level1Quote, MarketSnapshot, Timeframe
from data.provider.base import DataProvider


class _FiveMinuteProvider(DataProvider):
    def __init__(self) -> None:
        self.earliest_timeframes: list[Timeframe] = []
        self.latest_timeframes: list[Timeframe] = []

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
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
        self.earliest_timeframes.append(timeframe)
        return None

    def latest_history_end(
        self,
        timeframe: Timeframe,
        end: datetime,
    ) -> datetime:
        self.latest_timeframes.append(timeframe)
        return end

    def native_timeframes(self) -> list[Timeframe]:
        return [Timeframe.MIN5, Timeframe.DAILY, Timeframe.WEEKLY]


def test_provider_history_limits_receive_selected_native_timeframe() -> None:
    provider = _FiveMinuteProvider()
    aggregator = Aggregator(provider)
    end = datetime(2026, 11, 4, 15, 7, 12, tzinfo=UTC)

    aggregator.earliest_history_start(Timeframe.MIN65, end)
    aggregator.latest_history_end(Timeframe.MIN65, end)

    assert provider.earliest_timeframes == [Timeframe.MIN5]
    assert provider.latest_timeframes == [Timeframe.MIN5]


@pytest.mark.parametrize(
    ("timeframe", "endpoint", "expected"),
    [
        (
            Timeframe.MIN5,
            datetime(2026, 11, 4, 15, 7, 12, tzinfo=UTC),
            datetime(2026, 11, 4, 15, 5, tzinfo=UTC),
        ),
        (
            Timeframe.MIN5,
            datetime(2026, 11, 4, 8, 59, 50, tzinfo=UTC),
            datetime(2026, 11, 4, 8, 55, tzinfo=UTC),
        ),
        (
            Timeframe.DAILY,
            datetime(2026, 11, 4, 15, 7, 12, tzinfo=UTC),
            datetime(2026, 11, 4, 5, 0, tzinfo=UTC),
        ),
        (
            Timeframe.DAILY,
            datetime(2026, 7, 8, 15, 7, 12, tzinfo=UTC),
            datetime(2026, 7, 8, 4, 0, tzinfo=UTC),
        ),
        (
            Timeframe.WEEKLY,
            datetime(2026, 11, 4, 15, 7, 12, tzinfo=UTC),
            datetime(2026, 11, 2, 5, 0, tzinfo=UTC),
        ),
        (
            Timeframe.WEEKLY,
            datetime(2026, 7, 8, 15, 7, 12, tzinfo=UTC),
            datetime(2026, 7, 6, 4, 0, tzinfo=UTC),
        ),
    ],
)
def test_completed_history_end_uses_exchange_interval_boundary(
    timeframe: Timeframe,
    endpoint: datetime,
    expected: datetime,
) -> None:
    aggregator = Aggregator(_FiveMinuteProvider())

    assert aggregator.completed_history_end(timeframe, endpoint) == expected


@pytest.mark.parametrize(
    ("timeframe", "watermark", "expected"),
    [
        (
            Timeframe.MIN65,
            datetime(2026, 7, 6, 6, 0, tzinfo=UTC),
            datetime(2026, 7, 6, 5, 55, tzinfo=UTC),
        ),
        (
            Timeframe.MIN65,
            datetime(2026, 11, 9, 7, 0, tzinfo=UTC),
            datetime(2026, 11, 9, 6, 55, tzinfo=UTC),
        ),
        (
            Timeframe.MIN39,
            datetime(2026, 11, 9, 7, 0, tzinfo=UTC),
            datetime(2026, 11, 9, 6, 42, tzinfo=UTC),
        ),
        (
            Timeframe.MIN65,
            datetime(2026, 11, 9, 5, 10, tzinfo=UTC),
            datetime(2026, 11, 9, 5, 0, tzinfo=UTC),
        ),
        (
            Timeframe.MIN39,
            datetime(2026, 11, 9, 5, 10, tzinfo=UTC),
            datetime(2026, 11, 9, 5, 0, tzinfo=UTC),
        ),
    ],
)
def test_synthesized_refresh_start_matches_negative_session_bucket(
    timeframe: Timeframe,
    watermark: datetime,
    expected: datetime,
) -> None:
    aggregator = Aggregator(_FiveMinuteProvider())

    assert aggregator.refresh_start(timeframe, watermark) == expected


def test_native_refresh_starts_at_exclusive_watermark() -> None:
    aggregator = Aggregator(_FiveMinuteProvider())
    watermark = datetime(2026, 11, 4, 15, 5, tzinfo=UTC)

    assert aggregator.refresh_start(Timeframe.MIN5, watermark) == watermark


def test_synthesize_cached_bars_uses_existing_grouping_rules() -> None:
    aggregator = Aggregator(_FiveMinuteProvider())
    start = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
    bars = [
        Bar(
            timestamp=start + timedelta(minutes=offset),
            open=100.0 + offset,
            high=102.0 + offset,
            low=99.0 + offset,
            close=101.0 + offset,
            volume=100,
        )
        for offset in range(0, 65, 5)
    ]

    result = aggregator.synthesize_cached_bars(bars, Timeframe.MIN65)

    assert len(result) == 1
    assert result[0].timestamp == start
    assert result[0].open == 100.0
    assert result[0].close == 161.0
    assert result[0].volume == 1_300


class _PagedProvider(_FiveMinuteProvider):
    def __init__(self) -> None:
        super().__init__()
        self.page_span_timeframes: list[Timeframe] = []

    def history_page_span(self, timeframe: Timeframe) -> timedelta | None:
        self.page_span_timeframes.append(timeframe)
        return timedelta(days=30)


def test_history_page_span_uses_fetched_native_timeframe() -> None:
    provider = _PagedProvider()
    aggregator = Aggregator(provider)

    assert aggregator.history_page_span(Timeframe.MIN65) == timedelta(days=30)
    assert aggregator.history_page_span(Timeframe.DAILY) == timedelta(days=30)
    assert provider.page_span_timeframes == [Timeframe.MIN5, Timeframe.DAILY]


def test_history_page_span_defaults_to_single_request() -> None:
    assert Aggregator(_FiveMinuteProvider()).history_page_span(Timeframe.MIN5) is None
