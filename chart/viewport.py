"""
chart/viewport.py

Viewport interaction patches for finplot.

finplot gives us most of what we need, but two parts of its default
behavior conflict with the desired chart UX:

1. FinViewBox.update_y_zoom() clamps the x-range to the loaded data plus
   a small right margin. That makes horizontal panning feel capped.
2. FinViewBox.mouseDragEvent() hard-codes x-only left-drag behavior and
   AxisItem forwards right-axis drags as plain y-pan instead of a price
   scale adjustment.

This module installs small instance-level patches on the chart's viewboxes
 so the price panel can pan freely in both axes, the volume panel stays
 x-only, and dragging the price axis vertically adjusts the visible price
 range smoothly.
"""

from __future__ import annotations

import math
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast
from weakref import WeakKeyDictionary

import finplot as fplt
import pyqtgraph as pg
from PyQt6.QtCore import Qt

_AXIS_ZOOM_SENSITIVITY = 1.003
_MANUAL_Y_DRAG_PIXELS = 6.0
_MIN_V_ZOOM_SCALE = 0.02
_MAX_V_ZOOM_SCALE = 50.0


class _PointLike(Protocol):
    def x(self) -> float: ...
    def y(self) -> float: ...


class _RectLike(Protocol):
    def left(self) -> float: ...
    def right(self) -> float: ...
    def top(self) -> float: ...
    def bottom(self) -> float: ...
    def width(self) -> float: ...


class _DragEventLike(Protocol):
    def accept(self) -> None: ...
    def button(self) -> Qt.MouseButton: ...
    def buttonDownPos(self, button: Qt.MouseButton | None = None) -> _PointLike: ...
    def isFinish(self) -> bool: ...
    def lastPos(self) -> _PointLike: ...
    def lastScreenPos(self) -> _PointLike: ...
    def modifiers(self) -> Qt.KeyboardModifier: ...
    def pos(self) -> _PointLike: ...
    def scenePos(self) -> _PointLike: ...
    def screenPos(self) -> _PointLike: ...


class _DataSourceLike(Protocol):
    init_x0: float
    init_x1: float
    xlen: int

    def hilo(self, x0: float, x1: float) -> tuple[object, object, float, float, int]: ...
    def update_init_x(self, init_steps: int) -> None: ...


class _YScaleLike(Protocol):
    scalef: float
    scaletype: str


class _WindowStateLike(Protocol):
    _isMouseLeftDrag: bool


class _ViewBoxLike(Protocol):
    datasrc: _DataSourceLike | None
    datasrc_or_standalone: _DataSourceLike | None
    force_range_update: int
    init_steps: int
    master_viewbox: object | None
    max_zoom_points_f: float
    state: dict[str, object]
    v_autozoom: bool
    v_zoom_baseline: float
    v_zoom_scale: float
    win: _WindowStateLike
    x_indexed: bool
    yscale: _YScaleLike

    def mapToView(self, point: _PointLike) -> _PointLike: ...
    def mapSceneToView(self, point: _PointLike) -> _PointLike: ...
    def mouseDragEvent(self, event: _DragEventLike, axis: int | None = None) -> None: ...
    def setMouseEnabled(self, x: bool | None = None, y: bool | None = None) -> None: ...
    def set_range(self, x0: float | None, y0: float, x1: float | None, y1: float) -> bool | None: ...
    def targetRect(self) -> _RectLike: ...
    def update_y_zoom(self, x0: float | None = None, x1: float | None = None) -> bool | None: ...
    def viewRect(self) -> _RectLike: ...
    def zoom_changed(self) -> None: ...


@dataclass(frozen=True)
class _AxisZoomState:
    baseline: float
    start_scale: float
    start_screen_y: float
    start_rect_top: float
    start_rect_bottom: float


_AXIS_ZOOM_STATES: WeakKeyDictionary[object, _AxisZoomState] = WeakKeyDictionary()
_FREE_X_PAN_BARS = 1_000_000.0


def install_viewport_behavior(price_ax: object, volume_ax: object) -> None:
    """
    Patch the chart viewboxes to allow Webull-style viewport interaction.

    Price panel:
      - free x+y panning in the plot area
      - right-axis vertical drag adjusts y-scale only

    Volume panel:
      - x-only panning
      - y-range continues to auto-fit its visible bars
    """
    price_vb = cast(_ViewBoxLike, getattr(price_ax, "vb"))
    volume_vb = cast(_ViewBoxLike, getattr(volume_ax, "vb"))

    apply_interaction_modes(price_ax, volume_ax)
    volume_vb.master_viewbox = price_vb

    _patch_update_y_zoom(price_vb)
    _patch_update_y_zoom(volume_vb)

    _patch_mouse_drag(price_vb, allow_vertical_pan=True)
    _patch_mouse_drag(volume_vb, allow_vertical_pan=False)


