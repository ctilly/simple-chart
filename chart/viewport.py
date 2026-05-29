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
from datetime import date
from typing import Protocol, cast
from weakref import WeakKeyDictionary

import finplot as fplt
import pyqtgraph as pg
from PyQt6.QtCore import QEvent, QObject, Qt, QTimer
from PyQt6.QtGui import QNativeGestureEvent


_AXIS_ZOOM_SENSITIVITY = 1.003
_MAX_Y_OVERSHOOT_RATIO = 0.25
_PAN_AXIS_LOCK_PIXELS = 4.0
_NATIVE_GESTURE_FRAME_MS = 16
_NATIVE_ZOOM_SENSITIVITY = 0.8


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
    _simplechart_extension_drag_active: bool


class _ViewBoxLike(Protocol):
    childGroup: object
    datasrc: _DataSourceLike | None
    datasrc_or_standalone: _DataSourceLike | None
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

    def mapToView(self, _point: _PointLike) -> _PointLike: ...
    def mapSceneToView(self, _point: _PointLike) -> _PointLike: ...
    def linkedView(self, axis: int) -> object | None: ...
    def mouseDragEvent(self, event: _DragEventLike, axis: int | None = None) -> None: ...
    def translateBy(self, t: object | None = None, x: float | None = None, y: float | None = None) -> None: ...
    def _resetTarget(self) -> None: ...
    def setMouseEnabled(self, x: bool | None = None, y: bool | None = None) -> None: ...
    def set_range(self, x0: float | None, y0: float, x1: float | None, y1: float) -> bool | None: ...
    def targetRect(self) -> _RectLike: ...
    def update_y_zoom(self, x0: float | None = None, x1: float | None = None) -> bool | None: ...
    def viewRect(self) -> _RectLike: ...


@dataclass(frozen=True)
class _AxisZoomState:
    baseline: float
    min_decimals: int
    start_screen_y: float
    start_low: float
    start_high: float


@dataclass(frozen=True)
class _DragDelta:
    _x: float
    _y: float

    def x(self) -> float:
        return self._x

    def y(self) -> float:
        return self._y


@dataclass(frozen=True)
class ViewportState:
    viewbox: object
    left: float
    right: float
    top: float
    bottom: float
    v_autozoom: bool
    init_x0: float | None
    init_x1: float | None


@dataclass(frozen=True)
class ViewportSnapshot:
    states: list[ViewportState]


_AXIS_ZOOM_STATES: WeakKeyDictionary[object, _AxisZoomState] = WeakKeyDictionary()
_PAN_AXIS_STATES: WeakKeyDictionary[object, int] = WeakKeyDictionary()
_FREE_X_PAN_BARS = 1_000_000.0


def snapshot_viewports(axes: list[object]) -> ViewportSnapshot:
    states: list[ViewportState] = []
    for axis in axes:
        viewbox = cast(_ViewBoxLike, getattr(axis, "vb"))
        target_rect = viewbox.targetRect()
        datasrc = viewbox.datasrc_or_standalone
        init_x0 = None
        init_x1 = None
        if datasrc is not None:
            init_x0 = float(datasrc.init_x0)
            init_x1 = float(datasrc.init_x1)
        states.append(
            ViewportState(
                viewbox=viewbox,
                left=float(target_rect.left()),
                right=float(target_rect.right()),
                top=float(target_rect.top()),
                bottom=float(target_rect.bottom()),
                v_autozoom=bool(viewbox.v_autozoom),
                init_x0=init_x0,
                init_x1=init_x1,
            )
        )
    return ViewportSnapshot(states=states)


def restore_viewports(snapshot: ViewportSnapshot) -> None:
    for state in snapshot.states:
        viewbox = cast(_ViewBoxLike, state.viewbox)
        datasrc = viewbox.datasrc_or_standalone
        if (
            datasrc is not None
            and state.init_x0 is not None
            and state.init_x1 is not None
        ):
            datasrc.init_x0 = state.init_x0
            datasrc.init_x1 = state.init_x1
        viewbox.set_range(state.left, state.top, state.right, state.bottom)
        viewbox.v_autozoom = state.v_autozoom


