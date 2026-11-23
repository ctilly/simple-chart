from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from data.provider.symbol_search import search_symbols


class _SearchThread(QThread):
    result = pyqtSignal(int, object)

    def __init__(self, query: str, generation: int) -> None:
        super().__init__()
        self._query = query
        self._generation = generation

    def run(self) -> None:
        if self.isInterruptionRequested():
            return
        matches = search_symbols(self._query)
        if not self.isInterruptionRequested():
            self.result.emit(self._generation, matches)


class SymbolSearch(QObject):
    results = pyqtSignal(object)
    idle = pyqtSignal()

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._generation = 0
        self._query = ""
        self._pending = False
        self._thread: _SearchThread | None = None
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._on_debounce)

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def edit(self, text: str) -> None:
        self.cancel()
        self._query = text.strip()
        if len(self._query) >= 2:
            self._debounce.start()

    def cancel(self) -> None:
        self._generation += 1
        self._pending = False
        self._debounce.stop()
        if self._thread is not None:
            self._thread.requestInterruption()

    def _on_debounce(self) -> None:
        self._pending = True
        self._start_pending()

    def _start_pending(self) -> None:
        if not self._pending or self._thread is not None:
            return
        self._pending = False
        thread = _SearchThread(self._query, self._generation)
        self._thread = thread
        thread.result.connect(self._on_result)
        thread.finished.connect(self._on_finished)
        thread.start()

    def _on_result(self, generation: int, matches: object) -> None:
        if generation == self._generation:
            self.results.emit(matches)

    def _on_finished(self) -> None:
        thread = self._thread
        assert thread is not None
        self._thread = None
        thread.deleteLater()
        self._start_pending()
        if self._thread is None:
            self.idle.emit()
