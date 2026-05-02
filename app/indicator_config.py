"""
app/indicator_config.py

Configuration dialog for individual indicators.

Opens when the user right-clicks an indicator label in the legend (or
right-clicks a plot line on the chart). Builds a form dynamically from
the indicator's current params dict and returns the updated params on
accept.

Supported param types:
  int         → QSpinBox
  float       → QDoubleSpinBox
  str "#..."  → color picker button (shows hex value, opens QColorDialog on click)
  str (other) → QLineEdit
  ChoiceParam → QComboBox populated from .options; returns ChoiceParam with
                updated .value

Adding support for a new param type means adding a branch in
_build_field(). The indicator's default_params() dict is the contract —
whatever types appear there, this dialog must handle.
"""

from typing import Any

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from indicators._base import ChoiceParam


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


class IndicatorConfigDialog(QDialog):
    """
    Modal dialog for editing indicator parameters.

    Usage:
        dialog = IndicatorConfigDialog(
            indicator_label="Simple Moving Average",
            params={"days": 50, "color": "#00BFFF"},
            parent=parent_widget,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_params = dialog.result_params()
    """

    def __init__(
        self,
        indicator_label: str,
        params: dict[str, Any],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Configure: {indicator_label}")
        self.setMinimumWidth(300)

        self._params  = dict(params)   # working copy
        self._widgets: dict[str, QWidget] = {}

        layout = QVBoxLayout(self)
        form   = QFormLayout()

        # Skip non-editable internal params. "anchors" is AVWAP's anchor list;
        # keys starting with "_" are controller-injected runtime data (e.g.
        # "_daily_bars") that are not user-configurable.
        for key, value in params.items():
            if key == "anchors" or key.startswith("_"):
                continue
            widget = self._build_field(key, value)
            self._widgets[key] = widget
            form.addRow(self._format_label(key), widget)

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
        self.setLayout(layout)

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

    def _build_field(self, key: str, value: Any) -> QWidget:
        """Build the appropriate input widget for a param value."""
        if isinstance(value, bool):
            # bool check must come before int since bool is a subclass of int
            w = QCheckBox()
            w.setChecked(value)
            return w

        if isinstance(value, int):
            w = QSpinBox()
            w.setRange(1, 9999)
            w.setValue(value)
            return w

        if isinstance(value, float):
            w = QDoubleSpinBox()
            w.setRange(0.0, 9999.0)
            w.setDecimals(2)
            w.setValue(value)
            return w

        if isinstance(value, ChoiceParam):
            w = QComboBox()
            for opt in value.options:
                w.addItem(opt)
            idx = value.options.index(value.value) if value.value in value.options else 0
            w.setCurrentIndex(idx)
            return w

        if isinstance(value, str) and value.startswith("#"):
            return ColorButton(value)

        # Default: plain text field
        w = QLineEdit()
        w.setText(str(value))
        return w

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
