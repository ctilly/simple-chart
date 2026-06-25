from datetime import datetime, timezone

from data.calendar import timestamp_ms_to_bar_index


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
