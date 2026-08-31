import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from PyQt6.QtCore import QDate, QObject, QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QApplication,
)

from app.bar_comparison import (
    BarComparisonResult,
    BarComparisonRow,
    BarComparisonRowKind,
    BarField,
)
from app.dialogs import show_information, show_warning
from data.cache import Cache
from data.calendar import MARKET_TIMEZONE
from data.models import Bar, BarCorrection, BarInspection, Timeframe


_CHART_TIMEFRAMES = (
    Timeframe.MIN5,
    Timeframe.MIN15,
    Timeframe.MIN30,
    Timeframe.MIN39,
    Timeframe.MIN65,
    Timeframe.DAILY,
    Timeframe.WEEKLY,
)
_SOURCE_LABELS = {
    "yfinance": "Yahoo Finance",
    "alpaca:iex": "Alpaca / IEX",
    "alpaca:delayed_sip": "Alpaca / SIP (15-minute delayed)",
    "alpaca:sip": "Alpaca / SIP (real-time)",
}
_INSPECTION_ROLE = int(Qt.ItemDataRole.UserRole)
_COMPARISON_ROW_ROLE = _INSPECTION_ROLE + 1
_COMPARISON_FIELDS: dict[int, BarField] = {
    1: "open",
    2: "high",
    3: "low",
    4: "close",
    5: "volume",
}
_SYNTHESIZED_TIMEFRAMES = (Timeframe.MIN39, Timeframe.MIN65)
_PROVIDER_REVISION_COLOR = QColor("#fff3cd")
_CORROBORATION_COLOR = QColor("#d9ead3")


class BarComparisonServiceLike(Protocol):
    def compare(
        self,
        origin_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        cached_bar: Bar,
        should_cancel: Callable[[], bool] = ...,
    ) -> BarComparisonResult: ...


@dataclass(frozen=True)
class _ComparisonKey:
    cache_namespace: str
    symbol: str
    timeframe: Timeframe
    timestamp: datetime


@dataclass(frozen=True)
class _ComparisonRequest:
    key: _ComparisonKey
    cached_bar: Bar


class _ComparisonWorker(QObject):
    completed: pyqtSignal = pyqtSignal(object, object)
    failed: pyqtSignal = pyqtSignal(object, str)

    def __init__(
        self,
        service: BarComparisonServiceLike,
        request: _ComparisonRequest,
    ) -> None:
        super().__init__()
        self._service = service
        self._request = request

    @pyqtSlot()
    def run(self) -> None:
        try:
            thread = QThread.currentThread()
            assert thread is not None
            result = self._service.compare(
                self._request.key.cache_namespace,
                self._request.key.symbol,
                self._request.key.timeframe,
                self._request.cached_bar,
                should_cancel=thread.isInterruptionRequested,
            )
        except Exception:
            self.failed.emit(self._request, "The source comparison failed.")
            return
        self.completed.emit(self._request, result)


class _ComparisonThreadOwner(QObject):
    def __init__(self, application: QApplication) -> None:
        super().__init__(application)
        self._tasks: dict[QThread, _ComparisonWorker] = {}
        application.aboutToQuit.connect(self.shutdown)

    def adopt(self, thread: QThread, worker: _ComparisonWorker) -> None:
        thread.setParent(self)
        self._tasks[thread] = worker
        thread.finished.connect(self._release_finished_thread)

    def active_count(self) -> int:
        return len(self._tasks)

    @pyqtSlot()
    def shutdown(self) -> None:
        threads = tuple(self._tasks)
        for thread in threads:
            if thread.isRunning():
                thread.requestInterruption()
                thread.quit()
        for thread in threads:
            if thread.isRunning():
                thread.wait()
        self._tasks.clear()

    @pyqtSlot()
    def _release_finished_thread(self) -> None:
        thread = self.sender()
        if isinstance(thread, QThread):
            self._tasks.pop(thread, None)


_COMPARISON_THREAD_OWNER: _ComparisonThreadOwner | None = None


