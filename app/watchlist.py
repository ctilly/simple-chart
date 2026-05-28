"""
app/watchlist.py

Watchlist sidebar — a scrollable list of user-curated symbols.

Clicking a symbol emits symbol_selected so the controller can load it.
Right-clicking a symbol shows a context menu with a Remove option.
The "+" button at the bottom opens an input dialog to add a new symbol.

The widget does not talk to the database directly. The controller owns
all persistence — it passes callbacks (on_add, on_remove) and calls
add_symbol() / remove_symbol() / set_active_symbol() to keep the list
in sync after its own DB writes succeed.
"""

from collections.abc import Callable

from PyQt6.QtCore import QEvent, QObject, QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class WatchlistWidget(QWidget):
    """
    Narrow sidebar showing the user's watchlist.

    Signals:
        symbol_selected(str) — emitted when the user clicks a symbol.
    """

    symbol_selected: pyqtSignal = pyqtSignal(str)

    def __init__(
        self,
        symbols: list[str],
        on_add:    Callable[[str], None],
        on_remove: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_add    = on_add
        self._on_remove = on_remove
        # Lazy-created on first click: at __init__ time the WatchlistWidget has
        # no parent yet (the controller adds it to a layout afterwards), and the
        # popup needs to be parented to that wider container so it isn't
        # clipped by the 148px-wide watchlist column.
        self._popup: AddSymbolPopup | None = None
        self._build(symbols)
        self.setFixedWidth(148)

    def _build(self, symbols: list[str]) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QLabel("Watchlist")
        header.setFixedHeight(26)
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setStyleSheet(
            "background: #e0e0e0; font-weight: bold; font-size: 11px; "
            "color: #444444; border-bottom: 1px solid #c8c8c8;"
        )
        layout.addWidget(header)

        # Symbol list
        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget { border: none; font-size: 13px; background: #fafafa; }"
            "QListWidget::item { padding: 5px 8px; border-bottom: 1px solid #eeeeee; }"
            "QListWidget::item:selected { background: #d0eeff; color: #000000; }"
            "QListWidget::item:hover:!selected { background: #f0f0f0; }"
        )
        self._list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        for symbol in symbols:
            self._list.addItem(symbol)

        self._list.itemClicked.connect(
            lambda item: self.symbol_selected.emit(item.text())
        )
        self._list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self._list.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self._list)

        # Add button — toggles the AddSymbolPopup (styled to match the
        # Tools palette in the chart legend).
        self._add_btn = QPushButton("+ Add Symbol")
        self._add_btn.setFixedHeight(28)
        self._add_btn.setStyleSheet(
            "QPushButton { background: #f5f5f5; border: none; border-top: 1px solid #c8c8c8; "
            "font-size: 12px; color: #555555; }"
            "QPushButton:hover { background: #e8e8e8; }"
        )
        self._add_btn.clicked.connect(self._toggle_add_popup)
        layout.addWidget(self._add_btn)

    # ------------------------------------------------------------------
    # Public interface used by the controller
    # ------------------------------------------------------------------

    def add_symbol(self, symbol: str) -> None:
        """Append symbol to the list (no-op if already present)."""
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is not None and item.text() == symbol:
                return
        self._list.addItem(symbol)

    def remove_symbol(self, symbol: str) -> None:
        """Remove symbol from the list (no-op if not present)."""
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is not None and item.text() == symbol:
                self._list.takeItem(i)
                return

    def set_active_symbol(self, symbol: str) -> None:
        """Highlight the row matching symbol; clear selection if not found."""
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is not None and item.text() == symbol:
                self._list.setCurrentRow(i)
                return
        self._list.clearSelection()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _toggle_add_popup(self) -> None:
        if self._popup is None:
            popup_parent = self.parentWidget() if self.parentWidget() is not None else self
            self._popup = AddSymbolPopup(self._handle_popup_submit, popup_parent)
        if self._popup.isVisible():
            self._popup.hide()
            return
        self._popup.show_above(self._add_btn)

    def _handle_popup_submit(self, symbol: str) -> None:
        if symbol:
            self._on_add(symbol)

    def _show_context_menu(self, pos: QPoint) -> None:
        item = self._list.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        remove_action = menu.addAction("Remove")
        action = menu.exec(
            self._list.mapToGlobal(pos)
        )
        if action == remove_action:
            self._on_remove(item.text())


