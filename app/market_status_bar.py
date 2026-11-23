from datetime import datetime

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPaintEvent
from PyQt6.QtWidgets import (
    QGridLayout, QLabel, QSizePolicy, QStatusBar, QStyle, QStyleOption,
    QStylePainter, QWidget,
)

from app.data_source_status import DataSourceStatus
from data.session import MARKET_TIMEZONE


class MarketStatusBar(QStatusBar):
    def __init__(self, source: DataSourceStatus, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        content = QWidget(self)
        layout = QGridLayout(content)
        layout.setContentsMargins(4, 0, 0, 0)
        for column, stretch in enumerate((1, 2, 1)):
            layout.setColumnStretch(column, stretch)

        self._clock = QLabel()
        self._clock.setObjectName("marketClock")
        self._clock.setToolTip("Eastern Time (New York)")
        self._clock.setAccessibleName("Market clock, Eastern Time")
        layout.addWidget(self._clock, 0, 0, Qt.AlignmentFlag.AlignLeft)

        self._shortcuts = QLabel(
            "<b>Ctrl+r</b>: Reset Chart View"
            "&nbsp;&nbsp;&nbsp;<b>Esc</b>: Cancel Drawing"
        )
        self._shortcuts.setObjectName("chartShortcutHints")
        self._shortcuts.setTextFormat(Qt.TextFormat.RichText)
        self._shortcuts.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._shortcuts, 0, 1)

        self._message = QLabel()
        self._message.setObjectName("marketStatusMessage")
        self._message.setTextFormat(Qt.TextFormat.PlainText)
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._message, 0, 1)
        self._message.hide()

        layout.addWidget(source, 0, 2, Qt.AlignmentFlag.AlignRight)
        self.addPermanentWidget(content, 1)
        self.messageChanged.connect(self._show_center_message)

        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_clock)
        self._update_clock()
        self._clock_timer.start(1000)

    def _update_clock(self) -> None:
        self._clock.setText(datetime.now(MARKET_TIMEZONE).strftime("%H:%M ET"))

    def _show_center_message(self, message: str) -> None:
        self._message.setText(message)
        self._message.setToolTip(message)
        self._message.setVisible(bool(message))
        self._shortcuts.setVisible(not message)

    def stop_clock(self) -> None:
        self._clock_timer.stop()

    def paintEvent(self, event: QPaintEvent | None) -> None:
        # The center label displays status messages. Paint only the frame here.
        option = QStyleOption()
        option.initFrom(self)
        painter = QStylePainter(self)
        painter.drawPrimitive(QStyle.PrimitiveElement.PE_PanelStatusBar, option)
