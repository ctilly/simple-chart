from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QMouseEvent, QPalette
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class MessageKind(Enum):
    WARNING = "warning"
    INFORMATION = "information"


@dataclass(frozen=True)
class _DialogColors:
    window: str
    text: str
    title: str
    border: str
    divider: str
    hover: str


class DialogTitleBar(QFrame):
    def __init__(
        self,
        title: str,
        on_close: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        colors = _dialog_colors(self)
        self.setObjectName("dialogTitleBar")
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setStyleSheet(
            "QFrame#dialogTitleBar {"
            f" background: {colors.title};"
            f" border-bottom: 1px solid {colors.divider};"
            "}"
            "QLabel {"
            f" color: {colors.text};"
            " font-weight: bold;"
            "}"
            "QToolButton {"
            f" color: {colors.text};"
            f" background: {colors.title};"
            f" border: 1px solid {colors.divider};"
            " border-radius: 3px;"
            "}"
            "QToolButton:hover {"
            f" background: {colors.hover};"
            "}"
        )
        self._drag_offset: QPoint | None = None

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 5, 4)
        row.setSpacing(4)

        label = QLabel(title, self)
        label.setObjectName("dialogTitle")
        row.addWidget(label)
        row.addStretch(1)

        close_button = QToolButton(self)
        close_button.setObjectName("dialogCloseButton")
        close_button.setToolTip("Close")
        close_button.setFixedSize(20, 20)
        style = self.style()
        if style is None:
            raise RuntimeError("The application style is unavailable.")
        close_button.setIcon(
            style.standardIcon(QStyle.StandardPixmap.SP_TitleBarCloseButton)
        )
        close_button.clicked.connect(on_close)
        row.addWidget(close_button)

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        window = self.window()
        if (
            event is not None
            and window is not None
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._drag_offset = (
                event.globalPosition().toPoint()
                - window.frameGeometry().topLeft()
            )
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        window = self.window()
        if (
            event is not None
            and window is not None
            and self._drag_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            window.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        self._drag_offset = None
        if event is not None:
            event.accept()


def build_dialog_content(
    dialog: QDialog,
    object_name: str,
    title: str,
) -> QVBoxLayout:
    parent = dialog.parentWidget()
    if parent is not None:
        dialog.setPalette(parent.palette())
    colors = _dialog_colors(dialog)
    dialog.setWindowFlags(
        Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
    )
    dialog.setObjectName(object_name)
    dialog.setStyleSheet(
        f"QDialog#{object_name} {{"
        f" background: {colors.window};"
        f" color: {colors.text};"
        f" border: 2px solid {colors.border};"
        "}"
        f"QDialog#{object_name} QLabel {{"
        f" color: {colors.text};"
        "}"
    )

    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    outer.addWidget(DialogTitleBar(title, dialog.reject, dialog))

    content = QWidget(dialog)
    content.setObjectName("dialogContent")
    layout = QVBoxLayout(content)
    layout.setContentsMargins(12, 10, 12, 10)
    outer.addWidget(content)
    return layout


class ApplicationMessageDialog(QDialog):
    def __init__(
        self,
        title: str,
        message: str,
        kind: MessageKind,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setMaximumWidth(680)
        layout = build_dialog_content(
            self,
            "applicationMessageDialog",
            title,
        )

        message_row = QHBoxLayout()
        message_row.setSpacing(12)
        icon = QLabel(self)
        icon.setObjectName("applicationMessageIcon")
        icon.setFixedSize(36, 36)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        style = self.style()
        if style is None:
            raise RuntimeError("The application style is unavailable.")
        standard_icon = (
            QStyle.StandardPixmap.SP_MessageBoxWarning
            if kind == MessageKind.WARNING
            else QStyle.StandardPixmap.SP_MessageBoxInformation
        )
        icon.setPixmap(style.standardIcon(standard_icon).pixmap(32, 32))
        message_row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        text = QLabel(message, self)
        text.setObjectName("applicationMessageText")
        text.setWordWrap(True)
        text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        text.setMinimumWidth(320)
        message_row.addWidget(text, 1)
        layout.addLayout(message_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok,
            parent=self,
        )
        buttons.setObjectName("applicationMessageButtons")
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


def show_warning(
    parent: QWidget | None,
    title: str,
    message: str,
) -> None:
    ApplicationMessageDialog(
        title,
        message,
        MessageKind.WARNING,
        parent,
    ).exec()


def show_information(
    parent: QWidget | None,
    title: str,
    message: str,
) -> None:
    ApplicationMessageDialog(
        title,
        message,
        MessageKind.INFORMATION,
        parent,
    ).exec()


def _dialog_colors(widget: QWidget) -> _DialogColors:
    palette = widget.palette()
    window = palette.color(QPalette.ColorRole.Window)
    text = palette.color(QPalette.ColorRole.WindowText)
    light = window.lightnessF() >= 0.5
    title = _shade(window, 108, 120, light)
    border = _shade(window, 170, 170, light)
    divider = _shade(window, 125, 145, light)
    hover = _shade(window, 115, 135, light)
    return _DialogColors(
        window.name(),
        text.name(),
        title.name(),
        border.name(),
        divider.name(),
        hover.name(),
    )


def _shade(
    color: QColor,
    darker_factor: int,
    lighter_factor: int,
    light: bool,
) -> QColor:
    return color.darker(darker_factor) if light else color.lighter(lighter_factor)
