"""
chart/interactions.py

Mouse and keyboard event handling for the chart.

This module is the only place in the chart layer that knows about user
input. It translates raw finplot/Qt events into meaningful signals that
the controller can act on.

Signals emitted:
  bar_clicked(x_pos: float)
      User left-clicked a bar. x_pos is finplot's raw x coordinate, which
      is a bar index (0, 1, 2, ...) when x_indexed=True (the default).
      The controller resolves the bar index to a UTC timestamp using the
      currently loaded series.

  bar_right_clicked(x_pos: float)
      Same as bar_clicked but for right-click.

The chart layer emits signals; it does NOT call the controller directly.
This keeps the dependency one-way: controller imports chart, chart does
not import controller.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW FINPLOT MOUSE EVENTS WORK
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

finplot uses integer bar indexes on the x-axis by default (x_indexed=True).
When we map a scene click to view coordinates, pos.x() is a bar index
(e.g. 147.3), NOT a Unix timestamp. The controller is responsible for
converting the bar index to a timestamp using the current series bars.

For bar-level click handling we use the sigMouseClicked signal on the
price panel's scene. The callback receives a Qt MouseClickEvent; we
map the scene x-coordinate to a bar index via the price viewbox.
"""

from typing import Callable


class ChartInteractions:
    """
    Connects finplot mouse events to application-level callbacks.

    Created by the chart window and given the finplot price axis. The
    controller registers its handler functions via on_bar_clicked() and
    on_bar_right_clicked() before the chart is shown.

    Callbacks receive a float bar index. The controller converts this to
    a UTC millisecond timestamp using the currently loaded series.
    """

    def __init__(self, price_ax: object, master: object) -> None:
        """
        price_ax is the finplot axis for the price panel.
        master is the pg.GraphicsLayoutWidget that owns the scene.

        We connect to master.scene().sigMouseClicked rather than
        ax.vb.scene() because the viewbox scene is None at construction
        time when using a GraphicsLayoutWidget — the master widget's
        scene exists immediately.
        """
        self._price_ax = price_ax
        self._master   = master
        self._bar_clicked_cb:       Callable[[float], None] | None = None
        self._bar_right_clicked_cb: Callable[[float], None] | None = None
        self._connect()

    def on_bar_clicked(self, callback: Callable[[float], None]) -> None:
        """Register a handler for left-click on a bar. Receives bar index."""
        self._bar_clicked_cb = callback

    def on_bar_right_clicked(self, callback: Callable[[float], None]) -> None:
        """Register a handler for right-click on a bar. Receives bar index."""
        self._bar_right_clicked_cb = callback

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        """
        Attach to the scene's sigMouseClicked signal.

        We use master.scene() (the pg.GraphicsLayoutWidget's scene) rather
        than ax.vb.scene() because the viewbox scene is not populated until
        the widget is embedded in a window. The master widget's scene is
        available immediately after construction.
        """
        scene = self._master.scene()  # type: ignore[attr-defined]
        scene.sigMouseClicked.connect(self._on_scene_clicked)

    def _on_scene_clicked(self, event: object) -> None:
        """
        Raw Qt mouse click handler.

        Maps the scene x-coordinate to a finplot bar index and routes to
        the appropriate registered callback based on button. The bar index
        is a float — the controller rounds it and looks up the timestamp.
        """
        from PyQt6.QtCore import Qt

        # finplot uses integer bar indexes on the x-axis (x_indexed=True).
        # pos.x() is a bar index float (e.g. 147.3), not a Unix timestamp.
        pos = self._price_ax.vb.mapSceneToView(event.scenePos())  # type: ignore[attr-defined]
        x_pos: float = pos.x()

        button = event.button()  # type: ignore[attr-defined]

        if button == Qt.MouseButton.LeftButton:
            if self._bar_clicked_cb is not None:
                self._bar_clicked_cb(x_pos)

        elif button == Qt.MouseButton.RightButton:
            if self._bar_right_clicked_cb is not None:
                self._bar_right_clicked_cb(x_pos)
