"""
chart/window.py

The chart widget — the finplot area plus the legend strip.

This is a QWidget (not QMainWindow) containing everything visual that
is specific to the chart: candlesticks, volume, extension lines, and
the legend. The overall application window (symbol bar, timeframe
buttons, menu bar) is assembled in app/controller.py.

Layout:
    ┌─────────────────────────────────────┐
    │ Legend strip (extension labels)      │
    ├─────────────────────────────────────┤
    │                                      │
    │  Price panel (candles + extensions)  │
    │                                      │
    ├─────────────────────────────────────┤
    │  Volume panel                        │
    └─────────────────────────────────────┘

finplot integration:
    finplot.create_plot_widget(master, rows=N) requires a
    pyqtgraph.GraphicsLayoutWidget as its master. finplot attaches all
    axes directly to that widget. We create the GraphicsLayoutWidget,
    pass it as master, and embed it in our QVBoxLayout. The price/volume
    height ratio is set via pyqtgraph's row stretch factors.
"""

from collections.abc import Callable

import finplot as fplt
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from chart.interactions import ChartInteractions
from chart.legend import ChartLegend
from chart.panel import ExtensionPanelSlot, Panel, PanelType
from chart.plot_manager import PlotManager
from chart.styles import AXIS_TEXT_COLOR, BACKGROUND
from chart.viewport import install_extension_panel_behavior, install_viewport_behavior, reset_viewports, sync_x_axis_labels
from simplechart.api import ToolIconSpec


class _FinplotMaster(pg.GraphicsLayoutWidget):  # type: ignore[misc]
    """
    pg.GraphicsLayoutWidget with the .axs property that finplot expects.

    finplot's FinWindow (its normal standalone window class) defines:
        @property
        def axs(self): return [ax for ax in self.ci.items if isinstance(ax, pg.PlotItem)]

    When we embed finplot in our own QWidget we use a plain
    GraphicsLayoutWidget as the master, which lacks this property.
    finplot's mouse-move handler accesses master.axs on every cursor
    movement, so we must provide it.
    """

    @property
    def axs(self) -> list[pg.PlotItem]:
        return [ax for ax in self.ci.items if isinstance(ax, pg.PlotItem)]


