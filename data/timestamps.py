"""
data/timestamps.py

UTC millisecond timestamps to datetimes, on every platform:
datetime.fromtimestamp rejects times before 1970 on Windows.
"""

from datetime import datetime, timedelta, timezone

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def ms_to_datetime(timestamp_ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=timestamp_ms)
