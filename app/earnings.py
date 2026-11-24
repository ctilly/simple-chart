from datetime import date, datetime
from threading import Event
from typing import cast

from PyQt6.QtCore import QObject, pyqtSignal

from app.background_job import LatestJob
from data.provider.routing import CapabilityRouter, DataRoute
from data.provider.spec import Capability
from data.session import MARKET_TIMEZONE


HistoricalEventDates = tuple[tuple[date, ...], tuple[date, ...]]


def _format_upcoming_earnings(dates: tuple[date, date], today: date) -> tuple[str, bool]:
    first, last = dates
    label = first.strftime("%b %d").replace(" 0", " ")
    if first != last:
        end = (
            str(last.day) if first.month == last.month and first.year == last.year
            else last.strftime("%b %d").replace(" 0", " ")
        )
        label = f"{label}–{end}"
    return label, 0 <= (first - today).days <= 5


class EarningsFeed(QObject):
    upcoming_changed = pyqtSignal(str, bool, str)
    history_changed = pyqtSignal()
    idle = pyqtSignal()

    def __init__(self, router: CapabilityRouter, parent: QObject) -> None:
        super().__init__(parent)
        self._router = router
        self._upcoming_key: tuple[DataRoute, str] | None = None
        self._history_routes: tuple[DataRoute, DataRoute] | None = None
        self._symbol: str | None = None
        self._visibility = (False, False)
        self._history: HistoricalEventDates = ((), ())
        self._upcoming_job = LatestJob("Loading upcoming earnings", self)
        self._upcoming_job.succeeded.connect(self._on_upcoming)
        self._upcoming_job.idle.connect(self._on_idle)
        self._history_job = LatestJob("Loading historical chart events", self)
        self._history_job.succeeded.connect(self._on_history)
        self._history_job.failed.connect(self._on_history)
        self._history_job.idle.connect(self._on_idle)

    @property
    def busy(self) -> bool:
        return self._upcoming_job.busy or self._history_job.busy

    @property
    def symbol(self) -> str | None:
        return self._symbol

    @property
    def visibility(self) -> tuple[bool, bool]:
        return self._visibility

    @property
    def history(self) -> HistoricalEventDates:
        return self._history

    def select(self, symbol: str, visibility: tuple[bool, bool]) -> None:
        upcoming_route = self._router.route(Capability.UPCOMING_EARNINGS, symbol)
        earnings_route = self._router.route(Capability.PAST_EARNINGS, symbol)
        dividends_route = self._router.route(Capability.DIVIDENDS, symbol)
        symbol_changed = symbol != self._symbol
        visibility_changed = visibility != self._visibility
        history_routes = (earnings_route, dividends_route)
        history_changed = symbol_changed or history_routes != self._history_routes
        if history_changed:
            previous_earnings, previous_dividends = self._history_routes or (None, None)
            self._history = (
                self._history[0] if not symbol_changed and earnings_route is previous_earnings else (),
                self._history[1] if not symbol_changed and dividends_route is previous_dividends else (),
            )
            self._history_routes = history_routes
        self._symbol = symbol
        self._visibility = visibility

        if (upcoming_route, symbol) != self._upcoming_key:
            self._upcoming_key = (upcoming_route, symbol)

            def load_upcoming(cancelled: Event) -> tuple[date, date] | None:
                return (
                    None if cancelled.is_set()
                    else upcoming_route.aggregator.fetch_upcoming_earnings(symbol)
                )

            self._upcoming_job.submit(load_upcoming)
            self.upcoming_changed.emit("—", False, upcoming_route.spec.display_name)

        if history_changed or visibility_changed:
            earnings, dividends = visibility
            if earnings or dividends:
                def load_history(cancelled: Event) -> HistoricalEventDates:
                    return (
                        earnings_route.aggregator.fetch_past_earnings(symbol)
                        if earnings and not cancelled.is_set() else (),
                        dividends_route.aggregator.fetch_dividend_dates(symbol)
                        if dividends and not cancelled.is_set() else (),
                    )

                self._history_job.submit(load_history)
            else:
                self._history_job.cancel()

        if history_changed:
            self.history_changed.emit()

    def stop(self) -> None:
        self._upcoming_job.cancel()
        self._history_job.cancel()

    def _on_upcoming(self, result: object) -> None:
        dates = cast(tuple[date, date] | None, result)
        if dates is not None and self._upcoming_key is not None:
            label, highlight = _format_upcoming_earnings(dates, datetime.now(MARKET_TIMEZONE).date())
            route, _ = self._upcoming_key
            self.upcoming_changed.emit(label, highlight, route.spec.display_name)

    def _on_history(self, result: object = ((), ())) -> None:
        self._history = cast(HistoricalEventDates, result)
        self.history_changed.emit()

    def _on_idle(self) -> None:
        if not self.busy:
            self.idle.emit()
