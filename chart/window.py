"""
chart/window.py

The chart widget — the finplot area plus the legend strip.

This is a QWidget (not QMainWindow) containing everything visual that
is specific to the chart: candlesticks, volume, indicator lines, and
the legend. The overall application window (symbol bar, timeframe
buttons, menu bar) is assembled in app/controller.py.

Layout:
    ┌─────────────────────────────────────┐
    │ Legend strip (indicator labels)      │
    ├─────────────────────────────────────┤
    │                                      │
    │  Price panel (candles + indicators)  │
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

from typing import Callable

import finplot as fplt
import pyqtgraph as pg
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from chart.interactions import ChartInteractions
from chart.legend import ChartLegend
from chart.panel import Panel, PanelType
from chart.plot_manager import PlotManager
from chart.styles import AXIS_TEXT_COLOR, BACKGROUND
from chart.viewport import install_viewport_behavior, reset_viewports


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

    The controller constructs this, then registers callbacks for
    bar_clicked and bar_right_clicked before showing the window.

    Public interface used by the controller:
        plot_manager   — draw/update/remove data and indicators
        legend         — add/remove/toggle indicator labels
        interactions   — register click handlers
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._setup_finplot()
        self._build_layout()
        self._install_shortcuts()

    def _setup_finplot(self) -> None:
        """
        Initialize finplot and create the two panels.

        A pg.GraphicsLayoutWidget is required as the finplot master —
        it is the Qt widget that finplot attaches axes to. We create it
        here and embed it in our layout in _build_layout().

        create_plot_widget(master, rows=2) returns a list of two axes:
          axes[0] — price panel
          axes[1] — volume panel

        Row stretch factors set the 4:1 height ratio between panels.
        """
        # Set finplot globals before create_plot_widget, which calls
        # pg.setConfigOptions(foreground=..., background=...) internally.
        fplt.background = BACKGROUND       # chart fill — white
        fplt.foreground = AXIS_TEXT_COLOR  # axis text/ticks — dark gray
        fplt.odd_plot_background = BACKGROUND  # volume panel — also white

        self._master = _FinplotMaster()
        # Explicitly set master background — pg.setConfigOptions only affects
        # widgets created after the call, but _master exists already.
        self._master.setBackground(BACKGROUND)

        axes = fplt.create_plot_widget(self._master, rows=2, init_zoom_periods=200)

        price_ax  = axes[0]
        volume_ax = axes[1]

        # create_plot_widget returns axes but does NOT add them to the master —
        # that step belongs to the caller (mirroring what create_plot() does).
        for ax in axes:
            self._master.addItem(ax, col=1)
            self._master.nextRow()

        # 4:1 price-to-volume height ratio via pyqtgraph's layout.
        self._master.ci.layout.setRowStretchFactor(0, 4)
        self._master.ci.layout.setRowStretchFactor(1, 1)

        # Grid lines — very low alpha for a barely-visible reference grid.
        price_ax.showGrid(x=True, y=True, alpha=0.06)

        price_ax.crosshair.infos.append(
            lambda x, y, xtext, ytext: (xtext, "%.2f" % y)
        )

        install_viewport_behavior(price_ax, volume_ax)

        self._price_panel  = Panel(price_ax,  PanelType.PRICE,  ratio=4)
        self._volume_panel = Panel(volume_ax, PanelType.VOLUME, ratio=1)

    def _build_layout(self) -> None:
        """Assemble the legend strip above the finplot master widget."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Placeholder legend — real callbacks wired by controller via wire_legend().
        self._legend = ChartLegend(
            on_toggle=lambda _: None,
            on_configure=lambda _: None,
            on_remove=lambda _: None,
            on_add=lambda: None,
            parent=self,
        )

        layout.addWidget(self._legend)
        layout.addWidget(self._master)
        self.setLayout(layout)

        self._plot_manager = PlotManager(self._price_panel, self._volume_panel)
        self._interactions = ChartInteractions(self._price_panel.ax, self._master)

    def _install_shortcuts(self) -> None:
        reset_shortcut = QShortcut(QKeySequence("Ctrl+R"), self)
        reset_shortcut.activated.connect(self.reset_viewport)
        self._reset_shortcut = reset_shortcut

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

    def reset_viewport(self) -> None:
        reset_viewports(self._price_panel.ax, self._volume_panel.ax)
        fplt.refresh()

    def wire_legend(
        self,
        on_toggle:    Callable[[str], None],
        on_configure: Callable[[str], None],
        on_remove:    Callable[[str], None],
        on_add:       Callable[[], None],
    ) -> None:
        """
        Replace the placeholder legend callbacks with real ones from the
        controller. Called once during app startup.
        """
        new_legend = ChartLegend(
            on_toggle=on_toggle,
            on_configure=on_configure,
            on_remove=on_remove,
            on_add=on_add,
            parent=self,
        )
        layout = self.layout()
        assert layout is not None
        old_item = layout.itemAt(0)
        assert old_item is not None
        old_widget = old_item.widget()
        assert old_widget is not None
        layout.replaceWidget(old_widget, new_legend)
        old_widget.deleteLater()
        self._legend = new_legend
