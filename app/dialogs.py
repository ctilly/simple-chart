from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QMouseEvent, QPalette
from PyQt6.QtWidgets import (
    QCheckBox,
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

from data.adjustment import BasisChangeChoice


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


_FRAME_WIDTH = 2


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
        if event is not None and event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            assert window is not None
            window_handle = window.windowHandle()
            assert window_handle is not None
            window_handle.startSystemMove()
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
        f" border: {_FRAME_WIDTH}px solid {colors.border};"
        "}"
        f"QDialog#{object_name} QLabel {{"
        f" color: {colors.text};"
        "}"
    )

    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(
        _FRAME_WIDTH,
        _FRAME_WIDTH,
        _FRAME_WIDTH,
        _FRAME_WIDTH,
    )
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


class BasisChangeDialog(QDialog):
    """
    Asks whether to rescale or discard cached history that a data source
    has moved to a new adjustment basis but no longer returns.
    """

    def __init__(self, message: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._choice: BasisChangeChoice | None = None
        title = "Price History Adjusted"
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setMaximumWidth(680)
        layout = build_dialog_content(self, "basisChangeDialog", title)

        text = QLabel(message, self)
        text.setObjectName("basisChangeMessage")
        text.setWordWrap(True)
        layout.addWidget(text)
        help_text = QLabel(
            "Rescaling keeps the older bars and adjusts their prices and "
            "volumes to match. Drawings at fixed prices are not moved.",
            self,
        )
        help_text.setObjectName("basisChangeHelp")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self._remember = QCheckBox("Remember my choice", self)
        self._remember.setObjectName("basisChangeRemember")
        self._remember.setToolTip("You can change it later in Settings, UI.")
        layout.addWidget(self._remember)

        buttons = QDialogButtonBox(self)
        buttons.setObjectName("basisChangeButtons")
        rescale = buttons.addButton(
            "Rescale",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        discard = buttons.addButton(
            "Discard",
            QDialogButtonBox.ButtonRole.DestructiveRole,
        )
        assert rescale is not None and discard is not None
        rescale.setObjectName("basisChangeRescale")
        discard.setObjectName("basisChangeDiscard")
        rescale.clicked.connect(
            lambda: self._choose(BasisChangeChoice.RESCALE)
        )
        discard.clicked.connect(
            lambda: self._choose(BasisChangeChoice.DISCARD)
        )
        layout.addWidget(buttons)

    def answer(self) -> tuple[BasisChangeChoice, bool] | None:
        if self._choice is None:
            return None
        return (self._choice, self._remember.isChecked())

    def _choose(self, choice: BasisChangeChoice) -> None:
        self._choice = choice
        self.accept()


def ask_basis_change_choice(
    parent: QWidget | None,
    message: str,
) -> tuple[BasisChangeChoice, bool] | None:
    """The chosen action and whether to remember it; None when dismissed."""
    dialog = BasisChangeDialog(message, parent)
    dialog.exec()
    return dialog.answer()
