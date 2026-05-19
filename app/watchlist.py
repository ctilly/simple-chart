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

from typing import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
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

        # Add button
        add_btn = QPushButton("+ Add Symbol")
        add_btn.setFixedHeight(28)
        add_btn.setStyleSheet(
            "QPushButton { background: #f5f5f5; border: none; border-top: 1px solid #c8c8c8; "
            "font-size: 12px; color: #555555; }"
            "QPushButton:hover { background: #e8e8e8; }"
        )
        add_btn.clicked.connect(self._prompt_add)
        layout.addWidget(add_btn)

    # ------------------------------------------------------------------
    # Public interface used by the controller
    # ------------------------------------------------------------------

    def add_symbol(self, symbol: str) -> None:
        """Append symbol to the list (no-op if already present)."""
        for i in range(self._list.count()):
            if self._list.item(i).text() == symbol:
                return
        self._list.addItem(symbol)

    def remove_symbol(self, symbol: str) -> None:
        """Remove symbol from the list (no-op if not present)."""
        for i in range(self._list.count()):
            if self._list.item(i).text() == symbol:
                self._list.takeItem(i)
                return

    def set_active_symbol(self, symbol: str) -> None:
        """Highlight the row matching symbol; clear selection if not found."""
        for i in range(self._list.count()):
            if self._list.item(i).text() == symbol:
                self._list.setCurrentRow(i)
                return
        self._list.clearSelection()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _prompt_add(self) -> None:
        symbol, ok = QInputDialog.getText(
            self, "Add to Watchlist", "Symbol:"
        )
        if ok and symbol.strip():
            self._on_add(symbol.strip().upper())

    def _show_context_menu(self, pos: object) -> None:
        item = self._list.itemAt(pos)  # type: ignore[arg-type]
        if item is None:
            return
        menu = QMenu(self)
        remove_action = menu.addAction("Remove")
        action = menu.exec(
            self._list.mapToGlobal(pos)  # type: ignore[arg-type]
        )
        if action == remove_action:
            self._on_remove(item.text())