class AddSymbolPopup(QWidget):
    """
    Floating panel for entering a new watchlist symbol.

    Styled to match the Tools palette in the chart legend: framed box, gray
    title bar with a drag handle and an 'x' close button. Enter or the Add
    button submits; the popup hides itself afterwards.
    """

    def __init__(
        self,
        on_submit: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("addSymbolPopup")
        self._on_submit = on_submit
        self._drag_offset: QPoint | None = None
        self.hide()

        frame = QFrame(self)
        frame.setObjectName("addSymbolPopupFrame")
        frame.setStyleSheet(
            "QFrame#addSymbolPopupFrame {"
            " background: #eeeeee;"
            " border: 1px solid #b8bcc5;"
            "}"
        )
        frame.installEventFilter(self)

        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(0, 0, 0, 6)
        frame_layout.setSpacing(5)

        title_bar = QFrame(frame)
        title_bar.setObjectName("addSymbolPopupTitleBar")
        title_bar.setCursor(Qt.CursorShape.SizeAllCursor)
        title_bar.setStyleSheet(
            "QFrame#addSymbolPopupTitleBar {"
            " background: #dddddd;"
            " border-bottom: 1px solid #c4c4c4;"
            "}"
        )
        title_bar.installEventFilter(self)

        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(6, 3, 5, 3)
        title_layout.setSpacing(4)

        title = QLabel("Add Symbol", title_bar)
        title.setObjectName("addSymbolPopupTitle")
        title.setStyleSheet("color: #555555;")
        title.setCursor(Qt.CursorShape.SizeAllCursor)
        title.installEventFilter(self)
        title_layout.addWidget(title)
        title_layout.addStretch(1)

        close_button = QToolButton(title_bar)
        close_button.setObjectName("addSymbolPopupClose")
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

        input_row = QFrame(frame)
        input_row.setObjectName("addSymbolPopupInputRow")
        input_row.setStyleSheet("QFrame#addSymbolPopupInputRow { background: #eeeeee; }")

        input_layout = QHBoxLayout(input_row)
        input_layout.setContentsMargins(6, 0, 6, 0)
        input_layout.setSpacing(4)

        self._line_edit = QLineEdit(input_row)
        self._line_edit.setPlaceholderText("Symbol")
        self._line_edit.setFixedHeight(24)
        self._line_edit.setStyleSheet(
            "QLineEdit { color: #333333; background: #ffffff; "
            "border: 1px solid #cccccc; border-radius: 3px; padding: 0px 6px; }"
        )
        self._line_edit.returnPressed.connect(self._submit)
        input_layout.addWidget(self._line_edit)

        add_button = QToolButton(input_row)
        add_button.setObjectName("addSymbolPopupAdd")
        add_button.setText("Add")
        add_button.setFixedHeight(24)
        add_button.setStyleSheet(
            "QToolButton { color: #555555; background: #f7f7f7; "
            "border: 1px solid #cccccc; border-radius: 3px; padding: 0px 8px; }"
            "QToolButton:hover { background: #ffffff; }"
        )
        add_button.clicked.connect(self._submit)
        input_layout.addWidget(add_button)
        frame_layout.addWidget(input_row)

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(frame)

    def show_above(self, anchor: QWidget) -> None:
        """Position so the popup's bottom sits just above `anchor`, then show."""
        self.adjustSize()
        anchor_top_left = anchor.mapToGlobal(QPoint(0, 0))
        target_global = QPoint(anchor_top_left.x(), anchor_top_left.y() - self.height())
        parent_widget = self.parentWidget()
        position = (
            parent_widget.mapFromGlobal(target_global)
            if parent_widget is not None
            else target_global
        )
        self.move(position)
        self.show()
        self.raise_()
        self._line_edit.setFocus()
        self._line_edit.selectAll()

    def _submit(self) -> None:
        text = self._line_edit.text().strip().upper()
        self._line_edit.clear()
        self.hide()
        if text:
            self._on_submit(text)

    # Drag-to-move via the title bar (same pattern as DrawingToolPalette).

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
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            event.accept()
