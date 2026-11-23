from typing import Literal

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

from app.theme import active_colors


_SOURCE_STATUS_COLOR_KEYS: dict[str, str] = {
    "pending": "pending",
    "connected": "success",
    "history": "warning",
    "error": "error",
}
SourceStatus = Literal["pending", "connected", "history", "error"]


class DataSourceStatus(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dataSourceStatus")
        self.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Preferred,
        )
        source_layout = QHBoxLayout(self)
        source_layout.setContentsMargins(0, 0, 8, 0)
        source_layout.setSpacing(4)

        self._source_dot = QLabel(self)
        self._source_dot.setObjectName("dataSourceStatusDot")
        self._source_dot.setFixedSize(8, 8)
        source_layout.addWidget(self._source_dot)

        self._source_label = QLabel(self)
        self._source_label.setObjectName("dataSourceStatusLabel")
        self._source_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        source_layout.addWidget(self._source_label)

        self.set_data_source("Yahoo Finance", "pending")

    def set_data_source(self, label: str, status: SourceStatus) -> None:
        color = active_colors()[_SOURCE_STATUS_COLOR_KEYS[status]]
        self._source_dot.setStyleSheet(
            f"background: {color}; border: none; border-radius: 4px;"
        )
        self._source_label.setText(label)
        self.updateGeometry()
        status_text = {
            "pending": "Waiting for a data response",
            "connected": "Last data request succeeded",
            "history": "Loading indicators...",
            "error": "Last data request failed",
        }[status]
        tooltip = f"{label}: {status_text}"
        self._source_dot.setToolTip(tooltip)
        self._source_label.setToolTip(tooltip)