def apply_interaction_modes(price_ax: object, volume_ax: object) -> None:
    """Reapply the intended mouse interaction modes after finplot resets."""
    price_vb = cast(_ViewBoxLike, getattr(price_ax, "vb"))
    volume_vb = cast(_ViewBoxLike, getattr(volume_ax, "vb"))
    price_vb.setMouseEnabled(x=True, y=True)
    volume_vb.setMouseEnabled(x=True, y=False)


def reset_viewports(price_ax: object, volume_ax: object) -> None:
    """Restore the chart to its default x/y viewport."""
    _reset_viewbox(cast(_ViewBoxLike, getattr(price_ax, "vb")))
    _reset_viewbox(cast(_ViewBoxLike, getattr(volume_ax, "vb")))


def unlock_x_pan(ax: object) -> None:
    """
    Widen the axis x-limits so the viewport can move far past the last bar.

    finplot sets a tight xMax at draw/update time, which pins the newest
    candles near the right edge even if drag handling allows a wider range.
    """
    viewbox = cast(_ViewBoxLike, getattr(ax, "vb"))
    datasrc = viewbox.datasrc_or_standalone
    if datasrc is None:
        return
    ax.setLimits(  # type: ignore[attr-defined]
        xMin=-_FREE_X_PAN_BARS,
        xMax=datasrc.xlen + _FREE_X_PAN_BARS,
    )


def _patch_mouse_drag(viewbox: _ViewBoxLike, *, allow_vertical_pan: bool) -> None:
    original_mouse_drag = cast(Callable[[_DragEventLike, int | None], None], viewbox.mouseDragEvent)

    def _mouse_drag_event(
        self: _ViewBoxLike,
        event: _DragEventLike,
        axis: int | None = None,
    ) -> None:
        if self.master_viewbox is not None:
            master = cast(_ViewBoxLike, self.master_viewbox)
            forwarded_axis = 0 if not allow_vertical_pan else axis
            master.mouseDragEvent(event, forwarded_axis)
            return

        if self.datasrc is None:
            return

        no_modifier_drag = event.modifiers() == Qt.KeyboardModifier.NoModifier
        is_left_drag = event.button() == Qt.MouseButton.LeftButton

        if is_left_drag and no_modifier_drag and axis == 1 and allow_vertical_pan:
            _scale_from_price_axis_drag(self, event)
            return

        if is_left_drag and no_modifier_drag:
            if allow_vertical_pan:
                pg.ViewBox.mouseDragEvent(self, event, axis=None)
                if event.isFinish():
                    self.win._isMouseLeftDrag = False
                    drag = _drag_delta(event)
                    if abs(drag.x()) >= abs(drag.y()):
                        self.refresh_all_y_zoom()  # type: ignore[attr-defined]
                    else:
                        self.v_autozoom = False
                else:
                    self.win._isMouseLeftDrag = True
                event.accept()
                return

            pg.ViewBox.mouseDragEvent(self, event, axis=0)
            if event.isFinish():
                self.win._isMouseLeftDrag = False
            else:
                self.win._isMouseLeftDrag = True
            event.accept()
            return

        original_mouse_drag(event, axis)

    viewbox.mouseDragEvent = types.MethodType(_mouse_drag_event, viewbox)  # type: ignore[method-assign]


def _patch_update_y_zoom(viewbox: _ViewBoxLike) -> None:
    def _update_y_zoom(
        self: _ViewBoxLike,
        x0: float | None = None,
        x1: float | None = None,
    ) -> bool | None:
        datasrc = self.datasrc_or_standalone
        if datasrc is None:
            return None

        target_rect = self.targetRect()
        left = target_rect.left() if x0 is None else x0
        right = target_rect.right() if x1 is None else x1
        if right - left <= 1:
            return None

        visible_left, visible_right = _visible_data_window(left, right, datasrc.xlen)
        view_rect = self.viewRect()

        has_visible_data = visible_right > visible_left
        hi = view_rect.bottom()
        lo = view_rect.top()
        count = 0

        if has_visible_data:
            _, _, hi, lo, count = datasrc.hilo(visible_left, visible_right)
            if not _is_finite_range(hi, lo):
                hi = view_rect.bottom()
                lo = view_rect.top()
                count = 0

        min_len = int((fplt.max_zoom_points - 0.5) * self.max_zoom_points_f + 0.51)
        if count > 0 and (right - left) < view_rect.width() and count < min_len:
            return None

        if not self.v_autozoom or count == 0:
            hi = view_rect.bottom()
            lo = view_rect.top()

        if self.yscale.scaletype == "log":
            if lo < 0:
                lo = 0.05 * self.yscale.scalef
            else:
                lo = max(1e-100, lo)
            ratio = min((hi / lo) ** (1.0 / self.v_zoom_scale), 1e200)
            base = (hi * lo) ** self.v_zoom_baseline
            y0 = base / (ratio ** self.v_zoom_baseline)
            y1 = base * (ratio ** (1.0 - self.v_zoom_baseline))
        else:
            span = max((hi - lo) / self.v_zoom_scale, 2e-7)
            base = (hi + lo) * self.v_zoom_baseline
            y0 = base - span * self.v_zoom_baseline
            y1 = base + span * (1.0 - self.v_zoom_baseline)

        if not self.x_indexed:
            left, right = cast(tuple[float, float], fplt._xminmax(datasrc, x_indexed=False, extra_margin=0))

        return self.set_range(left, y0, right, y1)

    viewbox.update_y_zoom = types.MethodType(_update_y_zoom, viewbox)  # type: ignore[method-assign]


