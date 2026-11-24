from calendar import FRIDAY, SATURDAY, SUNDAY
from collections.abc import Callable
from datetime import date, timedelta


_QUARTER_MONTHS = (3, 6, 9, 12)
_ONE_DAY = timedelta(days=1)


def _good_friday(year: int) -> date:
    # Gregorian Easter computus (Meeus/Jones/Butcher).
    moon_cycle = year % 19
    century, year_in_century = divmod(year, 100)
    century_leap_cycles, century_remainder = divmod(century, 4)
    lunar_correction = (century - (century + 8) // 25 + 1) // 3
    epact = (19 * moon_cycle + century - century_leap_cycles - lunar_correction + 15) % 30
    leap_cycles, year_remainder = divmod(year_in_century, 4)
    weekday_offset = (32 + 2 * century_remainder + 2 * leap_cycles - epact - year_remainder) % 7
    correction = (moon_cycle + 11 * epact + 22 * weekday_offset) // 451
    month, day_offset = divmod(epact + weekday_offset - 7 * correction + 114, 31)
    return date(year, month, day_offset + 1) - timedelta(days=2)


def _is_observed_juneteenth(day: date) -> bool:
    if day.year < 2022 or day.month != 6:
        return False
    holiday = date(day.year, 6, 19)
    if holiday.weekday() == SATURDAY:
        holiday -= _ONE_DAY
    elif holiday.weekday() == SUNDAY:
        holiday += _ONE_DAY
    return day == holiday


def _third_friday(year: int, month: int) -> date:
    fifteenth = date(year, month, 15)
    return fifteenth + timedelta(days=(FRIDAY - fifteenth.weekday()) % 7)


def _next_quarterly_date(today: date, event_date: Callable[[date], date]) -> date:
    for month in _QUARTER_MONTHS:
        candidate = event_date(_third_friday(today.year, month))
        if candidate >= today:
            return candidate
    return event_date(_third_friday(today.year + 1, _QUARTER_MONTHS[0]))


# Cboe 2026 and OCC 2027 options expiration calendars. June is holiday-adjusted.
# https://www.optionseducation.org/referencelibrary/expiration-calendar
def _witching_date(third_friday: date) -> date:
    if _is_observed_juneteenth(third_friday) or (
        third_friday.month == 3 and third_friday == _good_friday(third_friday.year)
    ):
        return third_friday - _ONE_DAY
    return third_friday


def next_witching_date(today: date) -> date:
    return _next_quarterly_date(today, _witching_date)


# Nasdaq-100 methodology schedules changes at the first market open after
# the third Friday. The dates are scheduled effective dates, subject to change.
# https://indexes.nasdaq.com/docs/Methodology_NDX.pdf
def _qqq_rebalance_date(third_friday: date) -> date:
    monday = third_friday + timedelta(days=3)
    if _is_observed_juneteenth(monday):
        return monday + _ONE_DAY
    return monday


def next_qqq_rebalance_date(today: date) -> date:
    return _next_quarterly_date(today, _qqq_rebalance_date)
