from datetime import UTC, date, datetime, timezone
from zoneinfo import ZoneInfo

from app.controller import _is_regular_session_bar
from data.aggregator import Aggregator
from data.models import Bar, Level1Quote, MarketSnapshot, Timeframe
from data.provider.base import DataProvider
from data.session import (
    daily_session_is_settled,
    expected_last_session_bar_start,
    is_regular_session_timestamp,
    latest_expected_regular_session,
    regular_session_settled_at,
)


_ET = ZoneInfo("America/New_York")


class _Provider(DataProvider):
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

    def native_timeframes(self) -> list[Timeframe]:
        return [Timeframe.MIN1]


def _bar(timestamp: datetime, price: float) -> Bar:
    return Bar(timestamp, price, price, price, price, 1)


def test_session_membership_includes_open_excludes_close_and_weekends() -> None:
    samples = [
        (datetime(2026, 1, 5, 9, 29, tzinfo=_ET), False),
        (datetime(2026, 1, 5, 9, 30, tzinfo=_ET), True),
        (datetime(2026, 1, 5, 15, 59, tzinfo=_ET), True),
        (datetime(2026, 1, 5, 16, 0, tzinfo=_ET), False),
        (datetime(2026, 1, 9, 16, 0, tzinfo=_ET), False),
        (datetime(2026, 1, 10, 10, 0, tzinfo=_ET), False),
        (datetime(2026, 1, 12, 9, 29, tzinfo=_ET), False),
    ]

    for timestamp, expected in samples:
        assert is_regular_session_timestamp(timestamp) is expected
        assert _is_regular_session_bar(_bar(timestamp, 1.0)) is expected


def test_latest_expected_regular_session_handles_weekend_edges() -> None:
    cases = [
        (datetime(2026, 1, 9, 16, 0, tzinfo=_ET), (date(2026, 1, 9), False)),
        (datetime(2026, 1, 10, 10, 0, tzinfo=_ET), (date(2026, 1, 9), False)),
        (datetime(2026, 1, 12, 9, 29, tzinfo=_ET), (date(2026, 1, 9), False)),
        (datetime(2026, 1, 12, 9, 30, tzinfo=_ET), (date(2026, 1, 12), True)),
    ]

    for endpoint, expected in cases:
        assert latest_expected_regular_session(endpoint) == expected


def test_daily_session_settlement_eligibility_boundaries() -> None:
    cases = [
        (datetime(2026, 1, 5, 16, 0, tzinfo=_ET), False),
        (datetime(2026, 1, 5, 16, 4, 59, tzinfo=_ET), False),
        (datetime(2026, 1, 5, 16, 5, tzinfo=_ET), True),
        (datetime(2026, 1, 10, 16, 5, tzinfo=_ET), False),
    ]

    for endpoint, expected in cases:
        assert daily_session_is_settled(endpoint) is expected


def test_session_boundaries_return_input_timezone_across_dst() -> None:
    for session_date, expected_last_open, expected_next_midnight in (
        (
            date(2026, 3, 6),
            datetime(2026, 3, 6, 20, 55, tzinfo=UTC),
            datetime(2026, 3, 7, 5, 0, tzinfo=UTC),
        ),
        (
            date(2026, 3, 9),
            datetime(2026, 3, 9, 19, 55, tzinfo=UTC),
            datetime(2026, 3, 10, 4, 0, tzinfo=UTC),
        ),
    ):
        utc_open = expected_last_session_bar_start(
            Timeframe.MIN5,
            session_date,
            timezone.utc,
        )
        et_open = expected_last_session_bar_start(
            Timeframe.MIN5,
            session_date,
            _ET,
        )
        utc_settled = regular_session_settled_at(
            Timeframe.DAILY,
            session_date,
            timezone.utc,
        )
        et_settled = regular_session_settled_at(
            Timeframe.DAILY,
            session_date,
            _ET,
        )

        assert utc_open == expected_last_open
        assert utc_open.tzinfo is timezone.utc
        assert et_open.tzinfo is _ET
        assert et_open.astimezone(UTC) == utc_open
        assert utc_settled == expected_next_midnight
        assert utc_settled.tzinfo is timezone.utc
        assert et_settled.tzinfo is _ET
        assert et_settled.astimezone(UTC) == utc_settled


def test_synthesized_futures_keep_weekend_and_preopen_buckets() -> None:
    aggregator = Aggregator(_Provider())
    bars = [
        _bar(datetime(2026, 1, 10, 13, 0, tzinfo=UTC), 1.0),
        _bar(datetime(2026, 1, 10, 13, 5, tzinfo=UTC), 2.0),
        _bar(datetime(2026, 1, 12, 13, 0, tzinfo=UTC), 3.0),
        _bar(datetime(2026, 1, 12, 13, 5, tzinfo=UTC), 4.0),
    ]

    result = aggregator.synthesize_cached_bars(bars, Timeframe.MIN39)

    assert [bar.timestamp for bar in result] == [
        datetime(2026, 1, 10, 13, 0, tzinfo=UTC),
        datetime(2026, 1, 12, 13, 0, tzinfo=UTC),
    ]
