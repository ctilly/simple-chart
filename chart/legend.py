"""
chart/legend.py

Indicator legend — shows active indicators and lets the user toggle them.

Displayed as a horizontal strip at the top of the chart area, matching
the style of Webull's indicator label row. Each indicator appears as a
colored label. Clicking a label toggles the indicator's visibility.
Right-clicking opens the config dialog for that indicator.

The legend does not own indicator state — it only reflects what the
controller tells it. When the user clicks a label, the legend calls the
registered toggle callback; the controller updates state and calls
set_visible() on the PlotManager, then calls update() on the legend to
reflect the new state.
"""

from typing import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QCursor, QFont, QMouseEvent
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QWidget,
)

from chart.styles import LEGEND_FONT_SIZE


class IndicatorLabel(QLabel):
    """
    A single clickable label in the legend strip.

    Displays the indicator name in its assigned color. Muted when hidden.
    """

    def __init__(
        self,
        series_key: str,
        display_text: str,
        color: str,
        on_toggle: Callable[[str], None],
        on_configure: Callable[[str], None],
        on_remove: Callable[[str], None],
    ) -> None:
        super().__init__(display_text)
        self._series_key   = series_key
        self._color        = color
        self._on_toggle    = on_toggle
        self._on_configure = on_configure
        self._on_remove    = on_remove
        self._visible      = True
        self._apply_style()

    def set_color(self, color: str) -> None:
        """Update the label color (e.g. after user configures a new color)."""
        self._color = color
        self._apply_style()

    def set_indicator_visible(self, visible: bool) -> None:
        """Update visual state when the indicator is toggled."""
        self._visible = visible
        self._apply_style()

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_toggle(self._series_key)
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu()

    def _show_context_menu(self) -> None:
        menu = QMenu(self)
        configure_action = menu.addAction("Configure...")
        remove_action    = menu.addAction("Remove")
        action = menu.exec(QCursor.pos())
        if action == configure_action:
            self._on_configure(self._series_key)
        elif action == remove_action:
            self._on_remove(self._series_key)

    def _apply_style(self) -> None:
        color = self._color if self._visible else "#aaaaaa"
        font = QFont()
        font.setPointSize(LEGEND_FONT_SIZE)
        self.setFont(font)
        self.setStyleSheet(
            f"color: {color}; padding: 0px 6px;"
        )
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class ChartLegend(QWidget):
    """
    Horizontal strip of indicator labels displayed above the chart.

    The controller calls add_indicator(), remove_indicator(), and
    set_indicator_visible() to keep the legend in sync with chart state.
    """

    def __init__(
        self,
        on_toggle:    Callable[[str], None],
        on_configure: Callable[[str], None],
        on_remove:    Callable[[str], None],
        on_add:       Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_toggle    = on_toggle
        self._on_configure = on_configure
        self._on_remove    = on_remove
        self._labels: dict[str, IndicatorLabel] = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.setLayout(layout)
        self.setFixedHeight(24)

        add_btn = QPushButton("+")
        add_btn.setFixedSize(20, 20)
        add_btn.setToolTip("Add indicator")
        add_btn.setStyleSheet(
            "QPushButton { color: #555555; background: transparent; "
            "border: 1px solid #cccccc; border-radius: 3px; font-weight: bold; }"
            "QPushButton:hover { background: #eeeeee; }"
        )
        add_btn.clicked.connect(on_add)
        layout.addWidget(add_btn)

    def add_indicator(
        self,
        series_key: str,
        display_text: str,
        color: str,
    ) -> None:
        """Add a new indicator label to the legend."""
        if series_key in self._labels:
            return
        label = IndicatorLabel(
            series_key,
            display_text,
            color,
            self._on_toggle,
            self._on_configure,
            self._on_remove,
        )
        self._labels[series_key] = label
        self.layout().addWidget(label)  # type: ignore[union-attr]

    def remove_indicator(self, series_key: str) -> None:
        """Remove an indicator label from the legend."""
        if series_key not in self._labels:
            return
        label = self._labels.pop(series_key)
        self.layout().removeWidget(label)  # type: ignore[union-attr]
        label.deleteLater()

    def set_indicator_visible(self, series_key: str, visible: bool) -> None:
        """Update the visual state of a label without removing it."""
        if series_key in self._labels:
            self._labels[series_key].set_indicator_visible(visible)

    def clear_all(self) -> None:
        """Remove all indicator labels. Called when switching symbols."""
        for series_key in list(self._labels.keys()):
            self.remove_indicator(series_key)

    def update_color(self, series_key: str, color: str) -> None:
        """Update the color of an existing label (no-op if key not present)."""
        if series_key in self._labels:
            self._labels[series_key].set_color(color)
