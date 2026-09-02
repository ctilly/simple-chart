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
from typing import Literal

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFocusEvent, QIcon
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
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

_SETTINGS_ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "settings.svg"
_SOURCE_STATUS_COLORS: dict[str, str] = {
    "pending": "#8a8e96",
    "connected": "#198754",
    "error": "#c43d3d",
}
SourceStatus = Literal["pending", "connected", "error"]

# MIN1 is intentionally excluded — it is an internal base timeframe
# used by the aggregator, not a user-chartable timeframe.


class _SymbolInput(QLineEdit):
    """
    Line edit that selects its contents on focus, so typing a new ticker
    replaces the displayed symbol instead of appending to it.
    """

    def focusInEvent(self, a0: QFocusEvent | None) -> None:
        super().focusInEvent(a0)
        # Deferred: a focusing mouse click would otherwise clear the
        # selection when it places the text cursor after this event.
        QTimer.singleShot(0, self.selectAll)


class SymbolBar(QWidget):
    """
    Top bar containing the symbol input and timeframe buttons.
    """

    symbol_changed:    pyqtSignal = pyqtSignal(str)
    timeframe_changed: pyqtSignal = pyqtSignal(object)   # emits Timeframe
    settings_requested: pyqtSignal = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._active_timeframe: Timeframe = Timeframe.DAILY
        self._tf_buttons: dict[Timeframe, QPushButton] = {}
        self._build()

    def _build(self) -> None:
        self.setObjectName("symbolBar")
        self.setStyleSheet(
            "#symbolBar {"
            " background: #eef2f4;"
            " border-bottom: 1px solid #c8d0d4;"
            "}"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)

        # Symbol input
        self._symbol_input = _SymbolInput()
        self._symbol_input.setPlaceholderText("Symbol (e.g. QQQ)")
        self._symbol_input.setMaximumWidth(140)
        self._symbol_input.setStyleSheet(
            "font-size: 13px; padding: 2px 6px; text-transform: uppercase; "
            "background: #ffffff; border: 1px solid #c6c6c6; border-radius: 3px;"
        )
        self._symbol_input.returnPressed.connect(self._on_symbol_submitted)
        layout.addWidget(self._symbol_input)

        # Timeframe buttons
        for tf, label in _TIMEFRAME_LABELS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedWidth(40)
            btn.clicked.connect(lambda _checked, t=tf: self._on_timeframe_clicked(t))
            self._tf_buttons[tf] = btn
            layout.addWidget(btn)

        layout.addStretch()

        self._source_status = QWidget(self)
        self._source_status.setObjectName("dataSourceStatus")
        self._source_status.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Preferred,
        )
        source_layout = QHBoxLayout(self._source_status)
        source_layout.setContentsMargins(0, 0, 8, 0)
        source_layout.setSpacing(4)

        self._source_dot = QLabel(self._source_status)
        self._source_dot.setObjectName("dataSourceStatusDot")
        self._source_dot.setFixedSize(8, 8)
        source_layout.addWidget(self._source_dot)

        self._source_label = QLabel(self._source_status)
        self._source_label.setObjectName("dataSourceStatusLabel")
        self._source_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._source_label.setStyleSheet("color: #555555; font-size: 12px;")
        source_layout.addWidget(self._source_label)
        layout.addWidget(self._source_status)

        settings_button = QToolButton(self)
        settings_button.setObjectName("applicationSettingsButton")
        settings_button.setIcon(QIcon(str(_SETTINGS_ICON_PATH)))
        settings_button.setIconSize(QSize(16, 16))
        settings_button.setFixedSize(24, 24)
        settings_button.setToolTip("Application settings")
        settings_button.setStyleSheet(
            "QToolButton { background: transparent; border: 1px solid #cccccc; "
            "border-radius: 3px; }"
            "QToolButton:hover { background: #eeeeee; }"
        )
        settings_button.clicked.connect(self.settings_requested.emit)
        layout.addWidget(settings_button)

        self.setLayout(layout)
        self._highlight(self._active_timeframe)
        self.set_data_source("Yahoo Finance", "pending")

    def set_symbol(self, symbol: str) -> None:
        """Update the symbol field (called after a successful load)."""
        self._symbol_input.setText(symbol.upper())

    def set_data_source(self, label: str, status: SourceStatus) -> None:
        color = _SOURCE_STATUS_COLORS[status]
        self._source_dot.setStyleSheet(
            f"background: {color}; border: none; border-radius: 4px;"
        )
        self._source_label.setText(label)
        self._source_status.updateGeometry()
        status_text = {
            "pending": "Waiting for a data response",
            "connected": "Last data request succeeded",
            "error": "Last data request failed",
        }[status]
        tooltip = f"{label}: {status_text}"
        self._source_dot.setToolTip(tooltip)
        self._source_label.setToolTip(tooltip)

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
                    "font-weight: bold; border: 1px solid #00b8b8; border-radius: 3px;"
                )
            else:
                btn.setStyleSheet(
                    "background-color: #f7f7f7; color: #333333; "
                    "border: 1px solid #cccccc; border-radius: 3px;"
                )