def sync_x_axis_labels(axes: list[object]) -> None:
    """
    Enable x-axis labels on the bottommost visible panel; hide on all others.

    Called once after initial setup and again whenever an extension panel
    slot is shown or hidden, so the time labels always sit on the panel
    the user can actually see.
    """
    bottom_ax: object | None = None
    for ax in reversed(axes):
        if getattr(ax, "isVisible", lambda: False)():
            bottom_ax = ax
            break
    for ax in axes:
        set_visible_fn = getattr(ax, "set_visible", None)
        if callable(set_visible_fn):
            set_visible_fn(xaxis=(ax is bottom_ax))


def install_extension_panel_behavior(panel_ax: object, price_ax: object) -> None:
    """
    Patch an extension panel viewbox to follow the price panel's x-range
    and auto-scale its own y-range.

    Called once per slot on first assignment. Mirrors the volume panel
    pattern: drag events forward to the price viewbox so the full chart
    pans as a unit.
    """
    panel_vb = cast(_ViewBoxLike, getattr(panel_ax, "vb"))
    price_vb = cast(_ViewBoxLike, getattr(price_ax, "vb"))

    panel_vb.master_viewbox = price_vb
    panel_vb.setMouseEnabled(x=True, y=False)
    # Explicit x-link ensures _linked_x_viewboxes() finds this panel when
    # walking from the price viewbox, so _persist_current_x_range() keeps
    # the extension datasrc's init_x0/init_x1 in sync with the price panel.
    panel_vb.setXLink(price_vb)  # type: ignore[attr-defined]
    _patch_update_y_zoom(panel_vb)
    _patch_mouse_drag(panel_vb, allow_vertical_pan=False)
    _patch_x_axis_format(panel_ax)
    _patch_extension_axis_format(panel_ax)


def _patch_extension_axis_format(extension_ax: object) -> None:
    """
    Reduce tick density on an extension panel's y-axis.

    pyqtgraph's default tick density is tuned for price charts with many
    decimal places. Extension panels (e.g. RSI 0–100) need fewer, more
    widely spaced ticks so labels don't overlap.
    """
    axes = getattr(extension_ax, "axes", {})
    right_axis = axes.get("right", {}).get("item")
    if right_axis is None or getattr(right_axis, "_simplechart_ext_fmt_patch", False):
        return

    set_tick_density = getattr(right_axis, "setTickDensity", None)
    if callable(set_tick_density):
        set_tick_density(0.4)

    set_style = getattr(right_axis, "setStyle", None)
    if callable(set_style):
        set_style(
            maxTickLevel=1,          # major ticks only, no sub-levels
            textFillLimits=[(0, 0.6)],  # stop adding labels above 60% fill
        )

    right_axis._simplechart_ext_fmt_patch = True


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
    _patch_x_axis_format(price_ax)
    _patch_x_axis_format(volume_ax)
    _patch_price_axis_format(price_ax)
    _patch_volume_axis_format(volume_ax)

    _patch_update_y_zoom(price_vb)
    _patch_update_y_zoom(volume_vb)

    _install_native_gesture_handler(price_vb)
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
    x_min = -_FREE_X_PAN_BARS
    x_max = datasrc.xlen + _FREE_X_PAN_BARS
    ax.setLimits(  # type: ignore[attr-defined]
        xMin=x_min,
        xMax=x_max,
    )
    _force_x_limits(viewbox, x_min=x_min, x_max=x_max)


