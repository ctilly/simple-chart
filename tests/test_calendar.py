from datetime import date, datetime, timezone

from data.calendar import (
    bar_session_key,
    session_date_anchor,
    timestamp_ms_to_bar_index,
)
from data.models import Timeframe


def test_intraday_bar_session_key_is_the_exact_utc_millisecond() -> None:
    timestamp = datetime(2026, 2, 2, 14, 30, tzinfo=timezone.utc)

    assert bar_session_key(timestamp, Timeframe.MIN15) == _ms(timestamp)


def test_daily_bar_session_key_uses_provider_utc_date() -> None:
    yahoo_midnight = datetime(2026, 2, 2, 0, 0, tzinfo=timezone.utc)
    midnight_et = datetime(2026, 2, 2, 5, 0, tzinfo=timezone.utc)

    assert bar_session_key(yahoo_midnight, Timeframe.DAILY) == date(2026, 2, 2)
    assert bar_session_key(midnight_et, Timeframe.DAILY) == date(2026, 2, 2)


def test_weekly_session_keys_use_the_monday_utc_anchor() -> None:
    yahoo_monday = datetime(2026, 2, 2, 0, 0, tzinfo=timezone.utc)
    alpaca_monday = datetime(2026, 2, 2, 5, 0, tzinfo=timezone.utc)
    sunday = datetime(2026, 2, 8, 15, 0, tzinfo=timezone.utc)

    assert bar_session_key(yahoo_monday, Timeframe.WEEKLY) == date(2026, 2, 2)
    assert bar_session_key(alpaca_monday, Timeframe.WEEKLY) == date(2026, 2, 2)
    assert bar_session_key(sunday, Timeframe.WEEKLY) == date(2026, 2, 2)
    assert session_date_anchor(date(2026, 2, 5), Timeframe.WEEKLY) == date(
        2026,
        2,
        2,
    )


def test_timestamp_ms_to_bar_index_returns_containing_bar() -> None:
    timestamps = [
        _ms(datetime(2026, 6, 12, 4, tzinfo=timezone.utc)),
        _ms(datetime(2026, 6, 15, 4, tzinfo=timezone.utc)),
    ]
    anchor = _ms(datetime(2026, 6, 12, 15, 45, tzinfo=timezone.utc))

    assert timestamp_ms_to_bar_index(anchor, timestamps) == 0


def test_timestamp_ms_to_bar_index_keeps_exact_bar_boundary() -> None:
    timestamps = [
        _ms(datetime(2026, 6, 12, 4, tzinfo=timezone.utc)),
        _ms(datetime(2026, 6, 15, 4, tzinfo=timezone.utc)),
    ]

    assert timestamp_ms_to_bar_index(timestamps[1], timestamps) == 1


def test_timestamp_ms_to_bar_index_returns_latest_bar_for_current_interval() -> None:
    timestamps = [
        _ms(datetime(2026, 6, 12, 4, tzinfo=timezone.utc)),
        _ms(datetime(2026, 6, 15, 4, tzinfo=timezone.utc)),
    ]
    anchor = _ms(datetime(2026, 6, 15, 15, 45, tzinfo=timezone.utc))

    assert timestamp_ms_to_bar_index(anchor, timestamps) == 1


def test_timestamp_ms_to_bar_index_keeps_distant_future_not_drawable() -> None:
    timestamps = [
        _ms(datetime(2026, 6, 12, 4, tzinfo=timezone.utc)),
        _ms(datetime(2026, 6, 15, 4, tzinfo=timezone.utc)),
    ]
    anchor = _ms(datetime(2026, 6, 18, 4, tzinfo=timezone.utc))

    assert timestamp_ms_to_bar_index(anchor, timestamps) == 2


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)