def _finish_pan(
    viewbox: _ViewBoxLike,
    event: _DragEventLike,
    *,
    allow_vertical_pan: bool,
) -> None:
    drag = _drag_delta(event)
    moved_vertically = allow_vertical_pan and abs(drag.y()) >= max(_MANUAL_Y_DRAG_PIXELS, abs(drag.x()) * 0.25)

    if moved_vertically:
        viewbox.v_autozoom = False
        return

    if viewbox.v_autozoom:
        target_rect = viewbox.targetRect()
        viewbox.update_y_zoom(target_rect.left(), target_rect.right())


def _scale_from_price_axis_drag(viewbox: _ViewBoxLike, event: _DragEventLike) -> None:
    current_rect = viewbox.viewRect()
    current_span = current_rect.bottom() - current_rect.top()
    if current_span <= 0:
        event.accept()
        return

    state = _AXIS_ZOOM_STATES.get(viewbox)
    if state is None:
        anchor_view = viewbox.mapSceneToView(event.scenePos())
        baseline = (anchor_view.y() - current_rect.top()) / current_span
        state = _AxisZoomState(
            baseline=min(max(baseline, 0.0), 1.0),
            start_scale=viewbox.v_zoom_scale,
            start_screen_y=event.screenPos().y(),
            start_rect_top=current_rect.top(),
            start_rect_bottom=current_rect.bottom(),
        )
        _AXIS_ZOOM_STATES[viewbox] = state

    dy_total = event.screenPos().y() - state.start_screen_y
    zoom_factor = _AXIS_ZOOM_SENSITIVITY ** dy_total
    viewbox.v_zoom_baseline = state.baseline
    viewbox.v_autozoom = False
    viewbox.v_zoom_scale = min(
        max(state.start_scale * zoom_factor, _MIN_V_ZOOM_SCALE),
        _MAX_V_ZOOM_SCALE,
    )

    target_rect = viewbox.targetRect()
    viewbox.update_y_zoom(target_rect.left(), target_rect.right())

    if event.isFinish():
        _AXIS_ZOOM_STATES.pop(viewbox, None)
        viewbox.win._isMouseLeftDrag = False
    else:
        viewbox.win._isMouseLeftDrag = True

    event.accept()
def _reset_viewbox(viewbox: _ViewBoxLike) -> None:
    datasrc = viewbox.datasrc_or_standalone
    if datasrc is None:
        return

    _AXIS_ZOOM_STATES.pop(viewbox, None)
    datasrc.update_init_x(viewbox.init_steps)
    viewbox.v_autozoom = True
    viewbox.v_zoom_baseline = 0.5
    viewbox.v_zoom_scale = 1.0 - fplt.y_pad
    viewbox.update_y_zoom(datasrc.init_x0, datasrc.init_x1)


def _visible_data_window(left: float, right: float, xlen: int) -> tuple[float, float]:
    min_x = -fplt.side_margin
    max_x = xlen + fplt.right_margin_candles - fplt.side_margin
    return max(left, min_x), min(right, max_x)


def _is_finite_range(high: float, low: float) -> bool:
    return math.isfinite(high) and math.isfinite(low) and high > low


def _drag_delta(event: _DragEventLike) -> _PointLike:
    class _Delta:
        def __init__(self, x_value: float, y_value: float) -> None:
            self._x = x_value
            self._y = y_value

        def x(self) -> float:
            return self._x

        def y(self) -> float:
            return self._y

    return _Delta(
        event.pos().x() - event.buttonDownPos().x(),
        event.pos().y() - event.buttonDownPos().y(),
    )
