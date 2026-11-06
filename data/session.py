from datetime import date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

from data.models import Timeframe


MARKET_TIMEZONE = ZoneInfo("America/New_York")
REGULAR_SESSION_OPEN = time(9, 30)
REGULAR_SESSION_CLOSE = time(16, 0)
REGULAR_SESSION_MINUTES = 390
DAILY_SETTLEMENT_DELAY = timedelta(minutes=5)


def is_regular_session_timestamp(timestamp: datetime) -> bool:
    local = timestamp.astimezone(MARKET_TIMEZONE)
    return (
        local.weekday() < 5
        and REGULAR_SESSION_OPEN <= local.time() < REGULAR_SESSION_CLOSE
    )


def daily_session_is_settled(endpoint: datetime) -> bool:
    local = endpoint.astimezone(MARKET_TIMEZONE)
    settled_at = datetime.combine(
        local.date(),
        REGULAR_SESSION_CLOSE,
        tzinfo=MARKET_TIMEZONE,
    ) + DAILY_SETTLEMENT_DELAY
    return local.weekday() < 5 and local >= settled_at


def latest_expected_regular_session(endpoint: datetime) -> tuple[date, bool]:
    local = endpoint.astimezone(MARKET_TIMEZONE)
    local_time = local.time().replace(tzinfo=None)
    if (
        local.weekday() < 5
        and REGULAR_SESSION_OPEN <= local_time < REGULAR_SESSION_CLOSE
    ):
        return (local.date(), True)
    if local.weekday() < 5 and local_time >= REGULAR_SESSION_CLOSE:
        return (local.date(), False)

    session_date = local.date() - timedelta(days=1)
    while session_date.weekday() >= 5:
        session_date -= timedelta(days=1)
    return (session_date, False)


def expected_last_session_bar_start(
    timeframe: Timeframe,
    session_date: date,
    tz: tzinfo | None,
) -> datetime:
    session_open = datetime.combine(
        session_date,
        REGULAR_SESSION_OPEN,
        tzinfo=MARKET_TIMEZONE,
    )
    if timeframe == Timeframe.DAILY:
        return session_open.replace(hour=0, minute=0).astimezone(tz)
    period_minutes = timeframe.minutes
    if period_minutes is None:
        raise ValueError(f"No regular-session bar boundary for {timeframe!r}.")
    final_period = (REGULAR_SESSION_MINUTES - 1) // period_minutes
    return (
        session_open + timedelta(minutes=final_period * period_minutes)
    ).astimezone(tz)


def regular_session_settled_at(
    timeframe: Timeframe,
    session_date: date,
    tz: tzinfo | None,
) -> datetime:
    boundary_date = (
        session_date + timedelta(days=1)
        if timeframe == Timeframe.DAILY
        else session_date
    )
    boundary_time = (
        time.min
        if timeframe == Timeframe.DAILY
        else REGULAR_SESSION_CLOSE
    )
    return datetime.combine(
        boundary_date,
        boundary_time,
        tzinfo=MARKET_TIMEZONE,
    ).astimezone(tz)


def completed_week_start(endpoint: datetime) -> datetime:
    local = endpoint.astimezone(MARKET_TIMEZONE)
    week_start = local - timedelta(days=local.weekday())
    return week_start.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ).astimezone(endpoint.tzinfo)


def exchange_day_start(timestamp: datetime) -> datetime:
    local = timestamp.astimezone(MARKET_TIMEZONE)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        timestamp.tzinfo
    )


def session_period(
    timestamp: datetime,
    period_minutes: int,
) -> tuple[datetime, int]:
    local = timestamp.astimezone(MARKET_TIMEZONE)
    session_open = local.replace(
        hour=REGULAR_SESSION_OPEN.hour,
        minute=REGULAR_SESSION_OPEN.minute,
        second=0,
        microsecond=0,
    )
    seconds_elapsed = (local - session_open).total_seconds()
    period_index = int(seconds_elapsed // (period_minutes * 60))
    return session_open, period_index


def bucket_start(timestamp: datetime, period_minutes: int) -> datetime:
    session_open, period_index = session_period(timestamp, period_minutes)
    bucket_start = session_open + timedelta(
        minutes=period_index * period_minutes
    )
    day_start = session_open.replace(hour=0, minute=0)
    return max(bucket_start, day_start).astimezone(timestamp.tzinfo)