def _comparison_thread_owner() -> _ComparisonThreadOwner:
    global _COMPARISON_THREAD_OWNER
    application = QApplication.instance()
    if not isinstance(application, QApplication):
        raise RuntimeError("Source comparison requires a running application.")
    if _COMPARISON_THREAD_OWNER is None:
        _COMPARISON_THREAD_OWNER = _ComparisonThreadOwner(application)
    return _COMPARISON_THREAD_OWNER


class DataQualityTab(QWidget):
    bars_changed: pyqtSignal = pyqtSignal()

    def __init__(
        self,
        cache: Cache,
        parent: QWidget | None = None,
        *,
        comparison_service: BarComparisonServiceLike | None = None,
        initial_cache_namespace: str | None = None,
        initial_symbol: str | None = None,
        initial_timeframe: Timeframe | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("dataQualityTab")
        self._cache = cache
        self._comparison_service = comparison_service
        self._selected_inspection: BarInspection | None = None
        self._selected_deviation: float | None = None
        self._displayed_prices: dict[str, float] = {}
        self._comparison_cache: dict[_ComparisonKey, BarComparisonResult] = {}
        self._displayed_comparison_result: BarComparisonResult | None = None
        self._comparison_thread: QThread | None = None
        self._comparison_worker: _ComparisonWorker | None = None
        self._accept_comparison_results = True

        layout = QVBoxLayout(self)
        layout.addLayout(
            self._build_scope_controls(
                initial_cache_namespace,
                initial_symbol,
                initial_timeframe,
            )
        )
        layout.addLayout(self._build_search_controls())
        self._bars = self._build_results_table()
        layout.addWidget(self._bars, 1)
        layout.addWidget(self._build_editor())
        layout.addWidget(self._build_comparison(), 1)
        self._clear_editor()

    def _build_scope_controls(
        self,
        initial_cache_namespace: str | None,
        initial_symbol: str | None,
        initial_timeframe: Timeframe | None,
    ) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Data source", self))
        self._source = QComboBox(self)
        self._source.setObjectName("dataQualitySource")
        namespaces = set(self._cache.get_bar_cache_namespaces())
        if initial_cache_namespace is not None:
            namespaces.add(initial_cache_namespace)
        for namespace in sorted(namespaces):
            self._source.addItem(_source_label(namespace), namespace)
        if initial_cache_namespace is not None:
            self._source.setCurrentIndex(
                self._source.findData(initial_cache_namespace)
            )
        row.addWidget(self._source, 2)

        row.addWidget(QLabel("Symbol", self))
        self._symbol = QLineEdit(self)
        self._symbol.setObjectName("dataQualitySymbol")
        self._symbol.setMaximumWidth(120)
        self._symbol.setText("" if initial_symbol is None else initial_symbol)
        row.addWidget(self._symbol)

        row.addWidget(QLabel("Timeframe", self))
        self._timeframe = QComboBox(self)
        self._timeframe.setObjectName("dataQualityTimeframe")
        for timeframe in _CHART_TIMEFRAMES:
            self._timeframe.addItem(timeframe.value, timeframe)
        selected_timeframe = initial_timeframe or Timeframe.DAILY
        self._timeframe.setCurrentIndex(
            self._timeframe.findData(selected_timeframe)
        )
        row.addWidget(self._timeframe)
        return row

    def _build_search_controls(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Minimum deviation", self))
        self._deviation = QDoubleSpinBox(self)
        self._deviation.setObjectName("dataQualityDeviation")
        self._deviation.setRange(0.01, 1_000_000.0)
        self._deviation.setDecimals(2)
        self._deviation.setSuffix("%")
        self._deviation.setValue(100.0)
        row.addWidget(self._deviation)

        find_button = QPushButton("Find suspicious bars", self)
        find_button.setObjectName("findSuspiciousBars")
        find_button.clicked.connect(self._find_suspicious_bars)
        row.addWidget(find_button)
        row.addSpacing(20)

        row.addWidget(QLabel("Date", self))
        self._date = QDateEdit(QDate.currentDate(), self)
        self._date.setObjectName("dataQualityDate")
        self._date.setCalendarPopup(True)
        self._date.setDisplayFormat("yyyy-MM-dd")
        row.addWidget(self._date)

        load_button = QPushButton("Load date", self)
        load_button.setObjectName("loadBarsForDate")
        load_button.clicked.connect(self._load_date)
        row.addWidget(load_button)
        row.addStretch(1)
        return row

    def _build_results_table(self) -> QTableWidget:
        table = QTableWidget(self)
        table.setObjectName("dataQualityBars")
        table.setColumnCount(9)
        table.setHorizontalHeaderLabels(
            [
                "Date",
                "Time",
                "Open",
                "High",
                "Low",
                "Close",
                "Volume",
                "Deviation",
                "Status",
            ]
        )
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        vertical_header = table.verticalHeader()
        horizontal_header = table.horizontalHeader()
        assert vertical_header is not None
        assert horizontal_header is not None
        vertical_header.setVisible(False)
        horizontal_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        horizontal_header.setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        table.itemSelectionChanged.connect(self._select_result)
        return table

    def _build_editor(self) -> QGroupBox:
        group = QGroupBox("Selected bar", self)
        grid = QGridLayout(group)
        headings = ("Open", "High", "Low", "Close", "Volume")
        for column, heading in enumerate(headings, start=1):
            grid.addWidget(QLabel(heading, group), 0, column)
        grid.addWidget(QLabel("Provider", group), 1, 0)
        self._raw_fields: list[QLabel] = []
        for column, name in enumerate(
            ("Open", "High", "Low", "Close", "Volume"),
            start=1,
        ):
            label = QLabel("-", group)
            label.setObjectName(f"dataQualityRaw{name}")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._raw_fields.append(label)
            grid.addWidget(label, 1, column)

        grid.addWidget(QLabel("Corrected", group), 2, 0)
        self._open = _price_editor("dataQualityOpen", group)
        self._high = _price_editor("dataQualityHigh", group)
        self._low = _price_editor("dataQualityLow", group)
        self._close = _price_editor("dataQualityClose", group)
        self._volume = QLineEdit(group)
        self._volume.setObjectName("dataQualityVolume")
        for column, editor in enumerate(
            (self._open, self._high, self._low, self._close, self._volume),
            start=1,
        ):
            grid.addWidget(editor, 2, column)

        self._correction_status = QLabel("", group)
        self._correction_status.setObjectName("dataQualityCorrectionStatus")
        grid.addWidget(self._correction_status, 3, 0, 1, 4)

        buttons = QHBoxLayout()
        self._apply_button = QPushButton("Apply correction", group)
        self._apply_button.setObjectName("applyBarCorrection")
        self._apply_button.clicked.connect(self._apply_correction)
        buttons.addWidget(self._apply_button)
        self._restore_button = QPushButton("Restore provider values", group)
        self._restore_button.setObjectName("restoreProviderBar")
        self._restore_button.clicked.connect(self._restore_provider_bar)
        buttons.addWidget(self._restore_button)
        grid.addLayout(buttons, 3, 4, 1, 2)
        return group

    def _build_comparison(self) -> QGroupBox:
        group = QGroupBox("Compare sources", self)
        layout = QVBoxLayout(group)

        self._comparison_message = QLabel("Select a bar to compare sources.", group)
        self._comparison_message.setObjectName("dataQualityComparisonMessage")
        self._comparison_message.setWordWrap(True)
        layout.addWidget(self._comparison_message)

        self._comparison_table = QTableWidget(group)
        self._comparison_table.setObjectName("dataQualityComparisonTable")
        self._comparison_table.setColumnCount(7)
        self._comparison_table.setHorizontalHeaderLabels(
            ["Source", "Open", "High", "Low", "Close", "Volume", "Status"]
        )
        self._comparison_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self._comparison_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectItems
        )
        self._comparison_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        comparison_header = self._comparison_table.horizontalHeader()
        assert comparison_header is not None
        comparison_header.setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        comparison_header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self._comparison_table.currentCellChanged.connect(
            self._update_copy_button
        )
        layout.addWidget(self._comparison_table)

        buttons = QHBoxLayout()
        self._compare_button = QPushButton("Compare sources", group)
        self._compare_button.setObjectName("compareBarSources")
        self._compare_button.clicked.connect(self._compare_selected_bar)
        buttons.addWidget(self._compare_button)
        self._copy_comparison_button = QPushButton("Use selected value", group)
        self._copy_comparison_button.setObjectName("useComparisonValue")
        self._copy_comparison_button.clicked.connect(
            self._copy_selected_comparison_value
        )
        self._copy_comparison_button.setEnabled(False)
        buttons.addWidget(self._copy_comparison_button)
        self._refresh_provider_button = QPushButton(
            "Refresh provider bar",
            group,
        )
        self._refresh_provider_button.setObjectName("refreshProviderBar")
        self._refresh_provider_button.clicked.connect(
            self._refresh_provider_bar
        )
        self._refresh_provider_button.setEnabled(False)
        buttons.addWidget(self._refresh_provider_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return group

    def _find_suspicious_bars(self) -> None:
        scope = self._scope()
        if scope is None:
            return
        namespace, symbol, timeframe = scope
        candidates = self._cache.find_suspicious_bars(
            namespace,
            symbol,
            timeframe,
            self._deviation.value(),
        )
        self._set_results(
            [
                (candidate.inspection, candidate.deviation_percent)
                for candidate in candidates
            ]
        )

    def _load_date(self) -> None:
        scope = self._scope()
        if scope is None:
            return
        namespace, symbol, timeframe = scope
        inspections = self._cache.get_bar_inspections_for_date(
            namespace,
            symbol,
            timeframe,
            self._date.date().toPyDate(),
        )
        self._set_results([(inspection, None) for inspection in inspections])

    def _scope(self) -> tuple[str, str, Timeframe] | None:
        namespace = self._source.currentData()
        if not isinstance(namespace, str):
            show_warning(self, "No Data Source", "No cached data source is available.")
            return None
        symbol = self._symbol.text().strip().upper()
        if not symbol:
            show_warning(self, "Symbol Required", "Enter a symbol to inspect.")
            return None
        self._symbol.setText(symbol)
        timeframe = self._timeframe.currentData()
        if not isinstance(timeframe, Timeframe):
            raise RuntimeError("No data-quality timeframe is selected.")
        return (namespace, symbol, timeframe)

    def _set_results(
        self,
        results: list[tuple[BarInspection, float | None]],
    ) -> None:
        self._bars.clearContents()
        self._bars.setRowCount(len(results))
        self._selected_inspection = None
        self._selected_deviation = None
        self._clear_editor()
        for row, (inspection, deviation) in enumerate(results):
            self._set_result_row(row, inspection, deviation)

    def _set_result_row(
        self,
        row: int,
        inspection: BarInspection,
        deviation: float | None,
    ) -> None:
        display_timezone = (
            MARKET_TIMEZONE if inspection.timeframe.is_intraday else UTC
        )
        local = inspection.raw_bar.timestamp.astimezone(display_timezone)
        raw = inspection.raw_bar
        values = (
            local.strftime("%Y-%m-%d"),
            local.strftime("%H:%M"),
            _format_number(raw.open),
            _format_number(raw.high),
            _format_number(raw.low),
            _format_number(raw.close),
            _format_number(raw.volume),
            _format_deviation(deviation),
            _inspection_status(inspection),
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 0:
                item.setData(_INSPECTION_ROLE, (inspection, deviation))
            self._bars.setItem(row, column, item)

    def _select_result(self) -> None:
        row = self._bars.currentRow()
        if row < 0:
            self._selected_inspection = None
            self._selected_deviation = None
            self._clear_editor()
            return
        item = self._bars.item(row, 0)
        if item is None:
            return
        data = item.data(_INSPECTION_ROLE)
        if not isinstance(data, tuple) or len(data) != 2:
            raise RuntimeError("Selected data-quality row has no bar inspection.")
        inspection, deviation = data
        if not isinstance(inspection, BarInspection):
            raise RuntimeError("Selected data-quality row is invalid.")
        self._selected_inspection = inspection
        self._selected_deviation = deviation
        self._load_editor(inspection)

    def _load_editor(self, inspection: BarInspection) -> None:
        raw = inspection.raw_bar
        effective = inspection.effective_bar
        for label, value in zip(
            self._raw_fields,
            (raw.open, raw.high, raw.low, raw.close, raw.volume),
        ):
            label.setText(_format_number(value))
        self._open.setValue(effective.open)
        self._high.setValue(effective.high)
        self._low.setValue(effective.low)
        self._close.setValue(effective.close)
        self._displayed_prices = {
            "open": self._open.value(),
            "high": self._high.value(),
            "low": self._low.value(),
            "close": self._close.value(),
        }
        self._volume.setText(str(effective.volume))
        if inspection.correction_error is not None:
            self._correction_status.setText(
                f"Correction conflict: {inspection.correction_error}"
            )
        elif inspection.correction is not None:
            self._correction_status.setText("Corrected")
        else:
            self._correction_status.setText("Provider values")
        self._set_editor_enabled(True)
        self._restore_button.setEnabled(inspection.correction is not None)
        self._load_comparison_state(inspection)

    def _clear_editor(self) -> None:
        for label in self._raw_fields:
            label.setText("-")
        self._open.setValue(self._open.minimum())
        self._high.setValue(self._high.minimum())
        self._low.setValue(self._low.minimum())
        self._close.setValue(self._close.minimum())
        self._volume.clear()
        self._correction_status.clear()
        self._displayed_prices.clear()
        self._set_editor_enabled(False)
        self._comparison_table.clearContents()
        self._comparison_table.setRowCount(0)
        self._displayed_comparison_result = None
        self._comparison_message.setText("Select a bar to compare sources.")
        self._compare_button.setText("Compare sources")
        self._compare_button.setEnabled(False)
        self._copy_comparison_button.setEnabled(False)
        self._refresh_provider_button.setEnabled(False)

    def _set_editor_enabled(self, enabled: bool) -> None:
        for editor in (
            self._open,
            self._high,
            self._low,
            self._close,
            self._volume,
        ):
            editor.setEnabled(enabled)
        self._apply_button.setEnabled(enabled)
        if not enabled:
            self._restore_button.setEnabled(False)

    def _load_comparison_state(self, inspection: BarInspection) -> None:
        self._comparison_table.clearContents()
        self._comparison_table.setRowCount(0)
        self._displayed_comparison_result = None
        self._copy_comparison_button.setEnabled(False)
        self._refresh_provider_button.setEnabled(False)
        timeframe = inspection.timeframe
        if timeframe in _SYNTHESIZED_TIMEFRAMES:
            self._comparison_message.setText(
                f"{timeframe.value} bars are synthesized by SimpleChart and "
                "have no directly comparable provider bar. Edit this "
                "synthesized bar manually."
            )
            self._compare_button.setText("Compare sources")
            self._compare_button.setEnabled(False)
            return
        if self._comparison_service is None:
            self._comparison_message.setText("Source comparison is unavailable.")
            self._compare_button.setText("Compare sources")
            self._compare_button.setEnabled(False)
            return

        key = _comparison_key(inspection)
        cached = self._comparison_cache.get(key)
        if cached is not None:
            self._display_comparison(cached)
            self._compare_button.setText("Compare again")
        else:
            self._comparison_message.setText(
                "Fetch this bar from its provider and available comparison "
                "sources."
            )
            self._compare_button.setText("Compare sources")
        self._compare_button.setEnabled(not self.comparison_running())

    def _compare_selected_bar(self) -> None:
        inspection = self._selected_inspection
        service = self._comparison_service
        if (
            inspection is None
            or service is None
            or inspection.timeframe in _SYNTHESIZED_TIMEFRAMES
            or self.comparison_running()
        ):
            return

        request = _ComparisonRequest(
            _comparison_key(inspection),
            inspection.raw_bar,
        )
        worker = _ComparisonWorker(service, request)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._comparison_completed)
        worker.failed.connect(self._comparison_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.completed.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(self._comparison_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._comparison_worker = worker
        self._comparison_thread = thread
        self._compare_button.setEnabled(False)
        self._copy_comparison_button.setEnabled(False)
        self._refresh_provider_button.setEnabled(False)
        self._comparison_message.setText("Comparing provider bars...")
        thread.start()

    def _comparison_completed(
        self,
        request_value: object,
        result_value: object,
    ) -> None:
        if (
            not self._accept_comparison_results
            or not isinstance(request_value, _ComparisonRequest)
            or not isinstance(result_value, BarComparisonResult)
        ):
            return
        self._comparison_cache[request_value.key] = result_value
        inspection = self._selected_inspection
        if inspection is None or _comparison_key(inspection) != request_value.key:
            return
        self._display_comparison(result_value)
        self._compare_button.setText("Compare again")

    def _comparison_failed(
        self,
        request_value: object,
        message: str,
    ) -> None:
        if not self._accept_comparison_results:
            return
        inspection = self._selected_inspection
        if (
            isinstance(request_value, _ComparisonRequest)
            and inspection is not None
            and _comparison_key(inspection) == request_value.key
        ):
            self._comparison_message.setText(message)

    def _comparison_thread_finished(self) -> None:
        self._comparison_thread = None
        self._comparison_worker = None
        if not self._accept_comparison_results:
            return
        inspection = self._selected_inspection
        if inspection is not None:
            self._load_comparison_state(inspection)

    def _display_comparison(self, result: BarComparisonResult) -> None:
        self._displayed_comparison_result = result
        self._comparison_table.clearContents()
        self._comparison_table.setRowCount(len(result.rows))
        suggestion_by_field = {
            suggestion.field: suggestion for suggestion in result.suggestions
        }
        for row_index, comparison_row in enumerate(result.rows):
            values = _comparison_row_values(comparison_row, result)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(_COMPARISON_ROW_ROLE, comparison_row)
                field = _COMPARISON_FIELDS.get(column)
                suggestion = (
                    None if field is None else suggestion_by_field.get(field)
                )
                if (
                    suggestion is not None
                    and comparison_row.kind
                    == BarComparisonRowKind.REFRESHED_ORIGIN
                ):
                    item.setBackground(_PROVIDER_REVISION_COLOR)
                    item.setToolTip("Refreshed provider revision")
                elif (
                    suggestion is not None
                    and comparison_row.source_namespace
                    in suggestion.corroborating_sources
                ):
                    item.setBackground(_CORROBORATION_COLOR)
                    item.setToolTip("Corroborates refreshed provider value")
                self._comparison_table.setItem(row_index, column, item)

        self._comparison_message.setText(_comparison_message(result))
        self._copy_comparison_button.setEnabled(False)
        self._refresh_provider_button.setEnabled(
            _refreshed_origin_bar(result) is not None
        )

    def _update_copy_button(
        self,
        current_row: int,
        current_column: int,
        _previous_row: int,
        _previous_column: int,
    ) -> None:
        comparison_row = self._comparison_row(current_row)
        self._copy_comparison_button.setEnabled(
            _comparison_value_is_copyable(comparison_row, current_column)
        )

    def _comparison_row(self, row: int) -> BarComparisonRow | None:
        if row < 0:
            return None
        item = self._comparison_table.item(row, 0)
        if item is None:
            return None
        value = item.data(_COMPARISON_ROW_ROLE)
        return value if isinstance(value, BarComparisonRow) else None

    def _copy_selected_comparison_value(self) -> None:
        row = self._comparison_table.currentRow()
        column = self._comparison_table.currentColumn()
        comparison_row = self._comparison_row(row)
        if not _comparison_value_is_copyable(comparison_row, column):
            return
        assert comparison_row is not None
        assert comparison_row.bar is not None
        field = _COMPARISON_FIELDS[column]
        value = _bar_field_value(comparison_row.bar, field)
        if field == "open":
            self._open.setValue(float(value))
        elif field == "high":
            self._high.setValue(float(value))
        elif field == "low":
            self._low.setValue(float(value))
        elif field == "close":
            self._close.setValue(float(value))
        else:
            self._volume.setText(str(value))

    def _refresh_provider_bar(self) -> None:
        inspection = self._selected_inspection
        result = self._displayed_comparison_result
        if inspection is None or result is None:
            return
        refreshed_bar = _refreshed_origin_bar(result)
        if refreshed_bar is None:
            return
        try:
            self._cache.refresh_provider_bar(
                inspection.cache_namespace,
                inspection.symbol,
                inspection.timeframe,
                inspection.raw_bar.timestamp,
                refreshed_bar,
            )
        except ValueError as exc:
            show_warning(self, "Provider Refresh Failed", str(exc))
            return

        self._invalidate_comparison_scope(inspection)
        row = self._bars.currentRow()
        refreshed_inspection = self._cache.get_bar_inspection(
            inspection.cache_namespace,
            inspection.symbol,
            inspection.timeframe,
            refreshed_bar.timestamp,
        )
        if refreshed_inspection is None:
            raise RuntimeError("The refreshed provider bar no longer exists.")
        self._selected_inspection = refreshed_inspection
        self._selected_deviation = None
        self._set_result_row(row, refreshed_inspection, None)
        self._bars.setCurrentCell(row, 0)
        self._load_editor(refreshed_inspection)
        self.bars_changed.emit()

    def _invalidate_comparison_scope(self, inspection: BarInspection) -> None:
        self._comparison_cache = {
            key: value
            for key, value in self._comparison_cache.items()
            if (
                key.cache_namespace,
                key.symbol,
                key.timeframe,
            )
            != (
                inspection.cache_namespace,
                inspection.symbol,
                inspection.timeframe,
            )
        }

    def comparison_running(self) -> bool:
        thread = self._comparison_thread
        return thread is not None and thread.isRunning()

    def shutdown_comparison(self) -> None:
        self._accept_comparison_results = False
        thread = self._comparison_thread
        if thread is not None and thread.isRunning():
            worker = self._comparison_worker
            assert worker is not None
            _comparison_thread_owner().adopt(thread, worker)
            thread.requestInterruption()
            thread.quit()
        self._comparison_thread = None
        self._comparison_worker = None

    def _apply_correction(self) -> None:
        inspection = self._selected_inspection
        if inspection is None:
            return
        try:
            volume = int(self._volume.text().strip().replace(",", ""))
        except ValueError:
            show_warning(
                self,
                "Invalid Volume",
                "Corrected volume must be a nonnegative integer.",
            )
            return
        raw = inspection.raw_bar
        effective = inspection.effective_bar
        correction = BarCorrection(
            cache_namespace=inspection.cache_namespace,
            symbol=inspection.symbol,
            timeframe=inspection.timeframe,
            timestamp=raw.timestamp,
            open=self._edited_price("open", self._open, effective.open),
            high=self._edited_price("high", self._high, effective.high),
            low=self._edited_price("low", self._low, effective.low),
            close=self._edited_price("close", self._close, effective.close),
            volume=volume,
        )
        try:
            self._cache.put_bar_correction(correction)
        except ValueError as exc:
            if "must differ" in str(exc):
                show_information(
                    self,
                    "No Correction",
                    "The edited values match the provider bar.",
                )
            else:
                show_warning(self, "Invalid Correction", str(exc))
            return
        self._refresh_selected_row()
        self.bars_changed.emit()

    def _edited_price(
        self,
        field: str,
        editor: QDoubleSpinBox,
        exact_value: float,
    ) -> float:
        displayed = self._displayed_prices.get(field)
        if displayed is not None and editor.value() == displayed:
            return exact_value
        return editor.value()

    def _restore_provider_bar(self) -> None:
        inspection = self._selected_inspection
        if inspection is None or inspection.correction is None:
            return
        self._cache.delete_bar_correction(
            inspection.cache_namespace,
            inspection.symbol,
            inspection.timeframe,
            inspection.raw_bar.timestamp,
        )
        self._refresh_selected_row()
        self.bars_changed.emit()

    def _refresh_selected_row(self) -> None:
        inspection = self._selected_inspection
        row = self._bars.currentRow()
        if inspection is None or row < 0:
            return
        refreshed = self._cache.get_bar_inspection(
            inspection.cache_namespace,
            inspection.symbol,
            inspection.timeframe,
            inspection.raw_bar.timestamp,
        )
        if refreshed is None:
            raise RuntimeError("The selected provider bar no longer exists.")
        self._selected_inspection = refreshed
        self._set_result_row(row, refreshed, self._selected_deviation)
        self._bars.setCurrentCell(row, 0)
        self._load_editor(refreshed)


def _price_editor(object_name: str, parent: QWidget) -> QDoubleSpinBox:
    editor = QDoubleSpinBox(parent)
    editor.setObjectName(object_name)
    editor.setRange(0.000001, 1_000_000_000.0)
    editor.setDecimals(6)
    editor.setGroupSeparatorShown(True)
    return editor


def _source_label(cache_namespace: str) -> str:
    return _SOURCE_LABELS.get(cache_namespace, cache_namespace)


def _comparison_key(inspection: BarInspection) -> _ComparisonKey:
    return _ComparisonKey(
        inspection.cache_namespace,
        inspection.symbol,
        inspection.timeframe,
        inspection.raw_bar.timestamp,
    )


def _comparison_value_is_copyable(
    row: BarComparisonRow | None,
    column: int,
) -> bool:
    if row is None or row.bar is None or column not in _COMPARISON_FIELDS:
        return False
    return column != 5 or row.kind != BarComparisonRowKind.CORROBORATION


def _refreshed_origin_bar(result: BarComparisonResult) -> Bar | None:
    return next(
        (
            row.bar
            for row in result.rows
            if row.kind == BarComparisonRowKind.REFRESHED_ORIGIN
        ),
        None,
    )


def _comparison_row_values(
    row: BarComparisonRow,
    result: BarComparisonResult,
) -> tuple[str, ...]:
    bar = row.bar
    if bar is None:
        prices = ("-", "-", "-", "-", "-")
    else:
        prices = (
            _format_number(bar.open),
            _format_number(bar.high),
            _format_number(bar.low),
            _format_number(bar.close),
            _format_number(bar.volume),
        )
    return (row.label, *prices, _comparison_row_status(row, result))


def _comparison_row_status(
    row: BarComparisonRow,
    result: BarComparisonResult,
) -> str:
    if row.error is not None:
        return row.error
    if row.adjustment_basis_difference:
        return "Possible adjustment-basis difference"
    if row.kind == BarComparisonRowKind.CACHED_ORIGIN:
        return "Cached provider value"
    if row.kind == BarComparisonRowKind.REFRESHED_ORIGIN:
        if result.origin_unchanged:
            return "Unchanged"
        if any(
            suggestion.corroborating_sources
            for suggestion in result.suggestions
        ):
            return "Provider revision — corroborated"
        return "Provider revision"
    if any(
        row.source_namespace in suggestion.corroborating_sources
        for suggestion in result.suggestions
    ):
        return "Corroborates provider revision"
    return "Comparison only"


def _comparison_message(result: BarComparisonResult) -> str:
    cached_row = next(
        row for row in result.rows if row.kind == BarComparisonRowKind.CACHED_ORIGIN
    )
    origin_label = cached_row.label.removeprefix("Cached ")
    if result.origin_unchanged:
        has_comparison_source = any(
            row.kind == BarComparisonRowKind.CORROBORATION
            for row in result.rows
        )
        suffix = (
            "Other sources are shown for comparison."
            if has_comparison_source
            else "No other comparison source is available."
        )
        return (
            f"{origin_label} is unchanged since it was cached — {suffix}"
        )
    if not result.suggestions:
        return (
            f"{origin_label} could not provide a revised value. Other sources "
            "are shown for comparison."
        )
    fields = ", ".join(suggestion.field for suggestion in result.suggestions)
    corroborated = [
        suggestion
        for suggestion in result.suggestions
        if suggestion.corroborating_sources
    ]
    if not corroborated:
        return f"The refreshed provider changed: {fields}."
    corroborated_fields = ", ".join(
        suggestion.field for suggestion in corroborated
    )
    return (
        f"The refreshed provider changed: {fields}. Corroborated fields: "
        f"{corroborated_fields}."
    )


def _bar_field_value(bar: Bar, field: BarField) -> float | int:
    if field == "open":
        return bar.open
    if field == "high":
        return bar.high
    if field == "low":
        return bar.low
    if field == "close":
        return bar.close
    return bar.volume


def _inspection_status(inspection: BarInspection) -> str:
    if inspection.correction_error is not None:
        return "Correction conflict"
    if inspection.correction is not None:
        return "Corrected"
    return "Uncorrected"


def _format_deviation(value: float | None) -> str:
    if value is None:
        return ""
    if math.isinf(value):
        return "Invalid"
    return f"{value:.2f}%"


def _format_number(value: float | int) -> str:
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:.6f}".rstrip("0").rstrip(".")
