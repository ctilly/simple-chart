from datetime import date
from threading import Event

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from data.aggregator import Aggregator


HistoricalEventDates = tuple[tuple[date, ...], tuple[date, ...]]


class EarningsWorker(QObject):
    result = pyqtSignal(int, object)
    history_result = pyqtSignal(int, object)
    completed = pyqtSignal()

    def __init__(
        self, aggregator: Aggregator, symbol: str, generation: int,
        visibility: tuple[bool, bool], cancelled: Event,
    ) -> None:
        super().__init__()
        self._aggregator = aggregator
        self._symbol = symbol
        self._generation = generation
        self._visibility = visibility
        self._cancelled = cancelled

    @pyqtSlot()
    def run(self) -> None:
        try:
            if self._cancelled.is_set():
                return
            dates = self._aggregator.fetch_upcoming_earnings(self._symbol)
            self.result.emit(self._generation, dates)
            earnings, dividends = self._visibility
            history: HistoricalEventDates = (
                self._aggregator.fetch_past_earnings(self._symbol)
                if earnings and not self._cancelled.is_set() else (),
                self._aggregator.fetch_dividend_dates(self._symbol)
                if dividends and not self._cancelled.is_set() else (),
            )
            if (earnings or dividends) and not self._cancelled.is_set():
                self.history_result.emit(self._generation, history)
        finally:
            self.completed.emit()
