"""Collects every text a widget tree shows, for shielding assertions."""

from dataclasses import dataclass

import pytest
from PyQt6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QLabel,
    QLineEdit,
    QStatusBar,
    QWidget,
)

from app.dialogs import ApplicationMessageDialog


def widget_text(root: QWidget) -> str:
    parts: list[str] = []
    for widget in (root, *root.findChildren(QWidget)):
        parts.extend(
            (
                widget.windowTitle(),
                widget.toolTip(),
                widget.statusTip(),
                widget.whatsThis(),
                widget.accessibleName(),
                widget.accessibleDescription(),
            )
        )
        if isinstance(widget, (QLabel, QLineEdit, QAbstractButton)):
            parts.append(widget.text())
        if isinstance(widget, QLineEdit):
            parts.append(widget.placeholderText())
        if isinstance(widget, QComboBox):
            parts.append(widget.currentText())
        if isinstance(widget, QStatusBar):
            parts.append(widget.currentMessage())
    return "\n".join(part for part in parts if part)


@dataclass(frozen=True)
class ShownMessage:
    title: str
    message: str
    all_text: str


def capture_message_dialogs(monkeypatch: pytest.MonkeyPatch) -> list[ShownMessage]:
    shown: list[ShownMessage] = []

    def record(dialog: ApplicationMessageDialog) -> int:
        label = dialog.findChild(QLabel, "applicationMessageText")
        assert label is not None
        shown.append(
            ShownMessage(dialog.windowTitle(), label.text(), widget_text(dialog))
        )
        dialog.deleteLater()
        return 0

    monkeypatch.setattr(ApplicationMessageDialog, "exec", record)
    return shown
