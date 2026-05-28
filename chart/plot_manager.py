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
from typing import Any, cast

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from chart.panel import ExtensionPanelSlot, Panel
from chart.styles import (
    BACKGROUND,
    CANDLE_DOWN,
    CANDLE_UP,
    LINE_WIDTH_INDICATOR,
    VOLUME_DOWN,
    VOLUME_UP,
)
from chart.viewport import (
    apply_interaction_modes,
    restore_viewports,
    snapshot_viewports,
    unlock_x_pan,
)
from data.models import OHLCVSeries
from simplechart.api import HorizontalSegmentRender, MarkerRender, VerticalLineRender

_LINE_STYLES: dict[str, Qt.PenStyle] = {
    "solid":    Qt.PenStyle.SolidLine,
    "dash":     Qt.PenStyle.DashLine,
    "dot":      Qt.PenStyle.DotLine,
    "dash_dot": Qt.PenStyle.DashDotLine,
}


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
    ) -> None:
        self._price_panel     = price_panel
        self._volume_panel    = volume_panel
        self._extension_slots = extension_slots

        # Keys follow the render keys produced by extensions.
        self._plots: dict[str, object] = {}
        self._segments: dict[str, tuple[object, object]] = {}
        self._vertical_lines: dict[str, tuple[object, object]] = {}
        self._markers: dict[str, object] = {}

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
        import finplot as fplt

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
            cast(Any, self._candle_plot).colors.update({
                "bull_body":   BACKGROUND,    # hollow
                "bull_frame":  CANDLE_UP,
                "bull_shadow": CANDLE_UP,
                "bear_body":   CANDLE_DOWN,
                "bear_frame":  CANDLE_DOWN,
                "bear_shadow": CANDLE_DOWN,
            })

        unlock_x_pan(self._price_panel.ax)

    def draw_volume(self, series: OHLCVSeries) -> None:
        """
        Draw or replace the volume bar plot.

        finplot's volume_ocv() expects a DataFrame with columns:
        open, close, volume, indexed by bar timestamps.
        """
        import finplot as fplt

        df = _series_to_volume_df(series)

        if self._volume_plot is not None:
            self._volume_plot.update_data(df)  # type: ignore[attr-defined]
        else:
            self._volume_plot = fplt.volume_ocv(
                df,
                ax=self._volume_panel.ax,
                colorfunc=fplt.volume_colorfilter,
            )
            cast(Any, self._volume_plot).colors.update({
                "bull_frame": VOLUME_UP,
                "bull_body":  VOLUME_UP,
                "bear_frame": VOLUME_DOWN,
                "bear_body":  VOLUME_DOWN,
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
        import finplot as fplt

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

    @contextmanager
    def preserving_viewport(self) -> Iterator[None]:
        snapshot = snapshot_viewports(self._viewport_axes())
        try:
            yield
        finally:
            restore_viewports(snapshot)

    def refresh(self, *, preserve_view: bool = False) -> None:
        import finplot as fplt

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
        if series_key in self._markers:
            self._markers[series_key].setVisible(visible)  # type: ignore[attr-defined]

    def clear_all(self) -> None:
        """Remove everything including candles and volume."""
        # ax.reset() (set up by finplot on each axis) removes all items from
        # the scene and calls vb.reset() — which clears standalones, rois, and
        # datasrc. This is more thorough than calling removeItem individually
        # and prevents extension plots from one symbol bleeding onto another.
        self._price_panel.ax.reset()
        self._volume_panel.ax.reset()

        self._plots.clear()
        self._segments.clear()
        self._vertical_lines.clear()
        self._markers.clear()
        self._candle_plot = None
        self._volume_plot = None
        self._bar_index   = None

    def active_series_keys(self) -> list[str]:
        """Return the keys of all currently drawn extension render items."""
        return (
            list(self._plots.keys())
            + list(self._segments.keys())
            + list(self._vertical_lines.keys())
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
            return

        self.remove_marker(marker.key)
        item = pg.TextItem(
            text=marker.text,
            color=marker.color,
            anchor=(0.5, 0.72),
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

    def remove_marker(self, marker_key: str) -> None:
        # O(1) via the tracking dict — avoids scanning every item on the axis on
        # each call (hot during drag). Orphaned items not in the dict are swept
        # by scrub_orphan_markers, which runs once per full render.
        handle = self._markers.pop(marker_key, None)
        if handle is not None:
            self._price_panel.ax.removeItem(handle)

    def scrub_orphan_markers(self, active_marker_keys: set[str]) -> None:
        for marker_key in list(self._markers):
            if marker_key not in active_marker_keys:
                self.remove_marker(marker_key)
        for item in list(self._price_panel.ax.items):
            item_marker_key = getattr(item, "_simplechart_marker_key", None)
            if isinstance(item_marker_key, str) and item_marker_key not in active_marker_keys:
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

def _bar_datetime_index(series: OHLCVSeries) -> pd.Index:
    """
    Return the UTC DatetimeIndex finplot uses as the source x-axis.
    """
    return pd.DatetimeIndex(
        [bar.timestamp for bar in series.bars],
        dtype="datetime64[ns, UTC]",
    )


def _series_to_candle_df(series: OHLCVSeries) -> pd.DataFrame:
    """
    Build the DataFrame finplot expects for candlestick_ochl().
    Column order is OCHL — finplot's required convention.
    """
    idx = _bar_datetime_index(series)
    return pd.DataFrame(
        {
            "open":  [b.open  for b in series.bars],
            "close": [b.close for b in series.bars],
            "high":  [b.high  for b in series.bars],
            "low":   [b.low   for b in series.bars],
        },
        index=idx,
    )


def _series_to_volume_df(series: OHLCVSeries) -> pd.DataFrame:
    """
    Build the DataFrame finplot expects for volume_ocv().
    """
    idx = _bar_datetime_index(series)
    return pd.DataFrame(
        {
            "open":   [b.open   for b in series.bars],
            "close":  [b.close  for b in series.bars],
            "volume": [b.volume for b in series.bars],
        },
        index=idx,
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
