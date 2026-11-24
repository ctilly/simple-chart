import logging
from collections.abc import Callable
from threading import Event

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from app.errors import ErrorContext, report_error

_LOGGER = logging.getLogger(__name__)


class _JobThread(QThread):
    def __init__(
        self, work: Callable[[Event], object], generation: int, context: ErrorContext
    ) -> None:
        super().__init__()
        self._work = work
        self._context = context
        self.generation = generation
        self.cancellation = Event()
        self.result: object = None
        self.failed = False

    def run(self) -> None:
        try:
            self.result = self._work(self.cancellation)
        except Exception as exc:
            self.failed = True
            if not self.cancellation.is_set():
                report_error(_LOGGER, exc, self._context)


class LatestJob(QObject):
    """Run one request at a time and deliver results to the GUI thread."""

    succeeded = pyqtSignal(object)
    failed = pyqtSignal()
    idle = pyqtSignal()

    def __init__(self, action: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._context = ErrorContext(action)
        self._generation = 0
        self._pending: Callable[[Event], object] | None = None
        self._thread: _JobThread | None = None
        self._last_started: _JobThread | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None or self._pending is not None

    def submit(self, work: Callable[[Event], object]) -> None:
        self._generation += 1
        if self._thread is not None:
            self._thread.cancellation.set()
        self._pending = work
        self._start_pending()

    def cancel(self) -> None:
        self._generation += 1
        self._pending = None
        if self._thread is not None:
            self._thread.cancellation.set()

    def _start_pending(self) -> None:
        if self._thread is not None or self._pending is None:
            return
        thread = _JobThread(self._pending, self._generation, self._context)
        self._pending = None
        self._thread = thread
        self._last_started = thread
        thread.finished.connect(self._on_finished)
        thread.start()

    @pyqtSlot()
    def _on_finished(self) -> None:
        thread = self._thread
        assert thread is not None
        self._thread = None
        thread.deleteLater()
        if thread.generation == self._generation and not thread.cancellation.is_set():
            if thread.failed:
                self.failed.emit()
            else:
                self.succeeded.emit(thread.result)
        self._start_pending()
        # A result handler can start and drain a new job in a nested event loop.
        if not self.busy and self._last_started is thread:
            self._last_started = None
            self.idle.emit()