class _NativeGestureHandler(QObject):
    def __init__(self, master: QObject, viewbox: _ViewBoxLike) -> None:
        super().__init__(master)
        self._master = master
        self._viewbox = viewbox
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush_pending)
        self._active = False
        self._pending_pan_x = 0.0
        self._pending_pan_y = 0.0
        self._pending_zoom = 0.0
        self._pending_center: _PointLike | None = None
        self._total_pan_x = 0.0
        self._total_pan_y = 0.0

    def eventFilter(self, watched: QObject | None, event: QEvent | None) -> bool:
        if event is None or event.type() != QEvent.Type.NativeGesture:
            return super().eventFilter(watched, event)
        if not isinstance(event, QNativeGestureEvent):
            return super().eventFilter(watched, event)

        gesture_name = _enum_name(event.gestureType())
        if gesture_name == "BeginNativeGesture":
            self._begin()
            event.accept()
            return True
        if gesture_name == "EndNativeGesture":
            self._finish()
            event.accept()
            return True
        if gesture_name == "PanNativeGesture":
            delta = event.delta()
            self._pending_pan_x += delta.x()
            self._pending_pan_y += delta.y()
            self._total_pan_x += delta.x()
            self._total_pan_y += delta.y()
            self._pending_center = self._event_center(event)
            self._schedule_flush()
            event.accept()
            return True
        if gesture_name == "ZoomNativeGesture":
            self._pending_zoom += event.value()
            self._pending_center = self._event_center(event)
            self._schedule_flush()
            event.accept()
            return True
        if gesture_name == "RotateNativeGesture":
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def _begin(self) -> None:
        self._active = True
        self._pending_pan_x = 0.0
        self._pending_pan_y = 0.0
        self._pending_zoom = 0.0
        self._pending_center = None
        self._total_pan_x = 0.0
        self._total_pan_y = 0.0
        self._viewbox.win._isMouseLeftDrag = True

    def _finish(self) -> None:
        self._flush_pending()
        _persist_current_x_range(self._viewbox)
        if abs(self._total_pan_y) > abs(self._total_pan_x):
            self._viewbox.v_autozoom = False
        else:
            refresh_all_y_zoom = getattr(self._viewbox, "refresh_all_y_zoom", None)
            if callable(refresh_all_y_zoom):
                refresh_all_y_zoom()
        self._viewbox.win._isMouseLeftDrag = False
        self._active = False

    def _schedule_flush(self) -> None:
        if not self._active:
            self._begin()
        if not self._timer.isActive():
            self._timer.start(_NATIVE_GESTURE_FRAME_MS)

    def _flush_pending(self) -> None:
        pan_x = self._pending_pan_x
        pan_y = self._pending_pan_y
        zoom = self._pending_zoom
        center = self._pending_center
        self._pending_pan_x = 0.0
        self._pending_pan_y = 0.0
        self._pending_zoom = 0.0
        self._pending_center = None

        if pan_x != 0.0 or pan_y != 0.0:
            _translate_by_pixels(self._viewbox, pan_x, pan_y)
        if zoom != 0.0:
            _zoom_by_native_delta(self._viewbox, zoom, center)

    def _event_center(self, event: QNativeGestureEvent) -> _PointLike:
        map_to_scene = getattr(self._master, "mapToScene", None)
        if callable(map_to_scene):
            position = event.position()
            to_point = getattr(position, "toPoint", None)
            scene_pos = map_to_scene(to_point() if callable(to_point) else position)
            return self._viewbox.mapSceneToView(scene_pos)

        target = self._viewbox.targetRect()
        return _DragDelta(
            _x=(target.left() + target.right()) / 2.0,
            _y=(target.top() + target.bottom()) / 2.0,
        )


def _install_native_gesture_handler(viewbox: _ViewBoxLike) -> None:
    master = viewbox.win
    if not isinstance(master, QObject) or getattr(master, "_simplechart_native_gesture_handler", None) is not None:
        return
    handler = _NativeGestureHandler(master, viewbox)
    master.installEventFilter(handler)
    master._simplechart_native_gesture_handler = handler  # type: ignore[attr-defined]


def _enum_name(value: object) -> str:
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value)


def _translate_by_pixels(viewbox: _ViewBoxLike, pixel_x: float, pixel_y: float) -> None:
    delta = pg.Point(pixel_x, pixel_y) * -1
    transform = getattr(viewbox.childGroup, "transform")()
    inverse_transform = pg.functions.invertQTransform(transform)
    transformed_delta = inverse_transform.map(delta) - inverse_transform.map(pg.Point(0.0, 0.0))

    _force_x_limits(viewbox)
    viewbox._resetTarget()
    viewbox.translateBy(x=transformed_delta.x(), y=transformed_delta.y())
    viewbox.sigRangeChangedManually.emit(viewbox.state["mouseEnabled"])  # type: ignore[attr-defined]


