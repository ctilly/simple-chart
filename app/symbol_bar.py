"""
app/symbol_bar.py

Symbol entry bar and timeframe selector.

Displayed at the top of the main window. The user types a ticker symbol
and presses Enter (or clicks Go), then clicks a timeframe button to
switch the chart.

Signals emitted to the controller:
  symbol_changed(symbol: str)       — user submitted a new symbol
  timeframe_changed(tf: Timeframe)  — user clicked a timeframe button

The currently active timeframe button is highlighted. The symbol field
shows the last successfully loaded symbol.
"""

from pathlib import Path

from PyQt6.QtCore import QModelIndex, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFocusEvent, QIcon, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QCompleter,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QToolButton,
    QWidget,
)

from app.symbol_search import SymbolSearch
from data.models import Timeframe
from data.provider.symbol_search import SymbolMatch


# Display labels for each timeframe button, in the order they appear.
_TIMEFRAME_LABELS: list[tuple[Timeframe, str]] = [
    (Timeframe.MIN5,   "5m"),
    (Timeframe.MIN15,  "15m"),
    (Timeframe.MIN30,  "30m"),
    (Timeframe.MIN39,  "39m"),
    (Timeframe.MIN65,  "65m"),
    (Timeframe.DAILY,  "D"),
    (Timeframe.WEEKLY, "W"),
]

_SETTINGS_ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "settings.svg"
# MIN1 is intentionally excluded — it is an internal base timeframe
# used by the aggregator, not a user-chartable timeframe.


class _SymbolInput(QLineEdit):
    """
    Line edit that selects its contents on focus, so typing a new ticker
    replaces the displayed symbol instead of appending to it.
    """

    focus_departed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._select_on_focus = QTimer(self)
        self._select_on_focus.setSingleShot(True)
        self._select_on_focus.timeout.connect(self.selectAll)

    def focusInEvent(self, a0: QFocusEvent | None) -> None:
        super().focusInEvent(a0)
        # Deferred: a focusing mouse click would otherwise clear the
        # selection when it places the text cursor after this event.
        if a0 is not None and a0.reason() != Qt.FocusReason.PopupFocusReason:
            self._select_on_focus.start(0)

    def focusOutEvent(self, event: QFocusEvent | None) -> None:
        super().focusOutEvent(event)
        if event is not None and event.reason() != Qt.FocusReason.PopupFocusReason:
            self.focus_departed.emit()

    def begin_chart_entry(self, first_character: str) -> None:
        self.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._select_on_focus.stop()
        self.setText(first_character)
        self.setModified(True)


class SymbolBar(QWidget):
    """
    Top bar containing the symbol input and timeframe buttons.
    """

    symbol_changed:    pyqtSignal = pyqtSignal(str)
    timeframe_changed: pyqtSignal = pyqtSignal(object)   # emits Timeframe
    settings_requested: pyqtSignal = pyqtSignal()
    search_idle: pyqtSignal = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._active_timeframe: Timeframe = Timeframe.DAILY
        self._tf_buttons: dict[Timeframe, QPushButton] = {}
        self._build()
        self._search = SymbolSearch(self)
        self._search.results.connect(self._show_search_results)
        self._search.idle.connect(self.search_idle.emit)
        self._result_model = QStandardItemModel(self)
        self._completer = QCompleter(self._result_model, self)
        self._completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self._completer.setCompletionRole(Qt.ItemDataRole.UserRole.value)
        self._completer.setWidget(self._symbol_input)
        self._completer.activated[str].connect(self._submit_symbol)
        self._symbol_input.textEdited.connect(self._on_search_edit)
        self._symbol_input.focus_departed.connect(self.stop_search)

    def _build(self) -> None:
        self.setObjectName("symbolBar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)

        # Symbol input
        self._symbol_input = _SymbolInput()
        self._symbol_input.setPlaceholderText("Symbol or company name")
        self._symbol_input.setMaximumWidth(184)
        self._symbol_input.setObjectName("symbolInput")
        self._symbol_input.returnPressed.connect(self._on_symbol_submitted)
        layout.addWidget(self._symbol_input)

        # Timeframe buttons
        for tf, label in _TIMEFRAME_LABELS:
            btn = QPushButton(label)
            btn.setObjectName("timeframeButton")
            btn.setCheckable(True)
            btn.setFixedWidth(40)
            btn.clicked.connect(lambda _checked, t=tf: self._on_timeframe_clicked(t))
            self._tf_buttons[tf] = btn
            layout.addWidget(btn)

        layout.addStretch()

        settings_button = QToolButton(self)
        settings_button.setObjectName("applicationSettingsButton")
        settings_button.setIcon(QIcon(str(_SETTINGS_ICON_PATH)))
        settings_button.setIconSize(QSize(16, 16))
        settings_button.setFixedSize(24, 24)
        settings_button.setToolTip("Application settings")
        settings_button.clicked.connect(self.settings_requested.emit)
        layout.addWidget(settings_button)

        self.setLayout(layout)
        self._highlight(self._active_timeframe)

    def set_symbol(self, symbol: str, *, preserve_edit: bool = False) -> None:
        """Update the field from the chart or an explicit symbol selection."""
        if preserve_edit and self._symbol_input.hasFocus() and self._symbol_input.isModified():
            return
        self.stop_search()
        self._symbol_input.setText(symbol.upper())

    def begin_chart_entry(self, first_character: str) -> None:
        self._symbol_input.begin_chart_entry(first_character)
        self._on_search_edit(first_character)

    @property
    def search_busy(self) -> bool:
        return self._search.busy

    def stop_search(self) -> None:
        self._search.cancel()
        self._clear_suggestions()

    def _clear_suggestions(self) -> None:
        popup = self._completer.popup()
        if popup is not None:
            popup.hide()
        self._result_model.clear()

    def _on_search_edit(self, text: str) -> None:
        self._clear_suggestions()
        self._search.edit(text)

    def _show_search_results(self, matches: tuple[SymbolMatch, ...]) -> None:
        if not matches or not self._symbol_input.hasFocus():
            return
        for match in matches:
            label = "  ·  ".join(part for part in (match.symbol, match.name, match.exchange) if part)
            item = QStandardItem(label)
            item.setData(match.symbol, Qt.ItemDataRole.UserRole)
            self._result_model.appendRow(item)
        field = self._symbol_input
        self._completer.complete(QRect(0, 0, max(400, field.width()), field.height()))
        popup = self._completer.popup()
        if popup is not None:
            popup.setCurrentIndex(QModelIndex())

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _on_symbol_submitted(self) -> None:
        text = self._symbol_input.text().strip().upper()
        if not text:
            return
        popup = self._completer.popup()
        if popup is not None and popup.isVisible() and popup.currentIndex().isValid():
            return
        self._submit_symbol(text)

    def _submit_symbol(self, symbol: str) -> None:
        self.stop_search()
        self._symbol_input.setText(symbol)
        self._symbol_input.setModified(False)
        self.symbol_changed.emit(symbol)

    def _on_timeframe_clicked(self, tf: Timeframe) -> None:
        if tf == self._active_timeframe:
            # Re-clicking the active button does nothing.
            self._tf_buttons[tf].setChecked(True)
            return
        self._active_timeframe = tf
        self._highlight(tf)
        self.timeframe_changed.emit(tf)

    def _highlight(self, active: Timeframe) -> None:
        """Set the checked state and style for all timeframe buttons."""
        for tf, btn in self._tf_buttons.items():
            btn.setChecked(tf == active)
