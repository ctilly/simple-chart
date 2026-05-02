"""
chart/plot_manager.py

Owns all active finplot plot objects and is the only place in the app
where finplot draw calls are made.

Responsibilities:
  - Draw and update candlesticks and volume bars when a new symbol loads
  - Create, update, and remove indicator plot lines
  - Show/hide individual plot lines (for the indicator toggle)
  - Clear everything when switching symbols

Why a dedicated manager?
    finplot returns a plot handle when you draw something. To update that
    plot later (e.g. when new bars arrive or the user changes a parameter)
    you call update_data() on the handle rather than drawing a new line on
    top. Without a central registry of handles, you lose track of what's
    on the chart and end up with duplicate lines.

Note on pandas:
    finplot requires data formatted as a pandas DataFrame for candlesticks
    and volume. Pandas is already a transitive dependency of finplot, so
    this is not an additional dependency — we just use it here in the chart
    layer for the specific purpose of preparing data for finplot. It does
    not appear anywhere else in the codebase.
"""

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PyQt6.QtCore import Qt

from chart.panel import IndicatorPanelSlot, Panel
from chart.styles import (
    BACKGROUND,
    CANDLE_DOWN,
    CANDLE_UP,
    LINE_WIDTH_INDICATOR,
    VOLUME_DOWN,
    VOLUME_UP,
)
from chart.viewport import apply_interaction_modes, unlock_x_pan
from data.models import OHLCVSeries

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

        # Maps series_key -> finplot plot handle.
        # Keys follow the same naming convention as compute() output:
        # "sma_50", "avwap_1704067200000", etc.
        self._plots: dict[str, object] = {}

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
        the order: open, close, high, low, indexed by epoch-second floats.
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
            self._candle_plot.colors.update({  # type: ignore[attr-defined]
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
        open, close, volume, indexed by epoch-second floats.
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
            self._volume_plot.colors.update({  # type: ignore[attr-defined]
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
            handle = self._plots[series_key]
            # Update pen BEFORE update_data. Internally, update_data calls
            # finplot's _start_visual_update → pg setData → updateItems with
            # styleUpdate=True, which reads opts['pen']. Setting the pen first
            # ensures the new color is in opts['pen'] when that re-render runs.
            handle.setPen(  # type: ignore[attr-defined]
                pg.mkPen(color=color, width=width, style=pen_style)
            )
            handle.opts["handed_color"] = color  # type: ignore[attr-defined]
            handle.update_data(data)  # type: ignore[attr-defined]
        else:
            target_ax = self._resolve_ax(render_target)
            handle = fplt.plot(
                data,
                ax=target_ax,
                color=color,
                width=width,
            )
            handle.setPen(  # type: ignore[attr-defined]
                pg.mkPen(color=color, width=width, style=pen_style)
            )
            self._plots[series_key] = handle

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

        Called when the user deletes an AVWAP anchor or removes an
        indicator entirely.
        """
        if series_key in self._plots:
            handle = self._plots.pop(series_key)
            handle.ax.removeItem(handle)  # type: ignore[attr-defined]

    def set_visible(self, series_key: str, visible: bool) -> None:
        """
        Show or hide an indicator plot line without removing it.

        Used by the legend toggle — hiding keeps the plot handle alive
        so it can be shown again without recomputing.
        """
        if series_key in self._plots:
            self._plots[series_key].setVisible(visible)  # type: ignore[attr-defined]

    def clear_indicators(self) -> None:
        """
        Remove all indicator lines. Called when switching symbols.

        Candles and volume are replaced via draw_candles/draw_volume,
        so they are not cleared here.
        """
        for handle in self._plots.values():
            handle.ax.removeItem(handle)  # type: ignore[attr-defined]
        self._plots.clear()

    def clear_all(self) -> None:
        """Remove everything including candles and volume."""
        # ax.reset() (set up by finplot on each axis) removes all items from
        # the scene and calls vb.reset() — which clears standalones, rois, and
        # datasrc. This is more thorough than calling removeItem individually
        # and prevents indicator plots from one symbol bleeding onto another.
        self._price_panel.ax.reset()   # type: ignore[attr-defined]
        self._volume_panel.ax.reset()  # type: ignore[attr-defined]

        self._plots.clear()
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
        vb = self._price_panel.ax.vb  # type: ignore[attr-defined]
        datasrc = getattr(vb, "datasrc", None)
        if datasrc is None:
            return
        total: int = datasrc.xlen  # type: ignore[attr-defined]
        if total <= 1:
            return
        init_steps: int = getattr(vb, "init_steps", 200)
        zoom_n = min(init_steps, max(1, total // 2))
        datasrc.update_init_x(zoom_n)  # type: ignore[attr-defined]

    def active_series_keys(self) -> list[str]:
        """Return the keys of all currently drawn indicator series."""
        return list(self._plots.keys())


# ------------------------------------------------------------------
# Private helpers — data formatting for finplot
# ------------------------------------------------------------------

def _bar_timestamps_as_epoch(series: OHLCVSeries) -> pd.Index:
    """
    Convert bar timestamps to a pandas DatetimeIndex (UTC).
    finplot uses this as the x-axis.
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
    idx = _bar_timestamps_as_epoch(series)
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
    idx = _bar_timestamps_as_epoch(series)
    return pd.DataFrame(
        {
            "open":   [b.open   for b in series.bars],
            "close":  [b.close  for b in series.bars],
            "volume": [b.volume for b in series.bars],
        },
        index=idx,
    )
