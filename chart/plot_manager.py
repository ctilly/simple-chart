"""
chart/plot_manager.py

Owns all active finplot plot objects and is the only place in the app
where finplot draw calls are made.

Responsibilities:
  - Draw and update candlesticks and volume bars when a new symbol loads
  - Create, update, and remove extension render items
  - Show/hide extension render items (for legend toggles)
  - Clear everything when switching symbols

Why a dedicated manager?
    finplot and pyqtgraph return handles when drawing. Keeping one registry
    for line plots, horizontal segments, and markers lets updates replace the
    intended item instead of layering duplicates on the chart.

Note on pandas:
    finplot requires data formatted as a pandas DataFrame for candlesticks
    and volume. Pandas is already a transitive dependency of finplot, so
    this is not an additional dependency — we just use it here in the chart
    layer for the specific purpose of preparing data for finplot. It does
    not appear anywhere else in the codebase.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, cast

import finplot as fplt
import numpy as np
import pandas as pd
import pyqtgraph as pg
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QFont, QFontInfo, QPainter

from chart.panel import ExtensionPanelSlot, Panel
from chart.styles import (
    ChartColors,
    LAST_PRICE_LINE_ALPHA,
    LAST_PRICE_TAG_ALPHA,
    LAST_PRICE_TAG_TEXT,
    LINE_WIDTH_INDICATOR,
)
from chart.viewport import (
    apply_interaction_modes,
    restore_viewports,
    shift_viewports,
    snapshot_viewports,
    unlock_x_pan,
)
from data.models import OHLCVSeries, Timeframe
from simplechart.api import (
    AxisPriceLabelRender,
    HorizontalLineRender,
    HorizontalSegmentRender,
    MarkerRender,
    PolylineRender,
    VerticalLineRender,
    series_arrays,
)

_LINE_STYLES: dict[str, Qt.PenStyle] = {
    "solid":    Qt.PenStyle.SolidLine,
    "dash":     Qt.PenStyle.DashLine,
    "dot":      Qt.PenStyle.DotLine,
    "dash_dot": Qt.PenStyle.DashDotLine,
}


@dataclass
class _AxisPriceLabelHandle:
    text_item: object
    viewbox: object
    scene: object
    reposition: Callable[..., None]
    y_value: float


# The last-price tag is smaller than the horizontal-line tool's tags: a
# smaller font, less padding, and rounded corners.
_LAST_PRICE_TAG_POINTS_SMALLER = 3
_LAST_PRICE_TAG_PADDING = 1
_LAST_PRICE_TAG_CORNER_RADIUS = 3.0


class _RoundedTag(pg.TextItem):  # type: ignore[misc]
    """An axis tag with rounded corners; pg.TextItem fills a square box."""

    def __init__(self, text: str, text_color: str, fill_color: str) -> None:
        super().__init__(text=text, color=text_color, anchor=(0.0, 0.5))
        self._tag_brush = pg.mkBrush(fill_color)

    def paint(self, p: QPainter, *args: object) -> None:
        # The base class has no fill or border here; it only keeps the
        # item's transform current.
        super().paint(p, *args)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._tag_brush)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.drawRoundedRect(
            self.textItem.mapRectToParent(self.textItem.boundingRect()),
            _LAST_PRICE_TAG_CORNER_RADIUS,
            _LAST_PRICE_TAG_CORNER_RADIUS,
        )


class PlotManager:
    """
    Central registry of finplot plot handles.

    One PlotManager exists per chart window. The controller calls its
    methods whenever data or extension state changes.
    """

    def __init__(
        self,
        price_panel: Panel,
        volume_panel: Panel,
        extension_slots: list[ExtensionPanelSlot],
        colors: ChartColors = ChartColors(),
    ) -> None:
        self._colors          = colors
        self._price_panel     = price_panel
        self._volume_panel    = volume_panel
        self._extension_slots = extension_slots

        # Keys follow the render keys produced by extensions.
        self._plots: dict[str, object] = {}
        self._segments: dict[str, tuple[object, object]] = {}
        self._vertical_lines: dict[str, tuple[object, object]] = {}
        self._horizontal_lines: dict[str, tuple[object, object]] = {}
        self._polylines: dict[str, tuple[object, object]] = {}
        self._axis_price_labels: dict[str, _AxisPriceLabelHandle] = {}
        self._markers: dict[str, object] = {}

        # The last-price line and its axis tag belong to the chart, not to an
        # extension, so they stay out of the registries above (stale
        # extension renders are pruned from those). The newest bar's
        # (index, close) is kept while the marker is turned off.
        self._last_price_visible = True
        self._last_price: tuple[int, float] | None = None
        self._last_price_line: pg.PlotDataItem | None = None
        self._last_price_label: _AxisPriceLabelHandle | None = None

        # Candle and volume handles are separate — they are always present
        # and replaced entirely on symbol load rather than updated in place.
        self._candle_plot: object | None = None
        self._volume_plot: object | None = None

        # DatetimeIndex of the most recently drawn bar series. Stored so
        # extension plots can use the same time axis as the candles.
        # Without this, finplot aligns extension lines by integer array position
        # rather than time, which breaks during pan/zoom.
        self._bar_index: pd.DatetimeIndex | None = None

    # ------------------------------------------------------------------
    # Price and volume
    # ------------------------------------------------------------------

    def draw_candles(self, series: OHLCVSeries) -> None:
        """
        Draw or replace the candlestick plot for the loaded series.

        finplot's candlestick_ochl() expects a DataFrame with columns in
        the order: open, close, high, low, indexed by bar timestamps.
        The column order is OCHL (not OHLC) — finplot's convention.
        """
        df = _series_to_candle_df(series)
        self._bar_index = df.index  # save for extension alignment

        if self._candle_plot is not None:
            self._candle_plot.update_data(df)  # type: ignore[attr-defined]
        else:
            self._candle_plot = fplt.candlestick_ochl(
                df,
                ax=self._price_panel.ax,
                colorfunc=fplt.price_colorfilter,
            )
            # Hollow/solid scheme: bull candles have a white body (hollow)
            # with a green outline and wick; bear candles are fully red.
            colors = self._colors
            cast(Any, self._candle_plot).colors.update({
                "bull_body":   colors.hollow_body,
                "bull_frame":  colors.candle_up,
                "bull_shadow": colors.candle_up,
                "bear_body":   colors.candle_down,
                "bear_frame":  colors.candle_down,
                "bear_shadow": colors.candle_down,
            })

        unlock_x_pan(self._price_panel.ax)
        self._update_last_price(series)

    def set_last_price_visible(self, visible: bool) -> None:
        """Show or hide the last-price line and tag (an application setting)."""
        self._last_price_visible = visible
        self._draw_last_price()

    def _update_last_price(self, series: OHLCVSeries) -> None:
        self._last_price = (
            (len(series.bars) - 1, series.bars[-1].close) if series.bars else None
        )
        self._draw_last_price()

    def _draw_last_price(self) -> None:
        """
        A line from the newest bar's close to the view's right edge, and a
        small tag on the price axis, shown while the close is in view.
        """
        self._remove_last_price()
        if not self._last_price_visible or self._last_price is None:
            return
        index, close = self._last_price
        ax = self._price_panel.ax
        viewbox = ax.vb
        x_start = self._x_value_for_index(index)
        line = pg.PlotDataItem(
            [x_start, x_start],
            [close, close],
            pen=pg.mkPen(color=self._colors.last_price + LAST_PRICE_LINE_ALPHA, width=1),
        )
        ax.addItem(line, ignoreBounds=True)

        def follow_view(text_item: pg.TextItem) -> None:
            rect = viewbox.viewRect()
            line.setData([x_start, max(x_start, rect.right())], [close, close])
            text_item.setVisible(rect.top() <= close <= rect.bottom())

        text_item = _RoundedTag(
            f"{close:.2f}",
            LAST_PRICE_TAG_TEXT,
            self._colors.last_price + LAST_PRICE_TAG_ALPHA,
        )
        text_item.textItem.document().setDocumentMargin(_LAST_PRICE_TAG_PADDING)
        font = text_item.textItem.font()
        # QFontInfo resolves pixel-sized fonts, whose pointSizeF() is -1.
        font.setPointSizeF(QFontInfo(font).pointSizeF() - _LAST_PRICE_TAG_POINTS_SMALLER)
        text_item.setFont(font)
        label = self._add_axis_price_label(viewbox, close, text_item, follow_view)
        # Above the horizontal-line tool's tags, so the stacking does not
        # change from one redraw to the next.
        text_item.setZValue(1)
        self._last_price_line = line
        self._last_price_label = label

    def _remove_last_price(self) -> None:
        if self._last_price_line is not None:
            self._price_panel.ax.removeItem(self._last_price_line)
            self._last_price_line = None
        if self._last_price_label is not None:
            self._discard_axis_price_label(self._last_price_label)
            self._last_price_label = None

    def draw_volume(self, series: OHLCVSeries) -> None:
        """
        Draw or replace the volume bar plot.

        finplot's volume_ocv() expects a DataFrame with columns:
        open, close, volume, indexed by bar timestamps.
        """
        df = _series_to_volume_df(series)

        if self._volume_plot is not None:
            self._volume_plot.update_data(df)  # type: ignore[attr-defined]
        else:
            self._volume_plot = fplt.volume_ocv(
                df,
                ax=self._volume_panel.ax,
                colorfunc=fplt.volume_colorfilter,
            )
            colors = self._colors
            cast(Any, self._volume_plot).colors.update({
                "bull_frame": colors.volume_up,
                "bull_body":  colors.volume_up,
                "bear_frame": colors.volume_down,
                "bear_body":  colors.volume_down,
            })

        unlock_x_pan(self._volume_panel.ax)
        apply_interaction_modes(self._price_panel.ax, self._volume_panel.ax)

    # ------------------------------------------------------------------
    # Extensions
    # ------------------------------------------------------------------

    def update_extension(
        self,
        series_key: str,
        values: np.ndarray,
        color: str,
        width: float = LINE_WIDTH_INDICATOR,
        style: str = "solid",
        render_target: str = "chart",
    ) -> None:
        """
        Draw or update a single extension plot line.

        render_target routes the draw to the correct axis:
          "chart"      → price panel (default, chart extensions)
          any other str → the extension panel slot assigned that name

        If a plot for series_key already exists, its data is updated in
        place. If it does not exist, a new line is created on the
        appropriate axis.

        values is a numpy array aligned to the current bar series. NaN
        values are not drawn — finplot skips them automatically.
        """
        pen_style = _LINE_STYLES.get(style, Qt.PenStyle.SolidLine)

        # Wrap the numpy array in a pandas Series with the same DatetimeIndex
        # as the candles. Without this, finplot uses integer array positions
        # as x-coordinates, which causes extension lines to drift during
        # pan/zoom because finplot's x-indexed mode can't reconcile the two
        # datasrcs.
        if self._bar_index is not None:
            data: object = pd.Series(values, index=self._bar_index)
        else:
            data = values

        if series_key in self._plots:
            handle = cast(Any, self._plots[series_key])
            # Update pen BEFORE update_data. Internally, update_data calls
            # finplot's _start_visual_update → pg setData → updateItems with
            # styleUpdate=True, which reads opts['pen']. Setting the pen first
            # ensures the new color is in opts['pen'] when that re-render runs.
            handle.setPen(
                pg.mkPen(color=color, width=width, style=pen_style)
            )
            handle.opts["handed_color"] = color
            handle.update_data(data)
        else:
            target_ax = self._resolve_ax(render_target)
            handle = cast(Any, fplt.plot(
                data,
                ax=target_ax,
                color=color,
                width=width,
            ))
            handle.setPen(
                pg.mkPen(color=color, width=width, style=pen_style)
            )
            self._plots[series_key] = handle

    def update_horizontal_segment(self, segment: HorizontalSegmentRender) -> None:
        target_ax = self._resolve_ax(segment.render_target)
        x_start = self._x_value_for_index(segment.x_start)
        x_end = self._x_value_for_index(segment.x_end)
        pen_style = _LINE_STYLES.get(segment.line_style, Qt.PenStyle.SolidLine)
        pen = pg.mkPen(
            color=segment.color,
            width=segment.line_width,
            style=pen_style,
        )
        if segment.key in self._segments:
            item, current_ax = self._segments[segment.key]
            if current_ax is target_ax:
                handle = cast(Any, item)
                handle.setPen(pen)
                handle.setData([x_start, x_end], [segment.y_value, segment.y_value])
                handle.setVisible(segment.visible)
                return
            self.remove_horizontal_segment(segment.key)

        item = pg.PlotDataItem(
            [x_start, x_end],
            [segment.y_value, segment.y_value],
            pen=pen,
        )
        item.setVisible(segment.visible)
        target_ax.addItem(item)  # type: ignore[attr-defined]
        self._segments[segment.key] = (item, target_ax)

    def update_polyline(self, polyline: PolylineRender) -> None:
        target_ax = self._resolve_ax(polyline.render_target)
        xs = [self._x_value_for_index(x) for x, _ in polyline.points]
        ys = [y for _, y in polyline.points]
        pen_style = _LINE_STYLES.get(polyline.line_style, Qt.PenStyle.SolidLine)
        pen = pg.mkPen(
            color=polyline.color,
            width=polyline.line_width,
            style=pen_style,
        )
        if polyline.key in self._polylines:
            item, current_ax = self._polylines[polyline.key]
            if current_ax is target_ax:
                handle = cast(Any, item)
                handle.setPen(pen)
                handle.setData(xs, ys)
                handle.setVisible(polyline.visible)
                return
            self.remove_polyline(polyline.key)

        item = pg.PlotDataItem(xs, ys, pen=pen)
        item.setVisible(polyline.visible)
        target_ax.addItem(item)  # type: ignore[attr-defined]
        self._polylines[polyline.key] = (item, target_ax)

    def update_vertical_line(self, line: VerticalLineRender) -> None:
        self.remove_vertical_line(line.key)
        target_ax = self._resolve_ax(line.render_target)
        x_value = self._x_value_for_index(line.x_index)
        pen_style = _LINE_STYLES.get(line.line_style, Qt.PenStyle.SolidLine)
        item = pg.InfiniteLine(
            pos=x_value,
            angle=90,
            movable=False,
            pen=pg.mkPen(
                color=line.color,
                width=line.line_width,
                style=pen_style,
            ),
        )
        item.setVisible(line.visible)
        target_ax.addItem(item)  # type: ignore[attr-defined]
        self._vertical_lines[line.key] = (item, target_ax)

    def update_horizontal_line(self, line: HorizontalLineRender) -> None:
        self.remove_horizontal_line(line.key)
        target_ax = self._resolve_ax(line.render_target)
        pen_style = _LINE_STYLES.get(line.line_style, Qt.PenStyle.SolidLine)
        item = pg.InfiniteLine(
            pos=line.y_value,
            angle=0,
            movable=False,
            pen=pg.mkPen(
                color=line.color,
                width=line.line_width,
                style=pen_style,
            ),
        )
        item.setVisible(line.visible)
        target_ax.addItem(item)  # type: ignore[attr-defined]
        self._horizontal_lines[line.key] = (item, target_ax)

    def update_axis_price_label(self, label: AxisPriceLabelRender) -> None:
        self.remove_axis_price_label(label.key)
        target_ax = self._resolve_ax(label.render_target)
        text_item = pg.TextItem(
            text=label.text,
            color=label.text_color,
            anchor=(0.0, 0.5),
            fill=pg.mkBrush(label.fill_color),
            border=pg.mkPen(label.fill_color),
        )
        handle = self._add_axis_price_label(
            target_ax.vb,  # type: ignore[attr-defined]
            label.y_value,
            text_item,
        )
        text_item.setVisible(label.visible)
        self._axis_price_labels[label.key] = handle

    def _add_axis_price_label(
        self,
        viewbox: Any,
        y_value: float,
        text_item: pg.TextItem,
        on_view_changed: Callable[[pg.TextItem], None] | None = None,
    ) -> _AxisPriceLabelHandle:
        """
        Put text_item on the price axis at y_value and keep it there as the
        view pans, zooms and resizes. on_view_changed runs after each
        reposition.
        """
        scene = viewbox.scene()
        scene.addItem(text_item)

        def reposition(*_args: object) -> None:
            scene_x = viewbox.sceneBoundingRect().right()
            scene_y = viewbox.mapViewToScene(QPointF(0.0, y_value)).y()
            text_item.setPos(scene_x, scene_y)
            if on_view_changed is not None:
                on_view_changed(text_item)

        viewbox.sigRangeChanged.connect(reposition)
        viewbox.sigResized.connect(reposition)
        reposition()
        return _AxisPriceLabelHandle(
            text_item=text_item,
            viewbox=viewbox,
            scene=scene,
            reposition=reposition,
            y_value=y_value,
        )

    @contextmanager
    def preserving_viewport(self, x_shift: float = 0.0) -> Iterator[None]:
        snapshot = snapshot_viewports(self._viewport_axes())
        try:
            yield
        finally:
            restore_viewports(shift_viewports(snapshot, x_shift))

    def refresh(self, *, preserve_view: bool = False) -> None:
        if not preserve_view:
            fplt.refresh()
            return

        snapshot = snapshot_viewports(self._viewport_axes())
        restore_viewports(snapshot)
        fplt.refresh()
        restore_viewports(snapshot)

    def _resolve_ax(self, render_target: str) -> object:
        """Return the finplot axis for the given render_target string."""
        if render_target == "chart":
            return self._price_panel.ax
        for slot in self._extension_slots:
            if slot.name == render_target:
                return slot.panel.ax
        raise KeyError(f"No extension panel slot assigned for target {render_target!r}")

    def remove_extension(self, series_key: str) -> None:
        """
        Remove an extension render item from the chart.

        Called when the user deletes an extension-owned render item or
        removes an extension entirely.
        """
        if series_key in self._plots:
            handle = self._plots.pop(series_key)
            handle.ax.removeItem(handle)  # type: ignore[attr-defined]
        self.remove_horizontal_segment(series_key)
        self.remove_vertical_line(series_key)
        self.remove_horizontal_line(series_key)
        self.remove_polyline(series_key)
        self.remove_axis_price_label(series_key)
        self.remove_marker(series_key)

    def set_visible(self, series_key: str, visible: bool) -> None:
        """
        Show or hide an extension render item without removing it.

        Used by the legend toggle — hiding keeps the plot handle alive
        so it can be shown again without recomputing.
        """
        if series_key in self._plots:
            self._plots[series_key].setVisible(visible)  # type: ignore[attr-defined]
        if series_key in self._segments:
            self._segments[series_key][0].setVisible(visible)  # type: ignore[attr-defined]
        if series_key in self._vertical_lines:
            self._vertical_lines[series_key][0].setVisible(visible)  # type: ignore[attr-defined]
        if series_key in self._horizontal_lines:
            self._horizontal_lines[series_key][0].setVisible(visible)  # type: ignore[attr-defined]
        if series_key in self._polylines:
            self._polylines[series_key][0].setVisible(visible)  # type: ignore[attr-defined]
        if series_key in self._axis_price_labels:
            self._axis_price_labels[series_key].text_item.setVisible(visible)  # type: ignore[attr-defined]
        if series_key in self._markers:
            self._markers[series_key].setVisible(visible)  # type: ignore[attr-defined]

    def clear_all(self) -> None:
        """Remove everything including candles and volume."""
        self._remove_last_price()
        self._last_price = None
        # ax.reset() (set up by finplot on each axis) removes all items from
        # the scene and calls vb.reset() — which clears standalones, rois, and
        # datasrc. This is more thorough than calling removeItem individually
        # and prevents extension plots from one symbol bleeding onto another.
        self._price_panel.ax.reset()
        self._volume_panel.ax.reset()

        # ax.reset() removes items inside the viewbox; axis price labels are
        # scene-level items that survive otherwise, so disconnect their signals
        # and pull them off the scene explicitly.
        for label_key in list(self._axis_price_labels):
            self.remove_axis_price_label(label_key)
        for marker_key in list(self._markers):
            self.remove_marker(marker_key)
        self.scrub_orphan_markers(set())

        self._plots.clear()
        self._segments.clear()
        self._vertical_lines.clear()
        self._horizontal_lines.clear()
        self._polylines.clear()
        self._candle_plot = None
        self._volume_plot = None
        self._bar_index   = None

    def active_series_keys(self) -> list[str]:
        """Return the keys of all currently drawn extension render items."""
        return (
            list(self._plots.keys())
            + list(self._segments.keys())
            + list(self._vertical_lines.keys())
            + list(self._horizontal_lines.keys())
            + list(self._polylines.keys())
            + list(self._axis_price_labels.keys())
        )

    def active_marker_keys(self) -> list[str]:
        return list(self._markers.keys())

    def price_viewbox(self) -> object:
        return self._price_panel.ax.vb

    def _viewport_axes(self) -> list[object]:
        axes = [self._price_panel.ax]
        axes.extend(
            slot.panel.ax
            for slot in self._extension_slots
            if slot.name is not None
        )
        return axes

    def update_marker(self, marker: MarkerRender) -> None:
        """Draw or update a marker on the main price panel."""
        x_value = _x_value_for_index(
            float(marker.x_index),
            self._bar_index,
            bool(self._price_panel.ax.vb.x_indexed),
        )
        if marker.key in self._markers:
            item = cast(Any, self._markers[marker.key])
            item.setText(marker.text, color=marker.color)
            font = QFont()
            font.setPointSize(marker.font_size)
            item.setFont(font)
            item.setPos(x_value, marker.y_value)
            item.setZValue(marker.z)
            item.setVisible(marker.visible)
            self._remove_marker_items(marker.key, keep=item)
            return

        self.remove_marker(marker.key)
        self._remove_marker_items(marker.key)
        item = pg.TextItem(
            text=marker.text,
            color=marker.color,
            anchor=marker.anchor,
        )
        font = QFont()
        font.setPointSize(marker.font_size)
        item.setFont(font)
        item.setPos(x_value, marker.y_value)
        item.setZValue(marker.z)
        item.setVisible(marker.visible)
        item._simplechart_marker_key = marker.key
        self._price_panel.ax.addItem(item)
        self._markers[marker.key] = item

    def remove_horizontal_segment(self, segment_key: str) -> None:
        if segment_key in self._segments:
            item, target_ax = self._segments.pop(segment_key)
            target_ax.removeItem(item)  # type: ignore[attr-defined]

    def remove_vertical_line(self, line_key: str) -> None:
        if line_key in self._vertical_lines:
            item, target_ax = self._vertical_lines.pop(line_key)
            target_ax.removeItem(item)  # type: ignore[attr-defined]

    def remove_horizontal_line(self, line_key: str) -> None:
        if line_key in self._horizontal_lines:
            item, target_ax = self._horizontal_lines.pop(line_key)
            target_ax.removeItem(item)  # type: ignore[attr-defined]

    def remove_polyline(self, polyline_key: str) -> None:
        if polyline_key in self._polylines:
            item, target_ax = self._polylines.pop(polyline_key)
            target_ax.removeItem(item)  # type: ignore[attr-defined]

    def remove_axis_price_label(self, label_key: str) -> None:
        handle = self._axis_price_labels.pop(label_key, None)
        if handle is not None:
            self._discard_axis_price_label(handle)

    def _discard_axis_price_label(self, handle: _AxisPriceLabelHandle) -> None:
        try:
            handle.viewbox.sigRangeChanged.disconnect(handle.reposition)  # type: ignore[attr-defined]
        except (TypeError, RuntimeError):
            pass
        try:
            handle.viewbox.sigResized.disconnect(handle.reposition)  # type: ignore[attr-defined]
        except (TypeError, RuntimeError):
            pass
        handle.scene.removeItem(handle.text_item)  # type: ignore[attr-defined]

    def remove_marker(self, marker_key: str) -> None:
        # O(1) via the tracking dict — avoids scanning every item on the axis on
        # each call (hot during drag). Orphaned items not in the dict are swept
        # by scrub_orphan_markers, which runs once per full render.
        handle = self._markers.pop(marker_key, None)
        if handle is not None:
            self._price_panel.ax.removeItem(handle)
        self._remove_marker_items(marker_key)

    def scrub_orphan_markers(self, active_marker_keys: set[str]) -> None:
        for marker_key in list(self._markers):
            if marker_key not in active_marker_keys:
                self.remove_marker(marker_key)
        for item in list(self._price_panel.ax.items):
            item_marker_key = getattr(item, "_simplechart_marker_key", None)
            if isinstance(item_marker_key, str) and item_marker_key not in active_marker_keys:
                self._price_panel.ax.removeItem(item)

    def _remove_marker_items(self, marker_key: str, keep: object | None = None) -> None:
        for item in list(self._price_panel.ax.items):
            if item is keep:
                continue
            if getattr(item, "_simplechart_marker_key", None) == marker_key:
                self._price_panel.ax.removeItem(item)

    def _x_value_for_index(self, index: float) -> float:
        return _x_value_for_index(
            index,
            self._bar_index,
            bool(self._price_panel.ax.vb.x_indexed),
        )


# ------------------------------------------------------------------
# Private helpers — data formatting for finplot
# ------------------------------------------------------------------

_DAY_MS = 86_400_000
_HALF_DAY_MS = 43_200_000


def _bar_datetime_index(series: OHLCVSeries) -> pd.Index:
    """
    Return the UTC DatetimeIndex finplot uses as the source x-axis. Daily
    and weekly bars sit at 12:00 UTC of their UTC date.
    """
    timestamps_ms = series_arrays(series).timestamps_ms
    if series.timeframe in (Timeframe.DAILY, Timeframe.WEEKLY):
        timestamps_ms = timestamps_ms // _DAY_MS * _DAY_MS + _HALF_DAY_MS
    return pd.DatetimeIndex(
        pd.to_datetime(timestamps_ms, unit="ms", utc=True),
        dtype="datetime64[ns, UTC]",
    )


def _series_to_candle_df(series: OHLCVSeries) -> pd.DataFrame:
    """
    Build the DataFrame finplot expects for candlestick_ochl().
    Column order is OCHL — finplot's required convention.
    """
    arrays = series_arrays(series)
    return pd.DataFrame(
        {
            "open": arrays.open,
            "close": arrays.close,
            "high": arrays.high,
            "low": arrays.low,
        },
        index=_bar_datetime_index(series),
    )


def _series_to_volume_df(series: OHLCVSeries) -> pd.DataFrame:
    """
    Build the DataFrame finplot expects for volume_ocv().
    """
    arrays = series_arrays(series)
    return pd.DataFrame(
        {
            "open": arrays.open,
            "close": arrays.close,
            "volume": arrays.volume.astype(np.int64),
        },
        index=_bar_datetime_index(series),
    )


def _x_value_for_index(
    index: float,
    bar_index: pd.DatetimeIndex | None,
    x_indexed: bool,
) -> float:
    if x_indexed or bar_index is None:
        return float(index)
    if len(bar_index) == 0:
        return float(index)

    lower_index = int(index)
    fraction = index - lower_index
    if 0 <= lower_index < len(bar_index):
        base = float(bar_index[lower_index].value)
        if lower_index + 1 < len(bar_index):
            step = float(bar_index[lower_index + 1].value) - base
            return base + (step * fraction)
        if lower_index > 0:
            step = base - float(bar_index[lower_index - 1].value)
            return base + (step * fraction)
        return base

    if lower_index >= len(bar_index) and len(bar_index) > 1:
        last = float(bar_index[-1].value)
        step = last - float(bar_index[-2].value)
        return last + (step * (index - (len(bar_index) - 1)))

    if lower_index < 0 and len(bar_index) > 1:
        first = float(bar_index[0].value)
        step = float(bar_index[1].value) - first
        return first + (step * index)

    return float(index)