def _zoom_by_native_delta(
    viewbox: _ViewBoxLike,
    zoom_delta: float,
    center: _PointLike | None,
) -> None:
    scale = _native_zoom_scale(zoom_delta)
    target = viewbox.targetRect()
    if center is None:
        center = _DragDelta(
            _x=(target.left() + target.right()) / 2.0,
            _y=(target.top() + target.bottom()) / 2.0,
        )
    x0 = center.x() + (target.left() - center.x()) * scale
    x1 = center.x() + (target.right() - center.x()) * scale
    viewbox.update_y_zoom(x0, x1)


def _native_zoom_scale(zoom_delta: float) -> float:
    amount = abs(zoom_delta) * _NATIVE_ZOOM_SENSITIVITY
    if zoom_delta > 0:
        return max(0.2, 1.0 / (1.0 + amount))
    return min(5.0, 1.0 + amount)


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
        if (
            is_left_drag
            and no_modifier_drag
            and getattr(self.win, "_simplechart_extension_drag_active", False)
        ):
            self.win._isMouseLeftDrag = True
            event.accept()
            return

        if is_left_drag and no_modifier_drag and axis == 1 and allow_vertical_pan:
            _scale_from_price_axis_drag(self, event)
            return

        if is_left_drag and no_modifier_drag:
            if allow_vertical_pan:
                drag_axis = _pan_drag_axis(self, event, axis)
                _translate_drag(self, event, axis=drag_axis)
                if event.isFinish():
                    _PAN_AXIS_STATES.pop(self, None)
                    _persist_current_x_range(self)
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

            _translate_drag(self, event, axis=0)
            if event.isFinish():
                _persist_current_x_range(self)
                self.win._isMouseLeftDrag = False
            else:
                self.win._isMouseLeftDrag = True
            event.accept()
            return

        original_mouse_drag(event, axis)

    viewbox.mouseDragEvent = types.MethodType(_mouse_drag_event, viewbox)  # type: ignore[method-assign]


def _pan_drag_axis(
    viewbox: _ViewBoxLike,
    event: _DragEventLike,
    axis: int | None,
) -> int | None:
    if axis is not None:
        return axis
    locked_axis = _PAN_AXIS_STATES.get(viewbox)
    if locked_axis is not None:
        return locked_axis

    drag = _drag_delta(event)
    dx = abs(drag.x())
    dy = abs(drag.y())
    if max(dx, dy) < _PAN_AXIS_LOCK_PIXELS:
        return 0

    locked_axis = 0 if dx >= dy else 1
    _PAN_AXIS_STATES[viewbox] = locked_axis
    return locked_axis


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


def _translate_drag(
    viewbox: _ViewBoxLike,
    event: _DragEventLike,
    *,
    axis: int | None,
) -> None:
    pos = pg.Point(event.pos())
    last_pos = pg.Point(event.lastPos())
    delta = (pos - last_pos) * -1

    transform = getattr(viewbox.childGroup, "transform")()
    inverse_transform = pg.functions.invertQTransform(transform)
    transformed_delta = inverse_transform.map(delta) - inverse_transform.map(pg.Point(0.0, 0.0))

    delta_x = transformed_delta.x()
    delta_y = transformed_delta.y()

    move_x = axis in (None, 0)
    move_y = axis in (None, 1)

    x_shift = delta_x if move_x else None
    y_shift = delta_y if move_y else None

    _force_x_limits(viewbox)
    viewbox._resetTarget()
    if x_shift is not None or y_shift is not None:
        viewbox.translateBy(x=x_shift, y=y_shift)
        viewbox.sigRangeChangedManually.emit(viewbox.state["mouseEnabled"])  # type: ignore[attr-defined]