class ChartWidget(QWidget):
    """
    Self-contained chart area widget.

    The controller constructs this with the legend callbacks, then registers
    chart-interaction handlers (bar_clicked, etc.) before showing the window.

    Public interface used by the controller:
        plot_manager   — draw/update/remove data and extensions
        legend         — add/remove/toggle extension labels
        interactions   — register click handlers
    """

    def __init__(
        self,
        on_toggle:    Callable[[str], None],
        on_configure: Callable[[str], None],
        on_remove:    Callable[[str], None],
        on_add:       Callable[[], None],
        on_drawing_tool: Callable[[str], None],
        drawing_tools: list[tuple[str, str, ToolIconSpec | None]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._setup_finplot()
        self._legend = ChartLegend(
            on_toggle=on_toggle,
            on_configure=on_configure,
            on_remove=on_remove,
            on_add=on_add,
            on_drawing_tool=on_drawing_tool,
            drawing_tools=drawing_tools,
            parent=self,
        )
        self._build_layout()
        self._install_shortcuts()

    def _setup_finplot(self) -> None:
        """
        Initialize finplot and create all five panel axes.

        A pg.GraphicsLayoutWidget is required as the finplot master —
        it is the Qt widget that finplot attaches axes to. We create it
        here and embed it in our layout in _build_layout().

        create_plot_widget(master, rows=5) returns five axes:
          axes[0] — price panel  (stretch 4)
          axes[1] — volume panel (stretch 1)
          axes[2–4] — extension panel slots (stretch 0 — invisible until assigned)

        The three extension slots are pre-allocated so they live in the
        same _FinplotMaster. finplot's x-axis linking discovers axes via
        master.axs, which only covers axes in the same master widget. If
        extension panels were created dynamically in a separate widget,
        they would not be found and would fall out of x-sync after redraws.
        """
        # Set finplot globals before create_plot_widget, which calls
        # pg.setConfigOptions(foreground=..., background=...) internally.
        fplt.background = BACKGROUND       # chart fill — white
        fplt.foreground = AXIS_TEXT_COLOR  # axis text/ticks — dark gray
        fplt.odd_plot_background = BACKGROUND  # all panels — also white

        self._master = _FinplotMaster()
        # Explicitly set master background — pg.setConfigOptions only affects
        # widgets created after the call, but _master exists already.
        self._master.setBackground(BACKGROUND)

        axes = fplt.create_plot_widget(self._master, rows=5, init_zoom_periods=200)

        # create_plot_widget returns axes but does NOT add them to the master —
        # that step belongs to the caller (mirroring what create_plot() does).
        for ax in axes:
            self._master.addItem(ax, col=1)
            self._master.nextRow()

        # Price: row 0 (stretch 4), Volume: row 1 (stretch 1).
        # ChartExtension slots: rows 2–4: stretch 0 prevents proportional growth;
        # maxHeight 0 enforces the hard cap (stretch alone still allows min-size).
        self._master.ci.layout.setRowStretchFactor(0, 4)
        self._master.ci.layout.setRowStretchFactor(1, 1)
        for row in range(2, 5):
            self._master.ci.layout.setRowStretchFactor(row, 0)
            self._master.ci.layout.setRowMaximumHeight(row, 0.0)

        price_ax  = axes[0]
        volume_ax = axes[1]

        # Grid lines — very low alpha for a barely-visible reference grid.
        price_ax.showGrid(x=True, y=True, alpha=0.06)

        price_ax.crosshair.infos.append(
            lambda x, y, xtext, _ytext: (xtext, "%.2f" % y)
        )

        install_viewport_behavior(price_ax, volume_ax)

        self._price_panel  = Panel(price_ax,  PanelType.PRICE,  ratio=4)
        self._volume_panel = Panel(volume_ax, PanelType.VOLUME, ratio=1)

        self._extension_slots: list[ExtensionPanelSlot] = [
            ExtensionPanelSlot(Panel(axes[i + 2], PanelType.EXTENSION, ratio=0))
            for i in range(3)
        ]
        self._cancel_cb: Callable[[], None] | None = None
        self._commit_cb: Callable[[], None] | None = None

        # Hide all extension slots — they become visible only when assigned.
        for slot in self._extension_slots:
            slot.panel.ax.hide()

        all_axes = [price_ax, volume_ax] + [s.panel.ax for s in self._extension_slots]
        sync_x_axis_labels(all_axes)

    def _build_layout(self) -> None:
        """Assemble the legend strip above the finplot master widget."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._legend)
        layout.addWidget(self._master)
        self.setLayout(layout)

        self._plot_manager = PlotManager(
            self._price_panel,
            self._volume_panel,
            self._extension_slots,
        )
        self._interactions = ChartInteractions(self._price_panel.ax, self._master)

    def _install_shortcuts(self) -> None:
        reset_shortcut = QShortcut(QKeySequence("Ctrl+R"), self)
        reset_shortcut.activated.connect(self.reset_viewport)
        self._reset_shortcut = reset_shortcut

        cancel_drag_shortcut = QShortcut(QKeySequence("Esc"), self)
        cancel_drag_shortcut.activated.connect(self._cancel_drag)
        self._cancel_drag_shortcut = cancel_drag_shortcut

        # Enter/Return finishes a multi-point drawing in progress (e.g. poly-line).
        # Scoped to the chart subtree: a window-wide context would intercept
        # Enter before focused inputs elsewhere (e.g. the symbol box) see it.
        self._commit_shortcuts = [
            QShortcut(QKeySequence(key), self) for key in ("Return", "Enter")
        ]
        for shortcut in self._commit_shortcuts:
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self._commit_drawing)

    # ------------------------------------------------------------------
    # Public interface for the controller
    # ------------------------------------------------------------------

    @property
    def plot_manager(self) -> PlotManager:
        return self._plot_manager

    @property
    def legend(self) -> ChartLegend:
        return self._legend

    @property
    def interactions(self) -> ChartInteractions:
        return self._interactions

    def set_active_drawing_tool(self, tool_name: str | None) -> None:
        self._legend.set_active_drawing_tool(tool_name)

    def reset_viewport(self) -> None:
        reset_viewports(self._price_panel.ax, self._volume_panel.ax)
        fplt.refresh()

    def _cancel_drag(self) -> None:
        if self._interactions.cancel_drag():
            return
        if self._cancel_cb is not None:
            self._cancel_cb()

    def _all_axes(self) -> list[object]:
        return [self._price_panel.ax, self._volume_panel.ax] + [
            s.panel.ax for s in self._extension_slots
        ]

    def ensure_extension_panel(self, name: str) -> Panel:
        """
        Return the Panel for the given render_target name, allocating a
        slot if needed.

        If a slot is already assigned name, the existing panel is returned
        unchanged — calling this multiple times for the same name is safe.
        If no slot is assigned, the first free slot is claimed: its stretch
        is set to 2 and viewport behavior is installed (once, on first use).

        Raises RuntimeError if all three slots are occupied by other names.
        """
        for slot in self._extension_slots:
            if slot.name == name:
                return slot.panel

        for i, slot in enumerate(self._extension_slots):
            if slot.name is None:
                slot.name = name
                # Lift the hard height cap before setting stretch so the row
                # can actually grow to its proportional share.
                self._master.ci.layout.setRowMaximumHeight(i + 2, 10000.0)
                self._master.ci.layout.setRowStretchFactor(i + 2, 2)
                slot.panel.ax.show()
                if not slot.behavior_installed:
                    install_extension_panel_behavior(slot.panel.ax, self._price_panel.ax)
                    slot.behavior_installed = True
                sync_x_axis_labels(self._all_axes())
                return slot.panel

        raise RuntimeError(
            "All 3 extension panel slots are in use. "
            "Remove an existing panel extension before adding another."
        )

    def release_extension_panel(self, name: str) -> None:
        """
        Release the slot assigned to name, hiding the panel.

        Resets the axis data, collapses the row to zero height, and marks
        the slot as unoccupied. The viewport behavior patch installed on
        first use is left in place (it survives axis resets and will be
        valid if the slot is reused).
        """
        for i, slot in enumerate(self._extension_slots):
            if slot.name == name:
                slot.panel.ax.reset()
                slot.panel.ax.hide()
                self._master.ci.layout.setRowStretchFactor(i + 2, 0)
                self._master.ci.layout.setRowMaximumHeight(i + 2, 0.0)
                slot.name = None
                sync_x_axis_labels(self._all_axes())
                return

    def clear_all(self) -> None:
        """
        Remove all chart content and release all extension panel slots.

        Called by the controller on symbol load to give a clean slate
        before drawing new data. Equivalent to clearing price, volume, and
        all extension panels simultaneously.
        """
        self._plot_manager.clear_all()
        for slot in self._extension_slots:
            if slot.name is not None:
                self.release_extension_panel(slot.name)

    def _commit_drawing(self) -> None:
        if self._commit_cb is not None:
            self._commit_cb()

    def on_cancel(self, callback: Callable[[], None]) -> None:
        self._cancel_cb = callback

    def on_commit(self, callback: Callable[[], None]) -> None:
        self._commit_cb = callback
