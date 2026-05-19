"""
chart/plot_manager.py

Owns all active finplot plot objects and is the only place in the app
where finplot draw calls are made.

Responsibilities:
  - Draw and update candlesticks and volume bars when a new symbol loads
  - Create, update, and remove indicator render items
  - Show/hide indicator render items (for legend toggles)
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

from chart.panel import IndicatorPanelSlot, Panel
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
from simplechart.api import HorizontalSegmentRender, MarkerRender

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
    methods whenever data or indicator state changes.
    """

    def __init__(
        self,
        price_panel: Panel,
        volume_panel: Panel,
        indicator_slots: list[IndicatorPanelSlot],
    ) -> None:
        self._price_panel     = price_panel
        self._volume_panel    = volume_panel
        self._indicator_slots = indicator_slots

        # Keys follow the render keys produced by indicators.
        self._plots: dict[str, object] = {}
        self._segments: dict[str, tuple[object, object]] = {}
        self._markers: dict[str, object] = {}

        # Candle and volume handles are separate — they are always present
        # and replaced entirely on symbol load rather than updated in place.
        self._candle_plot: object | None = None
        self._volume_plot: object | None = None

        # DatetimeIndex of the most recently drawn bar series. Stored so
        # indicator plots can use the same time axis as the candles.
        # Without this, finplot aligns indicators by integer array position
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
        self._bar_index = df.index  # save for indicator alignment

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
    # Indicators
    # ------------------------------------------------------------------

    def update_indicator(
        self,
        series_key: str,
        values: np.ndarray,
        color: str,
        width: float = LINE_WIDTH_INDICATOR,
        style: str = "solid",
        render_target: str = "chart",
    ) -> None:
        """
        Draw or update a single indicator plot line.

        render_target routes the draw to the correct axis:
          "chart"      → price panel (default, chart indicators)
          any other str → the indicator panel slot assigned that name

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
        # as x-coordinates, which causes indicator lines to drift during
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
        self.remove_horizontal_segment(segment.key)
        target_ax = self._resolve_ax(segment.render_target)
        x_start = self._x_value_for_index(segment.x_start)
        x_end = self._x_value_for_index(segment.x_end)
        pen_style = _LINE_STYLES.get(segment.line_style, Qt.PenStyle.SolidLine)
        item = pg.PlotDataItem(
            [x_start, x_end],
            [segment.y_value, segment.y_value],
            pen=pg.mkPen(
                color=segment.color,
                width=segment.line_width,
                style=pen_style,
            ),
        )
        item.setVisible(segment.visible)
        target_ax.addItem(item)  # type: ignore[attr-defined]
        self._segments[segment.key] = (item, target_ax)

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
        for slot in self._indicator_slots:
            if slot.name == render_target:
                return slot.panel.ax
        raise KeyError(f"No indicator panel slot assigned for target {render_target!r}")

    def remove_indicator(self, series_key: str) -> None:
        """
        Remove an indicator plot line from the chart.

        Called when the user deletes an indicator-owned render item or
        removes an indicator entirely.
        """
        if series_key in self._plots:
            handle = self._plots.pop(series_key)
            handle.ax.removeItem(handle)  # type: ignore[attr-defined]
        self.remove_horizontal_segment(series_key)
        self.remove_marker(series_key)

    def set_visible(self, series_key: str, visible: bool) -> None:
        """
        Show or hide an indicator plot line without removing it.

        Used by the legend toggle — hiding keeps the plot handle alive
        so it can be shown again without recomputing.
        """
        if series_key in self._plots:
            self._plots[series_key].setVisible(visible)  # type: ignore[attr-defined]
        if series_key in self._segments:
            self._segments[series_key][0].setVisible(visible)  # type: ignore[attr-defined]
        if series_key in self._markers:
            self._markers[series_key].setVisible(visible)  # type: ignore[attr-defined]

    def clear_indicators(self) -> None:
        """
        Remove all indicator lines. Called when switching symbols.

        Candles and volume are replaced via draw_candles/draw_volume,
        so they are not cleared here.
        """
        for handle in self._plots.values():
            handle.ax.removeItem(handle)  # type: ignore[attr-defined]
        self._plots.clear()
        for item, target_ax in self._segments.values():
            target_ax.removeItem(item)  # type: ignore[attr-defined]
        self._segments.clear()
        for handle in self._markers.values():
            self._price_panel.ax.removeItem(handle)
        self._markers.clear()

    def clear_all(self) -> None:
        """Remove everything including candles and volume."""
        # ax.reset() (set up by finplot on each axis) removes all items from
        # the scene and calls vb.reset() — which clears standalones, rois, and
        # datasrc. This is more thorough than calling removeItem individually
        # and prevents indicator plots from one symbol bleeding onto another.
        self._price_panel.ax.reset()
        self._volume_panel.ax.reset()

        self._plots.clear()
        self._segments.clear()
        self._markers.clear()
        self._candle_plot = None
        self._volume_plot = None
        self._bar_index   = None

    def clamp_initial_zoom(self) -> None:
        """
        Ensure the initial x-zoom shows at most half of all available bars.

        Must be called AFTER all draw/update calls for a render pass (candles,
        volume, and every indicator), but BEFORE fplt.refresh(). If called
        earlier, finplot resets init_x0/init_x1 back to the full-data default
        each time fplt.plot() or update_data() adds a new series to the axis.

        Why this is needed:
            create_plot_widget is constructed once with init_zoom_periods=200.
            For short datasets (e.g. MIN39 ~60 bars), finplot computes
            init_x0 = max(60-200, 0) = 0, so the initial view covers ALL bars
            and the pan limits are simultaneously at their minimum — the user
            has nowhere to pan. Clamping to half the dataset guarantees
            at least 50 % of the data is off-screen and available for panning.

        For larger datasets (daily 600 bars, MIN5 4290 bars) min(200, N//2)
        equals 200 so this is a no-op.
        """
        vb = self._price_panel.ax.vb
        datasrc = getattr(vb, "datasrc", None)
        if datasrc is None:
            return
        total: int = datasrc.xlen
        if total <= 1:
            return
        init_steps: int = getattr(vb, "init_steps", 200)
        zoom_n = min(init_steps, max(1, total // 2))
        datasrc.update_init_x(zoom_n)

    def active_series_keys(self) -> list[str]:
        """Return the keys of all currently drawn indicator series."""
        return list(self._plots.keys()) + list(self._segments.keys())

    def active_marker_keys(self) -> list[str]:
        return list(self._markers.keys())

    def price_viewbox(self) -> object:
        return self._price_panel.ax.vb

    def _viewport_axes(self) -> list[object]:
        axes = [self._price_panel.ax]
        axes.extend(
            slot.panel.ax
            for slot in self._indicator_slots
            if slot.name is not None
        )
        return axes

    def update_marker(self, marker: MarkerRender) -> None:
        self.remove_marker(marker.key)
        x_value = _x_value_for_index(
            float(marker.x_index),
            self._bar_index,
            bool(self._price_panel.ax.vb.x_indexed),
        )
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

    def remove_marker(self, marker_key: str) -> None:
        if marker_key in self._markers:
            handle = self._markers.pop(marker_key)
            self._price_panel.ax.removeItem(handle)
        for item in list(self._price_panel.ax.items):
            if getattr(item, "_simplechart_marker_key", None) == marker_key:
                self._price_panel.ax.removeItem(item)

    def scrub_orphan_markers(self, active_marker_keys: set[str]) -> None:
        for marker_key in list(self._markers):
            if marker_key not in active_marker_keys:
                self.remove_marker(marker_key)
        for item in list(self._price_panel.ax.items):
            item_marker_key = getattr(item, "_simplechart_marker_key", None)
            if isinstance(item_marker_key, str):
                if item_marker_key not in active_marker_keys:
                    self._price_panel.ax.removeItem(item)
                continue
            if _text_item_plain_text(item) in {"⚓", "⚓️"}:
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


def _text_item_plain_text(item: object) -> str | None:
    text_item = getattr(item, "textItem", None)
    to_plain_text = getattr(text_item, "toPlainText", None)
    if callable(to_plain_text):
        return str(to_plain_text())
    return None
