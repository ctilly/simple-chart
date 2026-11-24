from collections.abc import Callable
from datetime import date, datetime, timedelta, tzinfo

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

from app import symbol_details
from app.fund_calendar import next_qqq_rebalance_date, next_witching_date
from data.models import SymbolProfile


_WITCHING_DATES = (
    date(2026, 3, 20), date(2026, 6, 18), date(2026, 9, 18), date(2026, 12, 18),
    date(2027, 3, 19), date(2027, 6, 17), date(2027, 9, 17), date(2027, 12, 17),
)
_QQQ_REBALANCE_DATES = (
    date(2026, 3, 23), date(2026, 6, 22), date(2026, 9, 21), date(2026, 12, 21),
    date(2027, 3, 22), date(2027, 6, 21), date(2027, 9, 20), date(2027, 12, 20),
)


@pytest.mark.parametrize(
    ("next_date", "expected"),
    [(next_witching_date, _WITCHING_DATES), (next_qqq_rebalance_date, _QQQ_REBALANCE_DATES)],
)
def test_2026_2027_calendar_matches_published_dates(
    next_date: Callable[[date], date], expected: tuple[date, ...],
) -> None:
    actual = []
    today = date(2026, 1, 1)
    while (event_date := next_date(today)).year <= 2027:
        actual.append(event_date)
        today = event_date + timedelta(days=1)
    assert tuple(actual) == expected


def test_good_friday_moves_witching_to_thursday() -> None:
    assert next_witching_date(date(2008, 3, 19)) == date(2008, 3, 20)


def test_witching_includes_today() -> None:
    assert next_witching_date(date(2027, 6, 17)) == date(2027, 6, 17)


def test_qqq_skips_monday_juneteenth_after_2027() -> None:
    assert next_qqq_rebalance_date(date(2028, 6, 16)) == date(2028, 6, 20)


def test_qqq_profile_formats_calendar_after_2027(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> "FixedDatetime":
            return cls(2028, 6, 16, tzinfo=tz)

    monkeypatch.setattr(symbol_details, "datetime", FixedDatetime)
    parent = QWidget()
    dialog = symbol_details.SymbolDetailsDialog(parent)
    dialog.reset("QQQ")
    dialog.set_profile(SymbolProfile(is_etf=True), source="Yahoo Finance")

    profile = dialog._profile.toPlainText()
    assert "Next U.S. witching date Jun 16, 2028" in profile
    assert "Next scheduled effective date Jun 20, 2028" in profile
