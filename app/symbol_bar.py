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

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QWidget,
)

from data.models import Timeframe


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

# MIN1 is intentionally excluded — it is an internal base timeframe
# used by the aggregator, not a user-chartable timeframe.


class SymbolBar(QWidget):
    """
    Top bar containing the symbol input and timeframe buttons.
    """

    symbol_changed:    pyqtSignal = pyqtSignal(str)
    timeframe_changed: pyqtSignal = pyqtSignal(object)   # emits Timeframe

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._active_timeframe: Timeframe = Timeframe.DAILY
        self._tf_buttons: dict[Timeframe, QPushButton] = {}
        self._build()

    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)

        # Symbol input
        self._symbol_input = QLineEdit()
        self._symbol_input.setPlaceholderText("Symbol (e.g. QQQ)")
        self._symbol_input.setMaximumWidth(140)
        self._symbol_input.setStyleSheet(
            "font-size: 13px; padding: 2px 6px; text-transform: uppercase;"
        )
        self._symbol_input.returnPressed.connect(self._on_symbol_submitted)
        layout.addWidget(self._symbol_input)

        # Timeframe buttons
        for tf, label in _TIMEFRAME_LABELS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedWidth(40)
            btn.clicked.connect(lambda checked, t=tf: self._on_timeframe_clicked(t))
            self._tf_buttons[tf] = btn
            layout.addWidget(btn)

        layout.addStretch()
        self.setLayout(layout)
        self._highlight(self._active_timeframe)

    def set_symbol(self, symbol: str) -> None:
        """Update the symbol field (called after a successful load)."""
        self._symbol_input.setText(symbol.upper())

    def set_timeframe(self, tf: Timeframe) -> None:
        """Update the active timeframe highlight without emitting a signal."""
        self._active_timeframe = tf
        self._highlight(tf)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _on_symbol_submitted(self) -> None:
        text = self._symbol_input.text().strip().upper()
        if text:
            self.symbol_changed.emit(text)

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
            if tf == active:
                btn.setStyleSheet(
                    "background-color: #00d4d4; color: #000000; "
                    "font-weight: bold; border: none; border-radius: 3px;"
                )
            else:
                btn.setStyleSheet(
                    "background-color: transparent; color: #333333; "
                    "border: 1px solid #cccccc; border-radius: 3px;"
                )