def _force_x_limits(
    viewbox: _ViewBoxLike,
    *,
    x_min: float | None = None,
    x_max: float | None = None,
) -> None:
    datasrc = viewbox.datasrc_or_standalone
    if datasrc is None:
        return

    limits = viewbox.state.get("limits")
    if not isinstance(limits, dict):
        return

    if x_min is None:
        x_min = -_FREE_X_PAN_BARS
    if x_max is None:
        x_max = datasrc.xlen + _FREE_X_PAN_BARS

    limits["xLimits"] = [x_min, x_max]



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
            min_decimals=_axis_min_decimals(viewbox),
            start_screen_y=event.screenPos().y(),
            start_low=current_rect.top(),
            start_high=current_rect.bottom(),
        )
        _AXIS_ZOOM_STATES[viewbox] = state

    dy_total = event.screenPos().y() - state.start_screen_y
    viewbox.v_autozoom = False
    zoom_factor = _AXIS_ZOOM_SENSITIVITY ** dy_total

    start_span = max(state.start_high - state.start_low, 2e-7)
    new_span = start_span / zoom_factor
    data_low, data_high = _data_y_bounds_for_window(viewbox, current_rect.left(), current_rect.right())
    if data_high is not None and data_low is not None:
        max_span = max(
            data_high - data_low,
            (data_high + max(abs(data_high) * _MAX_Y_OVERSHOOT_RATIO, (data_high - data_low) * _MAX_Y_OVERSHOOT_RATIO))
            - (data_low - max(abs(data_low) * _MAX_Y_OVERSHOOT_RATIO, (data_high - data_low) * _MAX_Y_OVERSHOOT_RATIO)),
        )
        new_span = min(max(new_span, 2e-7), max_span)

    anchor_value = state.start_low + state.baseline * start_span
    new_low = anchor_value - state.baseline * new_span
    new_high = anchor_value + (1.0 - state.baseline) * new_span

    target_rect = viewbox.targetRect()
    viewbox.set_range(target_rect.left(), new_low, target_rect.right(), new_high)
    _clamp_axis_drag_range(viewbox, target_rect.left(), target_rect.right())
    _refresh_price_axis_precision(viewbox, zooming_in=(dy_total > 0), min_decimals=state.min_decimals)

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


def _persist_current_x_range(viewbox: _ViewBoxLike) -> None:
    left, right = _current_target_x_range(viewbox)
    for target in _linked_x_viewboxes(viewbox):
        datasrc = target.datasrc_or_standalone
        if datasrc is None:
            continue
        datasrc.init_x0 = left
        datasrc.init_x1 = right


def _current_target_x_range(viewbox: _ViewBoxLike) -> tuple[float, float]:
    target_range = viewbox.state.get("targetRange")
    if isinstance(target_range, list) and len(target_range) >= 1:
        x_range = target_range[0]
        if isinstance(x_range, list) and len(x_range) == 2:
            left = x_range[0]
            right = x_range[1]
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return float(left), float(right)

    rect = viewbox.targetRect()
    return rect.left(), rect.right()


def _linked_x_viewboxes(viewbox: _ViewBoxLike) -> list[_ViewBoxLike]:
    linked: list[_ViewBoxLike] = [viewbox]
    window_axes = getattr(viewbox.win, "axs", [])
    for axis in window_axes:
        candidate = cast(_ViewBoxLike, getattr(axis, "vb"))
        if candidate is viewbox:
            continue
        if candidate.linkedView(0) is viewbox or viewbox.linkedView(0) is candidate:
            linked.append(candidate)
    return linked


def _refresh_price_axis_precision(
    viewbox: _ViewBoxLike,
    *,
    zooming_in: bool,
    min_decimals: int,
) -> None:
    axis = getattr(viewbox, "parent", lambda: None)()
    datasrc = viewbox.datasrc_or_standalone
    update_significants = getattr(fplt, "_update_significants", None)
    if axis is None or datasrc is None or not callable(update_significants):
        return
    update_significants(axis, datasrc, True)
    next_min = _bias_price_axis_precision(axis, viewbox, min_decimals=min_decimals, zooming_in=zooming_in)
    right_axis = axis.axes.get("right", {}).get("item")
    if right_axis is not None:
        right_axis._min_decimals = next_min
        right_axis.picture = None
        right_axis.update()


def _bias_price_axis_precision(
    axis: object,
    viewbox: _ViewBoxLike,
    *,
    min_decimals: int,
    zooming_in: bool,
) -> int:
    rect = viewbox.targetRect()
    span = abs(rect.bottom() - rect.top())
    if span <= 0:
        return min_decimals

    # Bias toward finer labels and denser ticks sooner than finplot's
    # default heuristics so cents appear earlier during zoom-in.
    target_step = span / 18.0
    if target_step <= 0:
        return min_decimals

    desired_decimals = max(0, min(2, int(math.ceil(-math.log10(target_step))) + 2))
    if span <= 25.0:
        desired_decimals = max(desired_decimals, 2)
    if zooming_in:
        desired_decimals = max(desired_decimals, min_decimals)
    desired_eps = min(getattr(axis, "significant_eps", 1e-8), max(target_step / 20.0, 1e-8))

    axis.significant_decimals = max(getattr(axis, "significant_decimals", 0), desired_decimals)  # type: ignore[attr-defined]
    axis.significant_eps = desired_eps  # type: ignore[attr-defined]
    axis.significant_forced = True  # type: ignore[attr-defined]
    return desired_decimals


