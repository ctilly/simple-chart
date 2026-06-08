"""
app/extension_config.py

Configuration dialog for individual extensions.

Opens when the user right-clicks an extension label in the legend (or
right-clicks a render item on the chart). Builds a form dynamically from
the extension's current params dict and returns the updated params on
accept.

Supported param types:
  int         → QSpinBox
  float       → QDoubleSpinBox
  str "#..."  → color picker button (shows hex value, opens QColorDialog on click)
  str (other) → QLineEdit
  ChoiceParam → QComboBox populated from .options; returns ChoiceParam with
                updated .value

Adding support for a new param type means adding a branch in
_build_field(). The extension's default_params() dict is the contract —
whatever types appear there, this dialog must handle.
"""

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor, QMouseEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from simplechart.api import ChoiceParam, FloatParam

_SESSION_PERSISTENCE_KEY = "persist_across_sessions"
_AGE_OFF_KEY = "age_off_days"


class ColorButton(QPushButton):
    """
    A button that displays the current color and opens a color picker
    when clicked.
    """

    def __init__(self, hex_color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = hex_color
        self._apply_style()
        self.clicked.connect(self._pick_color)

    def color(self) -> str:
        """Return the current color as a hex string."""
        return self._color

    def _pick_color(self) -> None:
        picked = QColorDialog.getColor(
            QColor(self._color),
            self,
            "Choose Color",
        )
        if picked.isValid():
            self._color = picked.name()
            self._apply_style()

    def _apply_style(self) -> None:
        # Show the hex value as text so the user can see the current color.
        # The button background is the color itself — clicking opens the picker.
        self.setText(f"  {self._color}  ")
        self.setToolTip("Click to open color picker")
        self.setStyleSheet(
            f"background-color: {self._color}; "
            f"color: {'#000000' if _is_light(self._color) else '#ffffff'}; "
            f"border: 2px solid #666666; "
            f"border-radius: 3px; "
            f"padding: 4px 10px; "
            f"font-weight: bold;"
        )


class _DialogTitleBar(QFrame):
    """
    Shaded, draggable title bar for the frameless config dialog.

    The dialog drops the native window frame (so there are no pointless
    minimize/maximize buttons and no white system bar that vanishes against the
    chart). This bar replaces it: a colored strip with the title and a single
    close button, and it moves the window when dragged — mirroring the drawing
    tool palette.
    """

    def __init__(
        self,
        title: str,
        on_close: "Callable[[], None]",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("configDialogTitleBar")
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setStyleSheet(
            "QFrame#configDialogTitleBar {"
            " background: #dddddd;"
            " border-bottom: 1px solid #c4c4c4;"
            "}"
        )
        self._drag_offset: QPoint | None = None

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 5, 4)
        row.setSpacing(4)

        label = QLabel(title, self)
        label.setStyleSheet("color: #555555; font-weight: bold;")
        row.addWidget(label)
        row.addStretch(1)

        close_button = QToolButton(self)
        close_button.setText("x")
        close_button.setToolTip("Close")
        close_button.setFixedSize(18, 18)
        close_button.setStyleSheet(
            "QToolButton { color: #555555; background: #dddddd; "
            "border: 1px solid #c4c4c4; border-radius: 3px; }"
            "QToolButton:hover { background: #d0d0d0; }"
        )
        close_button.clicked.connect(on_close)
        row.addWidget(close_button)

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        window = self.window()
        if event is not None and window is not None and event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - window.frameGeometry().topLeft()
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


class ExtensionConfigDialog(QDialog):
    """
    Modal dialog for editing extension parameters.

    Usage:
        dialog = ExtensionConfigDialog(
            extension_label="Simple Moving Average",
            params={"days": 50, "color": "#00BFFF"},
            parent=parent_widget,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_params = dialog.result_params()
    """

    def __init__(
        self,
        extension_label: str,
        params: dict[str, Any],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Configure: {extension_label}")
        self.setMinimumWidth(300)
        # Drop the native window frame: the config dialog has no use for
        # minimize/maximize, and the white system title bar disappears against
        # the white chart. A frameless window lets us paint our own shaded bar.
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        # The chart behind this dialog is white, so an unstyled (white) dialog is
        # nearly invisible. Give it the app's light-gray panel fill and a defined
        # border so it reads clearly as a separate surface. Scoped by object name
        # so only the dialog frame is painted, not every child widget.
        self.setObjectName("extensionConfigDialog")
        self.setStyleSheet(
            "QDialog#extensionConfigDialog {"
            " background: #eeeeee;"
            " border: 2px solid #8a8e96;"
            "}"
        )

        self._params = dict(params)  # working copy
        self._widgets: dict[str, QWidget] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(_DialogTitleBar(f"Configure: {extension_label}", self.reject, self))

        content = QWidget(self)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 10, 12, 10)
        form = QFormLayout()

        # Skip non-editable internal params. "anchors" is AVWAP's anchor list;
        # keys starting with "_" are controller-injected runtime data (e.g.
        # "_daily_bars") that are not user-configurable.
        has_session_lifecycle = _has_session_lifecycle(params)
        for key, value in params.items():
            if key == "anchors" or key.startswith("_"):
                continue
            if has_session_lifecycle and key in {_SESSION_PERSISTENCE_KEY, _AGE_OFF_KEY}:
                continue
            self._add_param_row(form, key, value)
        if has_session_lifecycle:
            self._add_session_lifecycle_group(form, params)

        if not self._widgets:
            form.addRow(QLabel("No configurable parameters."))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout.addLayout(form)
        layout.addWidget(buttons)
        outer.addWidget(content)

    def result_params(self) -> dict[str, Any]:
        """
        Return the params dict with values read from the form widgets.
        Skipped params (e.g. "anchors") are preserved from the original.
        Only call this after the dialog has been accepted.
        """
        result = dict(self._params)  # start with original (preserves skipped keys)
        for key, widget in self._widgets.items():
            result[key] = self._read_field(widget, self._params[key])
        return result

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _add_param_row(self, form: QFormLayout, key: str, value: Any) -> QWidget:
        widget = self._build_field(key, value)
        self._widgets[key] = widget
        form.addRow(self._format_label(key), widget)
        return widget

    def _add_session_lifecycle_group(
        self,
        form: QFormLayout,
        params: dict[str, Any],
    ) -> None:
        group = QGroupBox("Session Lifecycle")
        group_form = QFormLayout(group)
        for key in (_SESSION_PERSISTENCE_KEY, _AGE_OFF_KEY):
            self._add_param_row(group_form, key, params[key])
        form.addRow(group)
        persist_widget = self._widgets[_SESSION_PERSISTENCE_KEY]
        if isinstance(persist_widget, QCheckBox):
            persist_widget.toggled.connect(self._sync_age_off_enabled)
        self._sync_age_off_enabled()

    def _sync_age_off_enabled(self, checked: bool | None = None) -> None:
        persist_widget = self._widgets.get(_SESSION_PERSISTENCE_KEY)
        age_off_widget = self._widgets.get(_AGE_OFF_KEY)
        if isinstance(persist_widget, QCheckBox) and age_off_widget is not None:
            age_off_widget.setEnabled(persist_widget.isChecked())

    def _build_field(self, key: str, value: Any) -> QWidget:
        """Build the appropriate input widget for a param value."""
        if isinstance(value, bool):
            # bool check must come before int since bool is a subclass of int
            checkbox = QCheckBox()
            checkbox.setChecked(value)
            return checkbox

        if isinstance(value, int):
            spin = QSpinBox()
            spin.setRange(1, 9999)
            spin.setValue(value)
            return spin

        if isinstance(value, FloatParam):
            dspin = QDoubleSpinBox()
            dspin.setRange(value.minimum, value.maximum)
            dspin.setDecimals(value.decimals)
            dspin.setSingleStep(value.step)
            dspin.setValue(value.value)
            return dspin

        if isinstance(value, float):
            dspin = QDoubleSpinBox()
            dspin.setRange(0.0, 9999.0)
            dspin.setDecimals(2)
            dspin.setValue(value)
            return dspin

        if isinstance(value, ChoiceParam):
            combo = QComboBox()
            for opt in value.options:
                combo.addItem(opt)
            idx = value.options.index(value.value) if value.value in value.options else 0
            combo.setCurrentIndex(idx)
            return combo

        if isinstance(value, str) and value.startswith("#"):
            return ColorButton(value)

        # Default: plain text field
        edit = QLineEdit()
        edit.setText(str(value))
        return edit

    def _read_field(self, widget: QWidget, original: Any) -> Any:
        """Read the current value from a form widget."""
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QSpinBox):
            return widget.value()
        if isinstance(widget, QDoubleSpinBox):
            return widget.value()
        if isinstance(widget, ColorButton):
            return widget.color()
        if isinstance(widget, QComboBox):
            assert isinstance(original, ChoiceParam)
            return ChoiceParam(widget.currentText(), original.options)
        if isinstance(widget, QLineEdit):
            text = widget.text()
            # Preserve the original type if possible.
            if isinstance(original, int):
                try:
                    return int(text)
                except ValueError:
                    return original
            if isinstance(original, float):
                try:
                    return float(text)
                except ValueError:
                    return original
            return text
        return original

    @staticmethod
    def _format_label(key: str) -> str:
        """Convert a snake_case key to a Title Case label."""
        return key.replace("_", " ").title()


def _is_light(hex_color: str) -> bool:
    """
    Return True if the color is light enough that black text is readable
    on it. Used to choose button text color.
    """
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    # Standard relative luminance formula.
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return luminance > 0.5


def _has_session_lifecycle(params: dict[str, Any]) -> bool:
    return _SESSION_PERSISTENCE_KEY in params and _AGE_OFF_KEY in params
