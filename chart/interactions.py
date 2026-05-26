"""
chart/interactions.py

Mouse and keyboard event handling for the chart.

This module is the only place in the chart layer that knows about user
input. It translates raw finplot/Qt events into meaningful signals that
the controller can act on.

Signals emitted:
  bar_clicked(x_pos: float, y_pos: float)
      User left-clicked a bar. x_pos is finplot's raw x coordinate, which
      is a bar index (0, 1, 2, ...) when x_indexed=True (the default).
      y_pos is the price-axis coordinate.

  bar_right_clicked(x_pos: float, y_pos: float)
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

import types
from collections.abc import Callable
from typing import cast

from PyQt6.QtCore import Qt


class ChartInteractions:
    """
    Connects finplot mouse events to application-level callbacks.

    Created by the chart window and given the finplot price axis. The
    controller registers its handler functions via on_bar_clicked() and
    on_bar_right_clicked() before the chart is shown.

    Callbacks receive chart coordinates. The controller converts x to a
    UTC millisecond timestamp using the currently loaded series.
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
        self._bar_clicked_cb:       Callable[[float, float], None] | None = None
        self._bar_right_clicked_cb: Callable[[float, float], None] | None = None
        self._drag_start_cb:  Callable[[float, float], bool] | None = None
        self._drag_move_cb:   Callable[[float, float], None] | None = None
        self._drag_finish_cb: Callable[[float, float], None] | None = None
        self._drag_cancel_cb: Callable[[], None] | None = None
        self._mouse_move_cb:  Callable[[float, float], None] | None = None
        self._drag_active = False
        self._connect()
        self._patch_drag_handler()

    def on_bar_clicked(self, callback: Callable[[float, float], None]) -> None:
        """Register a handler for left-click on a bar. Receives bar index."""
        self._bar_clicked_cb = callback

    def on_bar_right_clicked(self, callback: Callable[[float, float], None]) -> None:
        """Register a handler for right-click on a bar. Receives bar index."""
        self._bar_right_clicked_cb = callback

    def on_drag_start(self, callback: Callable[[float, float], bool]) -> None:
        self._drag_start_cb = callback

    def on_drag_move(self, callback: Callable[[float, float], None]) -> None:
        self._drag_move_cb = callback

    def on_drag_finish(self, callback: Callable[[float, float], None]) -> None:
        self._drag_finish_cb = callback

    def on_drag_cancel(self, callback: Callable[[], None]) -> None:
        self._drag_cancel_cb = callback

    def on_mouse_move(self, callback: Callable[[float, float], None]) -> None:
        self._mouse_move_cb = callback

    def cancel_drag(self) -> bool:
        if not self._drag_active:
            return False
        self._drag_active = False
        self._set_indicator_drag_locked(False)
        self._price_ax.vb.win._isMouseLeftDrag = False  # type: ignore[attr-defined]
        if self._drag_cancel_cb is not None:
            self._drag_cancel_cb()
        return True

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
        scene.sigMouseMoved.connect(self._on_scene_mouse_moved)

    def _patch_drag_handler(self) -> None:
        viewbox = self._price_ax.vb  # type: ignore[attr-defined]
        original_mouse_drag = cast(
            Callable[[object, object | None], None],
            viewbox.mouseDragEvent,
        )

        def _mouse_drag_event(self_vb: object, event: object, axis: object | None = None) -> None:
            button = event.button()  # type: ignore[attr-defined]
            modifiers = event.modifiers()  # type: ignore[attr-defined]
            if button != Qt.MouseButton.LeftButton or modifiers != Qt.KeyboardModifier.NoModifier:
                original_mouse_drag(event, axis)
                return

            if self._drag_active:
                viewbox.win._isMouseLeftDrag = True
                self._set_indicator_drag_locked(True)
                if event.isFinish():  # type: ignore[attr-defined]
                    if (
                        self._scene_pos_in_price_panel(event)
                        and self._drag_finish_cb is not None
                    ):
                        pos = viewbox.mapToView(event.pos())  # type: ignore[attr-defined]
                        self._drag_finish_cb(pos.x(), pos.y())
                    else:
                        self.cancel_drag()
                    self._drag_active = False
                    viewbox.win._isMouseLeftDrag = False
                    event.accept()  # type: ignore[attr-defined]
                    self._set_indicator_drag_locked(False)
                    return

                if self._scene_pos_in_price_panel(event):
                    if self._drag_move_cb is not None:
                        pos = viewbox.mapToView(event.pos())  # type: ignore[attr-defined]
                        self._drag_move_cb(pos.x(), pos.y())
                else:
                    self.cancel_drag()
                event.accept()  # type: ignore[attr-defined]
                return

            if event.isFinish():  # type: ignore[attr-defined]
                original_mouse_drag(event, axis)
                return

            if self._drag_start_cb is not None and self._button_down_in_price_panel(event):
                if self._try_start_indicator_drag(viewbox, event):
                    self._drag_active = True
                    self._set_indicator_drag_locked(True)
                    viewbox.win._isMouseLeftDrag = True
                    if self._drag_move_cb is not None:
                        pos = viewbox.mapToView(event.pos())  # type: ignore[attr-defined]
                        self._drag_move_cb(pos.x(), pos.y())
                    event.accept()  # type: ignore[attr-defined]
                    return

            original_mouse_drag(event, axis)

        viewbox.mouseDragEvent = types.MethodType(_mouse_drag_event, viewbox)

    def _set_indicator_drag_locked(self, locked: bool) -> None:
        self._price_ax.vb.win._simplechart_indicator_drag_active = locked  # type: ignore[attr-defined]

    def _try_start_indicator_drag(self, viewbox: object, event: object) -> bool:
        if self._drag_start_cb is None:
            return False

        start_pos = viewbox.mapToView(event.buttonDownPos())  # type: ignore[attr-defined]
        if self._drag_start_cb(start_pos.x(), start_pos.y()):
            return True

        current_pos = viewbox.mapToView(event.pos())  # type: ignore[attr-defined]
        return self._drag_start_cb(current_pos.x(), current_pos.y())

    def _scene_pos_in_price_panel(self, event: object) -> bool:
        rect = self._price_ax.vb.sceneBoundingRect()  # type: ignore[attr-defined]
        return bool(rect.contains(event.scenePos()))  # type: ignore[attr-defined]

    def _button_down_in_price_panel(self, event: object) -> bool:
        rect = self._price_ax.vb.sceneBoundingRect()  # type: ignore[attr-defined]
        scene_pos = self._price_ax.vb.mapToScene(event.buttonDownPos())  # type: ignore[attr-defined]
        return bool(rect.contains(scene_pos))

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
        y_pos: float = pos.y()

        button = event.button()  # type: ignore[attr-defined]

        if button == Qt.MouseButton.LeftButton:
            if self._bar_clicked_cb is not None:
                self._bar_clicked_cb(x_pos, y_pos)

        elif button == Qt.MouseButton.RightButton:
            if self._bar_right_clicked_cb is not None:
                self._bar_right_clicked_cb(x_pos, y_pos)

    def _on_scene_mouse_moved(self, scene_pos: object) -> None:
        if self._mouse_move_cb is None:
            return
        rect = self._price_ax.vb.sceneBoundingRect()  # type: ignore[attr-defined]
        if not rect.contains(scene_pos):
            return
        pos = self._price_ax.vb.mapSceneToView(scene_pos)  # type: ignore[attr-defined]
        self._mouse_move_cb(pos.x(), pos.y())
