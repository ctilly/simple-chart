from threading import Event

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from app.background_job import LatestJob
from data.models import SymbolMatch
from data.provider.routing import CapabilityRouter
from data.provider.spec import Capability


class SymbolSearch(QObject):
    results = pyqtSignal(object)
    idle = pyqtSignal()

    def __init__(self, router: CapabilityRouter, parent: QObject) -> None:
        super().__init__(parent)
        self._router = router
        self._query = ""
        self._job = LatestJob("Searching for symbols", self)
        self._job.succeeded.connect(self.results.emit)
        self._job.failed.connect(self._on_failure)
        self._job.idle.connect(self.idle.emit)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._on_debounce)

    @property
    def busy(self) -> bool:
        return self._job.busy

    def edit(self, text: str) -> None:
        self.cancel()
        self._query = text.strip()
        if len(self._query) >= 2:
            self._debounce.start()

    def cancel(self) -> None:
        self._debounce.stop()
        self._job.cancel()

    def stop(self) -> None:
        self.cancel()

    def _on_debounce(self) -> None:
        query = self._query
        route = self._router.route(Capability.SEARCH, query)

        def work(cancelled: Event) -> tuple[SymbolMatch, ...]:
            return () if cancelled.is_set() else route.aggregator.search_symbols(query)

        self._job.submit(work)

    def _on_failure(self) -> None:
        self.results.emit(())