def _fmt_x_tick(s: str, mode: str) -> str:
    """
    Reformat a single finplot x-axis tick string for a given time mode.

    finplot produces ISO-style strings ('YYYY-MM-DD' or 'YYYY-MM-DD HH:MM').
    We trim these to compact, human-readable labels:
      months  : 'Jan \'25'
      weeks/days: 'Jan 6'
      hours   : 'Jan 6 10:00'
      minutes : '10:15'
    Years and seconds/milliseconds are left unchanged.
    """
    if not s:
        return s
    date_str, _, time_str = s.partition(" ")
    has_date = len(date_str) >= 10 and date_str[4] == "-"
    if not has_date:
        return s
    try:
        y, m, d = int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10])
        dt = date(y, m, d)
        if mode == "months":
            return dt.strftime("%b '%y")
        if mode in ("weeks", "days"):
            return f"{dt.strftime('%b')} {dt.day}"
        if mode == "hours":
            base = f"{dt.strftime('%b')} {dt.day}"
            return f"{base} {time_str}" if time_str else base
        if mode == "minutes":
            return time_str if time_str else s
    except (ValueError, IndexError):
        pass
    return s


def _patch_x_axis_format(ax: object) -> None:
    """
    Replace finplot's ISO-date tick labels with compact time-aware labels.

    Patches the bottom EpochAxisItem on ax so tickStrings reformats its
    output through _fmt_x_tick. The patch is idempotent — applied once per
    axis instance.
    """
    bottom_axis = getattr(ax, "axes", {}).get("bottom", {}).get("item")
    if bottom_axis is None or getattr(bottom_axis, "_simplechart_x_fmt_patch", False):
        return

    original_tick_strings = bottom_axis.tickStrings

    def _tick_strings(values: object, scale: object, spacing: object) -> list[str]:
        strs: list[str] = original_tick_strings(values, scale, spacing)
        mode: str = getattr(bottom_axis, "mode", "")
        if mode not in ("months", "weeks", "days", "hours", "minutes"):
            return strs
        return [_fmt_x_tick(s, mode) for s in strs]

    bottom_axis.tickStrings = _tick_strings
    bottom_axis._simplechart_x_fmt_patch = True


def _patch_price_axis_format(price_ax: object) -> None:
    axes = getattr(price_ax, "axes", {})
    right_axis = axes.get("right", {}).get("item")
    if right_axis is None or getattr(right_axis, "_simplechart_fmt_patch", False):
        return

    set_tick_density = getattr(right_axis, "setTickDensity", None)
    if callable(set_tick_density):
        set_tick_density(1.35)
    set_style = getattr(right_axis, "setStyle", None)
    if callable(set_style):
        set_style(
            maxTickLevel=2,
            textFillLimits=[
                (0, 1.00),
                (2, 0.90),
                (4, 0.80),
                (6, 0.70),
            ],
        )

    original_fmt_values = right_axis.fmt_values

    def _fmt_values(vs: object) -> object:
        result = original_fmt_values(vs)
        min_decimals = getattr(right_axis, "_min_decimals", 0)
        if min_decimals > 0 and getattr(right_axis, "next_fmt", "").endswith("f"):
            right_axis.next_fmt = f"%.{min_decimals}f"
        return result

    right_axis.fmt_values = _fmt_values
    right_axis._min_decimals = 0
    right_axis._simplechart_fmt_patch = True


def fmt_volume(v: float) -> str:
    """Format a raw volume value as a human-readable string with M/K suffix."""
    abs_v = abs(v)
    if abs_v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if abs_v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return f"{int(v)}"


