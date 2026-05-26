"""
chart/legend.py

ChartExtension legend — shows active indicators and lets the user toggle them.

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

from collections.abc import Callable
from typing import cast

from PyQt6.QtCore import QEvent, QObject, QPoint, QSize, Qt
from PyQt6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QIcon,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QStyle,
    QToolButton,
    QVBoxLayout,
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

    def mousePressEvent(self, event: object) -> None:
        from PyQt6.QtGui import QMouseEvent
        if not isinstance(event, QMouseEvent):
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


class DrawingToolPalette(QWidget):

    def __init__(
        self,
        tools: list[tuple[str, str]],
        on_drawing_tool: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Tools")
        self.setObjectName("drawingToolPalette")
        self._drag_offset: QPoint | None = None
        self.hide()

        frame = QFrame(self)
        frame.setObjectName("drawingToolPaletteFrame")
        frame.setStyleSheet(
            "QFrame#drawingToolPaletteFrame {"
            " background: #eeeeee;"
            " border: 1px solid #b8bcc5;"
            "}"
        )
        frame.installEventFilter(self)

        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(0, 0, 0, 6)
        frame_layout.setSpacing(5)

        title_bar = QFrame(frame)
        title_bar.setObjectName("drawingToolPaletteTitleBar")
        title_bar.setCursor(Qt.CursorShape.SizeAllCursor)
        title_bar.setStyleSheet(
            "QFrame#drawingToolPaletteTitleBar {"
            " background: #dddddd;"
            " border-bottom: 1px solid #c4c4c4;"
            "}"
        )
        title_bar.installEventFilter(self)

        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(6, 3, 5, 3)
        title_layout.setSpacing(4)

        title = QLabel("Tools", title_bar)
        title.setObjectName("drawingToolPaletteTitle")
        title.setStyleSheet("color: #555555;")
        title.setCursor(Qt.CursorShape.SizeAllCursor)
        title.installEventFilter(self)
        title_layout.addWidget(title)
        title_layout.addStretch(1)

        close_button = QToolButton(title_bar)
        close_button.setObjectName("drawingToolPaletteClose")
        close_button.setText("x")
        close_button.setToolTip("Close")
        close_button.setFixedSize(18, 18)
        close_button.setStyleSheet(
            "QToolButton { color: #555555; background: #dddddd; "
            "border: 1px solid #c4c4c4; border-radius: 3px; }"
            "QToolButton:hover { background: #d0d0d0; }"
        )
        close_button.clicked.connect(self.hide)
        title_layout.addWidget(close_button)
        frame_layout.addWidget(title_bar)

        tool_row = QFrame(frame)
        tool_row.setObjectName("drawingToolPaletteToolRow")
        tool_row.setStyleSheet("QFrame#drawingToolPaletteToolRow { background: #eeeeee; }")

        tool_layout = QHBoxLayout(tool_row)
        tool_layout.setContentsMargins(6, 0, 6, 0)
        tool_layout.setSpacing(4)

        for tool_name, tool_label in tools:
            button = QToolButton(tool_row)
            button.setObjectName(f"drawingToolButton_{tool_name}")
            button.setIcon(_tool_icon(tool_name, button))
            button.setIconSize(QSize(20, 20))
            button.setToolTip(tool_label)
            button.setFixedSize(28, 28)
            button.setStyleSheet(
                "QToolButton { color: #555555; background: #f7f7f7; "
                "border: 1px solid #cccccc; border-radius: 3px; }"
                "QToolButton:hover { background: #ffffff; }"
            )
            button.clicked.connect(
                lambda checked=False, name=tool_name: on_drawing_tool(name)
            )
            tool_layout.addWidget(button)
        frame_layout.addWidget(tool_row)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(frame)

    def eventFilter(self, watched: QObject | None, event: QEvent | None) -> bool:
        if isinstance(event, QMouseEvent):
            if event.type() == QEvent.Type.MouseButtonPress:
                self._begin_drag(event)
                return True
            if event.type() == QEvent.Type.MouseMove:
                self._continue_drag(event)
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._end_drag(event)
                return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: object) -> None:
        if not isinstance(event, QMouseEvent):
            return
        self._begin_drag(event)

    def mouseMoveEvent(self, event: object) -> None:
        if not isinstance(event, QMouseEvent):
            return
        self._continue_drag(event)

    def mouseReleaseEvent(self, event: object) -> None:
        if not isinstance(event, QMouseEvent):
            return
        self._end_drag(event)

    def _begin_drag(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint()
                - self.mapToGlobal(QPoint(0, 0))
            )
            event.accept()

    def _continue_drag(self, event: QMouseEvent) -> None:
        if self._drag_offset is None:
            return
        if event.buttons() & Qt.MouseButton.LeftButton:
            position = event.globalPosition().toPoint() - self._drag_offset
            parent = self.parentWidget()
            if parent is not None:
                position = parent.mapFromGlobal(position)
            self.move(position)
            event.accept()

    def _end_drag(self, event: QMouseEvent) -> None:
        if isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            event.accept()


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
        on_drawing_tool: Callable[[str], None],
        drawing_tools: list[tuple[str, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_toggle    = on_toggle
        self._on_configure = on_configure
        self._on_remove    = on_remove
        self._labels: dict[str, IndicatorLabel] = {}
        palette_parent = parent if parent is not None else self
        self._tool_palette = DrawingToolPalette(
            drawing_tools,
            on_drawing_tool,
            palette_parent,
        )
        self.destroyed.connect(self._tool_palette.deleteLater)
        self._tools_button: QToolButton | None = None

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

        layout.addStretch(1)

        tools_btn = QToolButton()
        tools_btn.setText("Tools")
        tools_btn.setFixedHeight(20)
        tools_btn.setToolTip("Drawing tools")
        tools_btn.setEnabled(bool(drawing_tools))
        tools_btn.setStyleSheet(
            "QToolButton { color: #555555; background: transparent; "
            "border: 1px solid #cccccc; border-radius: 3px; padding: 0px 6px; }"
            "QToolButton:hover { background: #eeeeee; }"
        )
        tools_btn.clicked.connect(self._toggle_tool_palette)
        layout.addWidget(tools_btn)
        self._tools_button = tools_btn

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
        layout = cast(QVBoxLayout, self.layout())
        assert layout is not None
        layout.insertWidget(max(1, layout.count() - 2), label)

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

    def _toggle_tool_palette(self) -> None:
        if self._tool_palette.isVisible():
            self._tool_palette.hide()
            return
        if self._tools_button is not None:
            self._tool_palette.adjustSize()
        self._tool_palette.show()
        self._position_tool_palette()
        self._tool_palette.raise_()

    def _position_tool_palette(self) -> None:
        self._tool_palette.adjustSize()
        self._tool_palette.move(self._tool_palette_position())

    def _tool_palette_position(self) -> QPoint:
        if self._tools_button is None:
            global_position = self.mapToGlobal(QPoint(0, self.height()))
        else:
            x = self._tools_button.mapToGlobal(
                QPoint(-self._tool_palette.width() - 12, 0)
            ).x()
            legend_bottom = self.mapToGlobal(QPoint(0, self.height())).y()
            y = legend_bottom - (self._tool_palette.height() // 4)
            global_position = QPoint(x, y)
        parent = self._tool_palette.parentWidget()
        if parent is None:
            return global_position
        return parent.mapFromGlobal(global_position)


def _tool_icon(tool_name: str, widget: QWidget) -> QIcon:
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#555555"), 2)
    painter.setPen(pen)
    if tool_name == "vertical_line":
        painter.drawLine(12, 4, 12, 20)
        painter.drawLine(7, 4, 17, 4)
        painter.drawLine(7, 20, 17, 20)
    elif tool_name == "fib_retracement":
        for y, length in ((5, 18), (9, 14), (14, 11), (19, 18)):
            painter.drawLine(3, y, 3 + length, y)
    else:
        style = widget.style()
        assert style is not None
        icon = style.standardIcon(QStyle.StandardPixmap.SP_ArrowRight)
        painter.end()
        return icon
    painter.end()
    return QIcon(pixmap)
