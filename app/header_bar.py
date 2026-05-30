"""
app/header_bar.py

Controlled in-app header for Simple Chart.

The native OS title bar is intentionally left alone because its styling is not
consistent across Linux, macOS, and Windows. This header gives the app a visible
cross-platform identity band inside the Qt content area.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget


_SHORTCUT_MESSAGES: list[str] = [
    "<b>Ctrl+r</b>: Reset Chart View",
    "<b>Esc</b>: Cancel Drawing",
]


class AppHeader(QFrame):
    """Application shortcut hint bar."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        self.setObjectName("appHeader")
        self.setFixedHeight(24)
        self.setStyleSheet(
            "#appHeader {"
            " background: #2f3437;"
            " border-bottom: 1px solid #1f2427;"
            "}"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(12)

        layout.addStretch(1)

        shortcuts = QLabel("&nbsp;&nbsp;&nbsp;".join(_SHORTCUT_MESSAGES))
        shortcuts.setTextFormat(Qt.TextFormat.RichText)
        shortcuts.setStyleSheet("color: #d7dde1; font-size: 12px;")
        layout.addWidget(shortcuts)