def _patch_volume_axis_format(volume_ax: object) -> None:
    axes = getattr(volume_ax, "axes", {})
    right_axis = axes.get("right", {}).get("item")
    if right_axis is None or getattr(right_axis, "_simplechart_vol_fmt_patch", False):
        return

    def _vol_tick_values(
        minVal: float,
        maxVal: float,
        _size: float,
    ) -> list[tuple[float, list[float]]]:
        if maxVal <= minVal:
            return [(1, [])]
        span = maxVal - minVal
        raw_step = span / 6.0
        exp = 10.0 ** math.floor(math.log10(max(raw_step, 1.0)))
        mantissa = raw_step / exp
        if mantissa <= 1.0:
            step = exp
        elif mantissa <= 2.0:
            step = 2.0 * exp
        elif mantissa <= 5.0:
            step = 5.0 * exp
        else:
            step = 10.0 * exp
        step = max(step, 1.0)
        start = math.ceil(minVal / step) * step
        ticks: list[float] = []
        v = start
        while v <= maxVal + step * 0.001:
            ticks.append(float(v))
            v += step
        return [(step, ticks)]

    right_axis.tickValues = _vol_tick_values

    # Format tick labels as M/K strings. finplot's YAxisItem.tickStrings ignores
    # the `scale` parameter (it uses xform instead), so we do the same — pass
    # the raw volume value directly to fmt_volume rather than multiplying by
    # scale, which pyqtgraph may set to a SI prefix factor like 1e-6.
    right_axis.tickStrings = lambda values, scale, spacing: [
        fmt_volume(v) for v in values
    ]
    right_axis._simplechart_vol_fmt_patch = True

    # Format the crosshair reticle label on the volume panel.
    crosshair = getattr(volume_ax, "crosshair", None)
    if crosshair is not None:
        crosshair.infos.append(
            lambda x, y, xtext, _ytext: (xtext, fmt_volume(y))
        )


def _axis_min_decimals(viewbox: _ViewBoxLike) -> int:
    axis = getattr(viewbox, "parent", lambda: None)()
    if axis is None:
        return 0
    right_axis = axis.axes.get("right", {}).get("item")
    if right_axis is None:
        return 0
    return int(getattr(right_axis, "_min_decimals", 0))


def _clamp_axis_drag_range(viewbox: _ViewBoxLike, left: float, right: float) -> None:
    data_low, data_high = _data_y_bounds_for_window(viewbox, left, right)
    if data_high is None or data_low is None:
        return

    span = data_high - data_low
    upper_padding = max(abs(data_high) * _MAX_Y_OVERSHOOT_RATIO, span * _MAX_Y_OVERSHOOT_RATIO)
    lower_padding = max(abs(data_low) * _MAX_Y_OVERSHOOT_RATIO, span * _MAX_Y_OVERSHOOT_RATIO)

    min_visible_low = data_low - lower_padding
    max_visible_high = data_high + upper_padding

    current_rect = viewbox.targetRect()
    current_low = current_rect.top()
    current_high = current_rect.bottom()

    clamped_low = max(current_low, min_visible_low)
    clamped_high = min(current_high, max_visible_high)

    if clamped_high <= clamped_low:
        clamped_low = min_visible_low
        clamped_high = max_visible_high

    viewbox.set_range(left, clamped_low, right, clamped_high)


def _data_y_bounds_for_window(viewbox: _ViewBoxLike, left: float, right: float) -> tuple[float | None, float | None]:
    datasrc = viewbox.datasrc_or_standalone
    if datasrc is None:
        return None, None

    visible_left, visible_right = _visible_data_window(left, right, datasrc.xlen)
    if visible_right <= visible_left:
        return None, None

    _, _, high, low, count = datasrc.hilo(visible_left, visible_right)
    if count <= 0 or not _is_finite_range(high, low):
        return None, None
    return low, high


def _visible_data_window(left: float, right: float, xlen: int) -> tuple[float, float]:
    min_x = -fplt.side_margin
    max_x = xlen + fplt.right_margin_candles - fplt.side_margin
    return max(left, min_x), min(right, max_x)


def _is_finite_range(high: float, low: float) -> bool:
    return math.isfinite(high) and math.isfinite(low) and high > low


def _drag_delta(event: _DragEventLike) -> _PointLike:
    return _DragDelta(
        _x=event.pos().x() - event.buttonDownPos().x(),
        _y=event.pos().y() - event.buttonDownPos().y(),
    )
