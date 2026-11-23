from datetime import date


# Cboe 2026 and OCC 2027 options expiration calendars. June is holiday-adjusted.
# https://www.optionseducation.org/referencelibrary/expiration-calendar
WITCHING_DATES = (
    date(2026, 3, 20), date(2026, 6, 18), date(2026, 9, 18), date(2026, 12, 18),
    date(2027, 3, 19), date(2027, 6, 17), date(2027, 9, 17), date(2027, 12, 17),
)

# Nasdaq-100 methodology schedules changes at the first market open after
# the third Friday. The dates are scheduled effective dates, subject to change.
# https://indexes.nasdaq.com/docs/Methodology_NDX.pdf
QQQ_REBALANCE_DATES = (
    date(2026, 3, 23), date(2026, 6, 22), date(2026, 9, 21), date(2026, 12, 21),
    date(2027, 3, 22), date(2027, 6, 21), date(2027, 9, 20), date(2027, 12, 20),
)


def next_calendar_date(dates: tuple[date, ...], today: date) -> str:
    return next((day.strftime("%b %d, %Y") for day in dates if day >= today), "—")
