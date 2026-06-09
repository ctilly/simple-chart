"""
app/controller.py

Central coordinator for SimpleChart.

The controller is the only place that holds references to all four layers
simultaneously (data, extensions, chart, app state). All cross-layer
workflows live here.

Workflows:
  load_symbol()       — user enters a symbol or changes timeframe
  chart action        — user right-clicks a bar to run extension actions
  toggle_extension()  — user clicks a legend label to show/hide
  configure_extension() — user right-clicks a legend label to edit params
  remove_extension()  — user removes an extension via legend context menu

Data fetch strategy:
  1. Maintain daily reference bars before intraday loads.
  2. Check the newest cached timestamp for the requested symbol + timeframe.
  3. Fetch only missing bars from the provider via the aggregator.
  4. Build an OHLCVSeries from the requested cached range.

Threading:
  Data fetches (network calls to yfinance or Alpaca) run in a background
  QThread to keep the UI responsive. The chart shows a loading state
  while the fetch is in progress. Results are delivered back to the main
  thread via Qt signals.

Initial workspace extension set:
  On first load the controller adds the extensions defined in
  INITIAL_EXTENSIONS below. The user can add or remove extensions during
  a session. In a future version this will be persisted to the config.
"""

import copy
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QMessageBox,
    QFrame,
    QVBoxLayout,
    QWidget,
)

from app.extension_config import ExtensionConfigDialog
from app.extension_runtime import ChartExtensionRenderPass, ChartExtensionRuntime
from app.extension_store import ChartExtensionStore
from app.header_bar import AppHeader
from app.state import ChartExtensionState, State
from app.symbol_bar import SymbolBar
from app.watchlist import WatchlistWidget
from chart.window import ChartWidget
from data.aggregator import Aggregator
from data.cache import Cache
from data.models import MarketSnapshot, OHLCVSeries, Timeframe
from data.provider import get_provider
from simplechart.api import (
    ChartEvent,
    ChoiceParam,
    DrawingSession,
    DrawingToolResult,
    ChartExtensionAddMode,
    AxisPriceLabelRender,
    ChartExtensionRender,
    HorizontalLineRender,
    HorizontalSegmentRender,
    LINE_STYLE_OPTIONS,
    PolylineRender,
    RENDER_CHART,
    SeriesRender,
    ToolIconSpec,
    VerticalLineRender,
    all_extensions,
)
from simplechart.plugins import load_plugins

load_plugins()


# ------------------------------------------------------------------
# Initial extension set loaded on every symbol
# ------------------------------------------------------------------
# Each entry is (extension_name, params). Adjust to taste.

INITIAL_EXTENSIONS: list[tuple[str, dict[str, Any]]] = [
    ("sma", {"days":  5, "color": "#FFA500", "line_width": 1.0, "line_style": ChoiceParam("solid", LINE_STYLE_OPTIONS)}),  # amber
    ("sma", {"days": 20, "color": "#00CED1", "line_width": 1.0, "line_style": ChoiceParam("solid", LINE_STYLE_OPTIONS)}),  # teal
    ("sma", {"days": 50, "color": "#1E90FF", "line_width": 1.0, "line_style": ChoiceParam("solid", LINE_STYLE_OPTIONS)}),  # blue
]

# How many calendar days of bars to load by default.
# 600 days ensures the 50-day SMA has ample warmup history visible from
# the left edge of the chart (~50 warmup bars + ~550 days of visible data).
_DEFAULT_LOOKBACK_DAYS = 600

# yfinance hard-limits intraday bar history by timeframe. Requesting
# dates outside these windows returns empty data (no exception raised).
#   1m: 7 days
#   5m / 15m / 30m / 39m (synthesized from 5m) / 65m (synthesized from 5m): 60 days
# Use 55 / 6 to stay comfortably inside the window.
_INTRADAY_SHORT_LOOKBACK = 6     # for 1m only
_INTRADAY_MEDIUM_LOOKBACK = 55   # for 5m/15m/30m/39m/65m
_SNAPSHOT_REFRESH_MS = 60_000
_DRAWING_PREVIEW_FRAME_MS = 16


def _lookback_days(tf: Timeframe) -> int:
    """Return the safe lookback window in calendar days for a timeframe."""
    if tf in (Timeframe.MIN1,):
        return _INTRADAY_SHORT_LOOKBACK
    if tf in (Timeframe.MIN5, Timeframe.MIN15, Timeframe.MIN30, Timeframe.MIN39, Timeframe.MIN65):
        return _INTRADAY_MEDIUM_LOOKBACK
    return _DEFAULT_LOOKBACK_DAYS


class _FetchWorker(QObject):
    """
    Runs a data fetch in a background thread.

    Emits finished(series) on success or error(message) on failure.
    The controller creates one worker per fetch, moves it to a QThread,
    and connects its signals before starting.
    """

    finished: pyqtSignal = pyqtSignal(object)   # emits OHLCVSeries
    error:    pyqtSignal = pyqtSignal(str)

    def __init__(
        self,
        aggregator: Aggregator,
        cache:      Cache,
        symbol:     str,
        timeframe:  Timeframe,
        lookback_days: int,
    ) -> None:
        super().__init__()
        self._aggregator    = aggregator
        self._cache         = cache
        self._symbol        = symbol
        self._timeframe     = timeframe
        self._lookback_days = lookback_days

    def run(self) -> None:
        """
        Fetch bars and return an OHLCVSeries.

        Intraday loads also refresh daily bars so day-based indicators have
        reference data without requiring the user to visit the daily chart.
        """
        try:
            now = datetime.now(tz=timezone.utc)
            series = _fetch_series_with_references(
                self._aggregator,
                self._cache,
                self._symbol,
                self._timeframe,
                self._lookback_days,
                now,
            )
            self.finished.emit(series)

        except Exception as exc:
            self.error.emit(str(exc))


class _SnapshotWorker(QObject):
    finished: pyqtSignal = pyqtSignal(object)   # emits dict[str, MarketSnapshot]
    error:    pyqtSignal = pyqtSignal(str)

    def __init__(self, aggregator: Aggregator, symbols: list[str]) -> None:
        super().__init__()
        self._aggregator = aggregator
        self._symbols = list(symbols)

    def run(self) -> None:
        try:
            self.finished.emit(self._aggregator.fetch_snapshots(self._symbols))
        except Exception as exc:
            self.error.emit(str(exc))


def _fetch_series_with_references(
    aggregator: Aggregator,
    cache: Cache,
    symbol: str,
    timeframe: Timeframe,
    lookback_days: int,
    now: datetime,
) -> OHLCVSeries:
    if timeframe.is_intraday:
        _fetch_and_cache_bars(
            aggregator,
            cache,
            symbol,
            Timeframe.DAILY,
            _DEFAULT_LOOKBACK_DAYS,
            now,
        )
    return _fetch_series(aggregator, cache, symbol, timeframe, lookback_days, now)


def _fetch_series(
    aggregator: Aggregator,
    cache: Cache,
    symbol: str,
    timeframe: Timeframe,
    lookback_days: int,
    now: datetime,
) -> OHLCVSeries:
    _fetch_and_cache_bars(aggregator, cache, symbol, timeframe, lookback_days, now)
    lookback_ms = int((now.timestamp() - lookback_days * 86_400) * 1000)
    bars = cache.get_bars(
        symbol,
        timeframe,
        lookback_ms,
        int(now.timestamp() * 1000),
    )
    return OHLCVSeries(
        symbol=symbol,
        timeframe=timeframe,
        bars=bars,
    )


def _fetch_and_cache_bars(
    aggregator: Aggregator,
    cache: Cache,
    symbol: str,
    timeframe: Timeframe,
    lookback_days: int,
    now: datetime,
) -> None:
    newest_ts = cache.newest_cached_timestamp(symbol, timeframe)
    if newest_ts is not None:
        start = datetime.fromtimestamp(newest_ts / 1000, tz=timezone.utc)
    else:
        start = datetime.fromtimestamp(
            now.timestamp() - lookback_days * 86_400,
            tz=timezone.utc,
        )

    new_bars = aggregator.fetch_bars(symbol, timeframe, start, now)
    if new_bars:
        cache.put_bars(symbol, timeframe, new_bars)


class MainWindow(QMainWindow):
    """
    The application's main window.

    Layout:
        ┌─────────────────────────────────┐
        │  SymbolBar (symbol + timeframe) │
        ├─────────────────────────────────┤
        │  ChartWidget (chart + legend)   │
        └─────────────────────────────────┘
    """

    def __init__(self, db_path: str, provider_name: str = "yfinance") -> None:
        super().__init__()
        self.setWindowTitle("Simple Chart")
        self.resize(1400, 800)

        # ------------------------------------------------------------------
        # Data layer
        # ------------------------------------------------------------------
        self._cache      = Cache(db_path)
        self._aggregator = Aggregator(get_provider(provider_name))

        # ------------------------------------------------------------------
        # App state
        # ------------------------------------------------------------------
        self._state = State()
        self._extension_store = ChartExtensionStore(self._state, self._cache)
        self._extension_runtime = ChartExtensionRuntime(
            self._state,
            self._cache,
            self._extension_store,
            _DEFAULT_LOOKBACK_DAYS,
        )
        self._active_drawing_tool: str | None = None
        self._drawing_session: DrawingSession | None = None
        self._preview_keys: set[str] = set()
        self._pending_drawing_preview_event: ChartEvent | None = None
        self._drawing_preview_timer = QTimer(self)
        self._drawing_preview_timer.setSingleShot(True)
        self._drawing_preview_timer.setInterval(_DRAWING_PREVIEW_FRAME_MS)
        self._drawing_preview_timer.timeout.connect(self._flush_drawing_preview)
        for name, params in INITIAL_EXTENSIONS:
            self._state.extensions.append(
                ChartExtensionState(name=name, params=dict(params))
            )

        # ------------------------------------------------------------------
        # UI
        # ------------------------------------------------------------------
        frame = QFrame()
        frame.setObjectName("appFrame")
        frame.setStyleSheet(
            "QFrame#appFrame {"
            " background: #ffffff;"
            " border: 5px solid #9d978d;"
            "}"
        )

        # Top-level layout: app header above the watchlist/chart workspace.
        root_layout = QVBoxLayout(frame)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self._app_header = AppHeader()
        root_layout.addWidget(self._app_header)

        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        root_layout.addLayout(main_layout)

        # Watchlist sidebar
        self._watchlist = WatchlistWidget(
            symbols=self._cache.get_watchlist(),
            on_add=self._on_watchlist_add,
            on_remove=self._on_watchlist_remove,
            on_reorder=self._on_watchlist_reorder,
            parent=frame,
        )
        self._watchlist.symbol_selected.connect(self._on_watchlist_symbol_selected)
        main_layout.addWidget(self._watchlist)

        # Chart area: symbol controls above the chart.
        chart_area = QWidget()
        chart_layout = QVBoxLayout(chart_area)
        chart_layout.setContentsMargins(0, 0, 0, 0)
        chart_layout.setSpacing(0)

        self._symbol_bar = SymbolBar()
        self._chart      = ChartWidget(
            on_toggle=self._on_extension_toggled,
            on_configure=self._on_extension_configure,
            on_remove=self._on_extension_remove,
            on_add=self._on_add_extension,
            on_drawing_tool=self._on_drawing_tool_selected,
            drawing_tools=self._drawing_tool_entries(),
        )

        chart_layout.addWidget(self._symbol_bar)
        chart_layout.addWidget(self._chart)
        main_layout.addWidget(chart_area)

        self.setCentralWidget(frame)

        # Wire chart interactions.
        self._chart.interactions.on_bar_clicked(self._on_bar_clicked)
        self._chart.interactions.on_bar_right_clicked(self._on_bar_right_clicked)
        self._chart.interactions.on_mouse_move(self._on_mouse_move)
        self._chart.interactions.on_drag_start(self._on_drag_start)
        self._chart.interactions.on_drag_move(self._on_drag_move)
        self._chart.interactions.on_drag_finish(self._on_drag_finish)
        self._chart.interactions.on_drag_cancel(self._on_drag_cancel)
        self._chart.on_cancel(self._on_cancel_shortcut)
        self._chart.on_commit(self._on_commit_shortcut)

        # Wire symbol bar signals.
        self._symbol_bar.symbol_changed.connect(self._on_symbol_changed)
        self._symbol_bar.timeframe_changed.connect(self._on_timeframe_changed)

        # Active fetch thread — kept as an attribute to prevent GC.
        self._fetch_thread: QThread | None = None
        self._fetch_worker: _FetchWorker | None = None

        self._snapshot_thread: QThread | None = None
        self._snapshot_worker: _SnapshotWorker | None = None
        self._snapshot_timer = QTimer(self)
        self._snapshot_timer.setInterval(_SNAPSHOT_REFRESH_MS)
        self._snapshot_timer.timeout.connect(self._refresh_watchlist_snapshots)

        # Most recently loaded series — used to convert bar index to timestamp
        # when the user clicks a bar (finplot's x-axis is indexed, not time-based).
        self._current_series: OHLCVSeries | None = None

        # The symbol for which self._state.extensions currently holds state.
        # Used to save/restore per-symbol extension state on symbol switch.
        self._loaded_symbol: str | None = None
        self._per_symbol_state: dict[str, list[ChartExtensionState]] = {}

        # Load the initial symbol on startup: first watchlist entry, or SPY.
        watchlist = self._cache.get_watchlist()
        initial_symbol = watchlist[0] if watchlist else "SPY"
        self._state.symbol = initial_symbol
        self._symbol_bar.set_symbol(initial_symbol)
        self._load()
        self._snapshot_timer.start()
        self._refresh_watchlist_snapshots()

    # ------------------------------------------------------------------
    # Symbol and timeframe loading
    # ------------------------------------------------------------------

    def _on_symbol_changed(self, symbol: str) -> None:
        self._state.symbol = symbol
        self._load()

    def _on_timeframe_changed(self, tf: Timeframe) -> None:
        self._state.timeframe = tf
        self._load()

    def _load(self) -> None:
        """
        Kick off a background fetch for the current symbol and timeframe.
        Clears the chart immediately so the user sees a clean slate while
        loading, rather than stale data from the previous symbol.
        """
        if self._state.symbol is None:
            return

        # Cancel any in-progress fetch.
        if self._fetch_thread and self._fetch_thread.isRunning():
            self._fetch_thread.quit()
            self._fetch_thread.wait()

        self._chart.clear_all()
        self._chart.legend.clear_all()

        worker = _FetchWorker(
            aggregator=self._aggregator,
            cache=self._cache,
            symbol=self._state.symbol,
            timeframe=self._state.timeframe,
            lookback_days=_lookback_days(self._state.timeframe),
        )
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_fetch_done)
        worker.error.connect(self._on_fetch_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)

        self._fetch_thread = thread
        self._fetch_worker = worker
        thread.start()

    def _on_fetch_done(self, series: OHLCVSeries) -> None:
        """Called on the main thread when the background fetch completes."""
        # Save extension state for the symbol we're leaving.
        if self._loaded_symbol is not None and self._loaded_symbol != series.symbol:
            registry = all_extensions()
            self._per_symbol_state[self._loaded_symbol] = [
                ChartExtensionState(
                    name=s.name,
                    params=copy.deepcopy(s.params),
                    visible=s.visible,
                    series_visibility=copy.deepcopy(s.series_visibility),
                )
                for s in self._state.extensions
                if registry[s.name]().preserve_ui_state_per_symbol()
            ]

        # Restore or initialize extension state for the arriving symbol.
        if self._loaded_symbol != series.symbol:
            if series.symbol in self._per_symbol_state:
                self._state.extensions = [
                    ChartExtensionState(
                        name=s.name,
                        params=copy.deepcopy(s.params),
                        visible=s.visible,
                        series_visibility=copy.deepcopy(s.series_visibility),
                    )
                    for s in self._per_symbol_state[series.symbol]
                ]
            else:
                self._state.extensions = [
                    ChartExtensionState(name=name, params=dict(params))
                    for name, params in INITIAL_EXTENSIONS
                ]

        self._loaded_symbol = series.symbol
        self._current_series = series
        self._extension_store.load_for_symbol(series.symbol)
        self._render(series)
        self._symbol_bar.set_symbol(series.symbol)
        self._watchlist.set_active_symbol(series.symbol)

    def _on_fetch_error(self, message: str) -> None:
        QMessageBox.warning(self, "Load Error", message)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self, series: OHLCVSeries) -> None:
        """
        Draw candles, volume, and all active extensions for the series.
        Called after a successful fetch, and after a timeframe switch.
        """
        if not series.bars:
            QMessageBox.warning(
                self,
                "No Data",
                f"No bars returned for {series.symbol} "
                f"({series.timeframe.value}).\n\n"
                "The provider may not have data for this symbol or timeframe.",
            )
            return

        pm = self._chart.plot_manager
        pm.draw_candles(series)
        pm.draw_volume(series)

        render_passes = self._extension_runtime.render_all(series)
        for render_pass in render_passes:
            self._draw_extension_render(render_pass)
        self._remove_stale_extension_renders(render_passes)

        pm.refresh()

    def _draw_extension_render(
        self,
        render_pass: ChartExtensionRenderPass,
    ) -> None:
        """
        Push one runtime render result to the PlotManager and legend.
        """
        # For panel extensions, claim a slot before drawing. If all three
        # slots are occupied, warn the user and skip this extension.
        if render_pass.render_target != RENDER_CHART:
            try:
                self._chart.ensure_extension_panel(render_pass.render_target)
            except RuntimeError as exc:
                QMessageBox.warning(self, "Panel Limit Reached", str(exc))
                return

        ind_state = render_pass.state
        render = render_pass.render
        self._draw_render_content(render, ind_state)
        self._draw_markers(render, ind_state, ind_state.series_keys)

    def _draw_drag_render(self, render_pass: ChartExtensionRenderPass) -> None:
        pm = self._chart.plot_manager
        price_vb: Any = pm.price_viewbox()
        price_vb.win._isMouseLeftDrag = True
        with pm.preserving_viewport():
            ind_state = render_pass.state
            render = render_pass.render
            # Drag-render optimization: a drag changes only position, never a
            # legend entry's color/label/visibility, and the entry already exists
            # from the committed render. Skipping legend updates avoids a
            # per-frame setStyleSheet repolish on the dragged item's label.
            # The finish/cancel reload re-runs the full (legend-updating) render.
            rendered_keys = self._draw_render_content(render, ind_state, update_legend=False)
            self._draw_markers(render, ind_state, rendered_keys)

    def _draw_render_content(
        self,
        render: ChartExtensionRender,
        ind_state: ChartExtensionState,
        update_legend: bool = True,
    ) -> set[str]:
        """
        Draw a render's series, segments, and vertical lines to the chart and
        legend. Returns the set of non-marker keys drawn. When update_legend is
        False the legend is left untouched (used by the drag-preview path).
        """
        rendered_keys: set[str] = set()
        for series_render in render.series:
            rendered_keys.add(series_render.key)
            self._draw_series(series_render, ind_state, update_legend)
        for segment in render.segments:
            rendered_keys.add(segment.key)
            self._draw_segment(segment, ind_state, update_legend)
        for vline in render.vertical_lines:
            rendered_keys.add(vline.key)
            self._draw_vertical_line(vline, ind_state)
        for hline in render.horizontal_lines:
            rendered_keys.add(hline.key)
            self._draw_horizontal_line(hline, ind_state)
        for polyline in render.polylines:
            rendered_keys.add(polyline.key)
            self._draw_polyline(polyline, ind_state, update_legend)
        for label in render.axis_price_labels:
            rendered_keys.add(label.key)
            self._draw_axis_price_label(label, ind_state)
        return rendered_keys

    def _draw_series(
        self,
        series_render: SeriesRender,
        ind_state: ChartExtensionState,
        update_legend: bool = True,
    ) -> None:
        pm = self._chart.plot_manager
        series_key = series_render.key
        visible = ind_state.series_visibility.get(series_key, ind_state.visible)
        # Reference lines (e.g. RSI overbought/oversold) are drawn as ordinary
        # render series but excluded from the legend.
        is_reference = series_render.reference
        if not is_reference:
            visible = visible and series_render.visible
        pm.update_extension(
            series_key,
            series_render.values,
            series_render.color,
            series_render.line_width,
            series_render.line_style,
            series_render.render_target,
        )
        pm.set_visible(series_key, visible)
        if is_reference or not update_legend:
            return
        self._chart.legend.add_extension(series_key, series_render.label, series_render.color)
        self._chart.legend.update_color(series_key, series_render.color)
        self._chart.legend.set_extension_visible(series_key, visible)

    def _draw_segment(
        self,
        segment: HorizontalSegmentRender,
        ind_state: ChartExtensionState,
        update_legend: bool = True,
    ) -> None:
        pm = self._chart.plot_manager
        visible = ind_state.series_visibility.get(segment.key, ind_state.visible)
        visible = visible and segment.visible
        segment.visible = visible
        pm.update_horizontal_segment(segment)
        if segment.reference or not update_legend:
            return
        self._chart.legend.add_extension(segment.key, segment.label, segment.color)
        self._chart.legend.update_color(segment.key, segment.color)
        self._chart.legend.set_extension_visible(segment.key, visible)

    def _draw_vertical_line(
        self,
        line: VerticalLineRender,
        ind_state: ChartExtensionState,
    ) -> None:
        visible = ind_state.series_visibility.get(line.key, ind_state.visible)
        line.visible = visible and line.visible
        self._chart.plot_manager.update_vertical_line(line)

    def _draw_horizontal_line(
        self,
        line: HorizontalLineRender,
        ind_state: ChartExtensionState,
    ) -> None:
        visible = ind_state.series_visibility.get(line.key, ind_state.visible)
        line.visible = visible and line.visible
        self._chart.plot_manager.update_horizontal_line(line)

    def _draw_polyline(
        self,
        polyline: PolylineRender,
        ind_state: ChartExtensionState,
        update_legend: bool = True,
    ) -> None:
        pm = self._chart.plot_manager
        visible = ind_state.series_visibility.get(polyline.key, ind_state.visible)
        polyline.visible = visible and polyline.visible
        pm.update_polyline(polyline)
        if polyline.reference or not update_legend:
            return
        self._chart.legend.add_extension(polyline.key, polyline.label, polyline.color)
        self._chart.legend.update_color(polyline.key, polyline.color)
        self._chart.legend.set_extension_visible(polyline.key, polyline.visible)

    def _draw_axis_price_label(
        self,
        label: AxisPriceLabelRender,
        ind_state: ChartExtensionState,
    ) -> None:
        visible = ind_state.series_visibility.get(label.key, ind_state.visible)
        label.visible = visible and label.visible
        self._chart.plot_manager.update_axis_price_label(label)

    def _draw_markers(
        self,
        render: ChartExtensionRender,
        ind_state: ChartExtensionState,
        cleanup_keys: Iterable[str],
    ) -> None:
        pm = self._chart.plot_manager
        marker_keys = {marker.key for marker in render.markers}
        for marker in render.markers:
            visible = ind_state.series_visibility.get(marker.key, ind_state.visible)
            marker.visible = marker.visible and visible
            pm.update_marker(marker)
        for series_key in cleanup_keys:
            if series_key not in marker_keys:
                pm.remove_marker(series_key)

    def _draw_preview_render(self, render_pass: ChartExtensionRenderPass) -> None:
        pm = self._chart.plot_manager
        new_keys = self._render_keys(render_pass.render)
        for stale_key in self._preview_keys - new_keys:
            pm.remove_extension(stale_key)
            pm.remove_marker(stale_key)

        with pm.preserving_viewport():
            for series_render in render_pass.render.series:
                pm.update_extension(
                    series_render.key,
                    series_render.values,
                    series_render.color,
                    series_render.line_width,
                    series_render.line_style,
                    series_render.render_target,
                )
                pm.set_visible(series_render.key, series_render.visible)
            for segment in render_pass.render.segments:
                pm.update_horizontal_segment(segment)
            for vline in render_pass.render.vertical_lines:
                pm.update_vertical_line(vline)
            for hline in render_pass.render.horizontal_lines:
                pm.update_horizontal_line(hline)
            for polyline in render_pass.render.polylines:
                pm.update_polyline(polyline)
            for label in render_pass.render.axis_price_labels:
                pm.update_axis_price_label(label)
            for marker in render_pass.render.markers:
                pm.update_marker(marker)
        self._preview_keys = new_keys

    def _clear_preview_render(self) -> None:
        if not self._preview_keys:
            return
        pm = self._chart.plot_manager
        with pm.preserving_viewport():
            for key in self._preview_keys:
                pm.remove_extension(key)
                pm.remove_marker(key)
        self._preview_keys.clear()
        pm.refresh(preserve_view=True)

    def _render_keys(self, render: ChartExtensionRender) -> set[str]:
        keys = {series.key for series in render.series}
        keys.update(segment.key for segment in render.segments)
        keys.update(line.key for line in render.vertical_lines)
        keys.update(line.key for line in render.horizontal_lines)
        keys.update(polyline.key for polyline in render.polylines)
        keys.update(label.key for label in render.axis_price_labels)
        keys.update(marker.key for marker in render.markers)
        return keys

    # ------------------------------------------------------------------
    # Chart interaction workflow
    # ------------------------------------------------------------------

    def _on_bar_clicked(self, x_pos: float, y_pos: float, px_x: float, px_y: float) -> None:
        if self._current_series is None or self._active_drawing_tool is None:
            return
        event = self._extension_runtime.chart_event(
            self._current_series, x_pos, y_pos,
            pixel_size_x=px_x, pixel_size_y=px_y,
        )
        if self._drawing_session is None:
            result = self._extension_runtime.start_drawing(
                self._active_drawing_tool,
                self._current_series,
                event,
            )
            self._handle_drawing_result(result)
            return
        result = self._extension_runtime.advance_drawing(
            self._current_series,
            self._drawing_session,
            event,
        )
        self._handle_drawing_result(result)

    def _on_mouse_move(self, x_pos: float, y_pos: float, px_x: float, px_y: float) -> None:
        if self._current_series is None or self._drawing_session is None:
            return
        self._pending_drawing_preview_event = self._extension_runtime.chart_event(
            self._current_series, x_pos, y_pos,
            pixel_size_x=px_x, pixel_size_y=px_y,
        )
        if not self._drawing_preview_timer.isActive():
            self._drawing_preview_timer.start()

    def _flush_drawing_preview(self) -> None:
        if self._current_series is None or self._drawing_session is None:
            self._pending_drawing_preview_event = None
            return
        event = self._pending_drawing_preview_event
        self._pending_drawing_preview_event = None
        if event is None:
            return
        render_pass = self._extension_runtime.preview_drawing(
            self._current_series,
            self._drawing_session,
            event,
        )
        if render_pass is not None:
            self._draw_preview_render(render_pass)

    def _handle_drawing_result(self, result: DrawingToolResult) -> None:
        if result.render is not None and self._current_series is not None:
            render_pass = ChartExtensionRenderPass(
                state=ChartExtensionState(
                    name=self._drawing_session.extension_name if self._drawing_session else "",
                    params=self._drawing_session.working_params if self._drawing_session else {},
                    series_keys=list(self._render_keys(result.render)),
                ),
                render=result.render,
                render_target=RENDER_CHART,
            )
            self._draw_preview_render(render_pass)
        if result.cancel or result.done:
            self._clear_pending_drawing_preview()
            self._drawing_session = None
            self._clear_preview_render()
            if result.deactivate_tool:
                self._set_active_drawing_tool(None)
            if result.mutation is not None or result.clear_transient:
                self._reload_extensions(draw_bars=False, preserve_view=True)
            return
        self._drawing_session = result.session

    def _on_commit_shortcut(self) -> None:
        if self._drawing_session is None or self._current_series is None:
            return
        result = self._extension_runtime.commit_drawing(
            self._current_series,
            self._drawing_session,
        )
        self._handle_drawing_result(result)

    def _on_cancel_shortcut(self) -> None:
        if self._drawing_session is None:
            self._set_active_drawing_tool(None)
            return
        mutation = self._extension_runtime.cancel_drawing(self._drawing_session)
        self._clear_pending_drawing_preview()
        self._drawing_session = None
        self._clear_preview_render()
        self._set_active_drawing_tool(None)
        if mutation is not None:
            self._reload_extensions(draw_bars=False, preserve_view=True)

    def _on_drag_start(self, x_pos: float, y_pos: float, px_x: float, px_y: float) -> bool:
        if self._current_series is None:
            return False
        event = self._extension_runtime.chart_event(
            self._current_series,
            x_pos,
            y_pos,
            pixel_size_x=px_x,
            pixel_size_y=px_y,
        )
        return bool(self._extension_runtime.begin_drag(self._current_series, event))

    def _on_drag_move(self, x_pos: float, y_pos: float, px_x: float, px_y: float) -> None:
        if self._current_series is None:
            return
        event = self._extension_runtime.chart_event(
            self._current_series, x_pos, y_pos,
            pixel_size_x=px_x, pixel_size_y=px_y,
        )
        render_pass = self._extension_runtime.drag_to(self._current_series, event)
        if render_pass is not None:
            self._draw_drag_render(render_pass)

    def _on_drag_finish(self, x_pos: float, y_pos: float, px_x: float, px_y: float) -> None:
        if self._current_series is None:
            return
        event = self._extension_runtime.chart_event(
            self._current_series, x_pos, y_pos,
            pixel_size_x=px_x, pixel_size_y=px_y,
        )
        render_pass = self._extension_runtime.drag_to(self._current_series, event)
        if render_pass is not None:
            self._draw_drag_render(render_pass)
        self._extension_runtime.finish_drag(self._current_series, event)
        self._reload_extensions(draw_bars=False, preserve_view=True)

    def _on_drag_cancel(self) -> None:
        self._extension_runtime.cancel_drag()
        self._reload_extensions(draw_bars=False, preserve_view=True)

    def _on_bar_right_clicked(self, x_pos: float, y_pos: float, px_x: float, px_y: float) -> None:
        """Right-click on a bar: show context menu."""
        if self._current_series is None:
            return
        event = self._extension_runtime.chart_event(
            self._current_series,
            x_pos,
            y_pos,
            button="right",
            pixel_size_x=px_x,
            pixel_size_y=px_y,
        )
        if self._drawing_session is None and self._show_drawing_context_menu(event):
            return
        actions = self._extension_runtime.context_actions(self._current_series, event)
        if not actions:
            return
        menu = QMenu(self)
        qactions = [
            (menu.addAction(action.label), action)
            for action in actions
        ]
        selected = menu.exec(QCursor.pos())
        for qaction, action in qactions:
            if selected == qaction:
                self._extension_runtime.apply_action(
                    self._current_series,
                    action,
                    event,
                )
                self._reload_extensions()
                return

    def _show_drawing_context_menu(self, event: ChartEvent) -> bool:
        if self._current_series is None:
            return False
        hit = self._extension_runtime.drawing_hit_test(self._current_series, event)
        if hit is None:
            return False

        menu = QMenu(self)
        configure_action = None
        if self._extension_runtime.config_request(hit.handle_key) is not None:
            configure_action = menu.addAction("Configure...")
        remove_action = menu.addAction("Remove")
        selected = menu.exec(QCursor.pos())

        if configure_action is not None and selected == configure_action:
            self._on_extension_configure(hit.handle_key)
            return True
        if selected == remove_action:
            self._on_extension_remove(hit.handle_key)
            return True
        return True

    def _reload_extensions(
        self,
        *,
        draw_bars: bool = True,
        preserve_view: bool = True,
    ) -> None:
        """
        Re-read bars from cache and re-render all extensions.
        Used after extension action/remove/config changes — does not re-fetch
        from the provider.
        """
        if self._state.symbol is None:
            return
        if not draw_bars and self._current_series is not None:
            series = self._current_series
        else:
            now = datetime.now(tz=timezone.utc)
            lookback_ms = int(
                (now.timestamp() - _lookback_days(self._state.timeframe) * 86_400) * 1000
            )
            bars = self._cache.get_bars(
                self._state.symbol,
                self._state.timeframe,
                lookback_ms,
                int(now.timestamp() * 1000),
            )
            if (
                not bars
                and self._current_series is not None
                and self._current_series.symbol == self._state.symbol
                and self._current_series.timeframe == self._state.timeframe
            ):
                series = self._current_series
                draw_bars = False
            else:
                series = OHLCVSeries(
                    symbol=self._state.symbol,
                    timeframe=self._state.timeframe,
                    bars=bars,
                )
                self._current_series = series

        # Keep candle/volume data and _bar_index in sync with the reloaded
        # bar slice. If the bar count has changed since the last full render
        # (e.g. the lookback boundary shifted by a second and dropped the
        # oldest bar), pd.Series(values, index=self._bar_index) will raise
        # a length-mismatch ValueError. Refreshing candles here keeps
        # _bar_index aligned, which also ensures extension color changes
        # (which call update_data) take effect without a timeframe reload.
        pm = self._chart.plot_manager
        if draw_bars:
            if preserve_view:
                with pm.preserving_viewport():
                    pm.draw_candles(series)
                    pm.draw_volume(series)
            else:
                pm.draw_candles(series)
                pm.draw_volume(series)

        render_passes = self._extension_runtime.render_all(series)

        if preserve_view:
            with pm.preserving_viewport():
                for render_pass in render_passes:
                    self._draw_extension_render(render_pass)
                self._remove_stale_extension_renders(render_passes)
        else:
            for render_pass in render_passes:
                self._draw_extension_render(render_pass)
            self._remove_stale_extension_renders(render_passes)
        pm.refresh(preserve_view=preserve_view)

    def _remove_stale_extension_renders(
        self,
        render_passes: list[ChartExtensionRenderPass],
    ) -> None:
        active_series_keys: set[str] = {
            series_render.key
            for render_pass in render_passes
            for series_render in render_pass.render.series
        }
        active_series_keys.update(
            segment.key
            for render_pass in render_passes
            for segment in render_pass.render.segments
        )
        active_series_keys.update(
            line.key
            for render_pass in render_passes
            for line in render_pass.render.vertical_lines
        )
        active_series_keys.update(
            line.key
            for render_pass in render_passes
            for line in render_pass.render.horizontal_lines
        )
        active_series_keys.update(
            polyline.key
            for render_pass in render_passes
            for polyline in render_pass.render.polylines
        )
        active_series_keys.update(
            label.key
            for render_pass in render_passes
            for label in render_pass.render.axis_price_labels
        )
        active_marker_keys: set[str] = {
            marker.key
            for render_pass in render_passes
            for marker in render_pass.render.markers
        }
        pm = self._chart.plot_manager
        for series_key in pm.active_series_keys():
            if series_key not in active_series_keys:
                pm.remove_extension(series_key)
                self._chart.legend.remove_extension(series_key)
        for marker_key in pm.active_marker_keys():
            if marker_key not in active_marker_keys:
                pm.remove_marker(marker_key)
        pm.scrub_orphan_markers(active_marker_keys)

    # ------------------------------------------------------------------
    # ChartExtension toggle and configuration
    # ------------------------------------------------------------------

    def _on_extension_toggled(self, series_key: str) -> None:
        """Show or hide all render items belonging to the extension."""
        result = self._extension_runtime.toggle_visibility(series_key)
        if result is None:
            return
        pm = self._chart.plot_manager
        keys, visible = result
        for key in keys:
            pm.set_visible(key, visible)
            self._chart.legend.set_extension_visible(key, visible)

    def _on_extension_configure(self, series_key: str) -> None:
        """Open the config dialog for the extension owning series_key."""
        request = self._extension_runtime.config_request(series_key)
        if request is None:
            return
        dialog = ExtensionConfigDialog(
            extension_label=request.label,
            params=request.params,
            parent=self,
        )
        if dialog.exec() == ExtensionConfigDialog.DialogCode.Accepted:
            self._extension_runtime.apply_config(
                series_key,
                dialog.result_params(),
            )
            self._reload_extensions(draw_bars=False)

    def _on_extension_remove(self, series_key: str) -> None:
        """
        Remove an extension-owned render item from the chart.
        """
        removal = self._extension_runtime.remove(series_key)
        if removal is None:
            return
        pm = self._chart.plot_manager
        with pm.preserving_viewport():
            for key in removal.series_keys:
                pm.remove_extension(key)
                self._chart.legend.remove_extension(key)
            if removal.release_panel:
                self._chart.release_extension_panel(removal.render_target)

        self._reload_extensions(draw_bars=False, preserve_view=True)

    # ------------------------------------------------------------------
    # Watchlist
    # ------------------------------------------------------------------

    def _on_watchlist_symbol_selected(self, symbol: str) -> None:
        """User clicked a symbol in the watchlist — load it."""
        self._state.symbol = symbol
        self._symbol_bar.set_symbol(symbol)
        self._load()

    def _on_watchlist_add(self, symbol: str) -> None:
        """Persist and display a new watchlist entry."""
        self._cache.add_to_watchlist(symbol)
        self._watchlist.add_symbol(symbol)
        self._refresh_watchlist_snapshots()

    def _on_watchlist_remove(self, symbol: str) -> None:
        """Remove a watchlist entry from DB and UI."""
        self._cache.remove_from_watchlist(symbol)
        self._watchlist.remove_symbol(symbol)

    def _on_watchlist_reorder(self, symbols: list[str]) -> None:
        """Persist reordered watchlist symbols."""
        self._cache.reorder_watchlist(symbols)

    def _refresh_watchlist_snapshots(self) -> None:
        if self._snapshot_thread is not None and self._snapshot_thread.isRunning():
            return
        symbols = self._watchlist.symbols()
        if not symbols:
            return

        worker = _SnapshotWorker(self._aggregator, symbols)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_watchlist_snapshots)
        worker.error.connect(self._on_watchlist_snapshot_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)

        self._snapshot_thread = thread
        self._snapshot_worker = worker
        thread.start()

    def _on_watchlist_snapshots(
        self,
        snapshots: dict[str, MarketSnapshot],
    ) -> None:
        current_symbols = set(self._watchlist.symbols())
        self._watchlist.set_percent_changes(
            {
                symbol: snapshot.change_percent
                for symbol, snapshot in snapshots.items()
                if symbol in current_symbols
            }
        )

    def _on_watchlist_snapshot_error(self, message: str) -> None:
        status_bar = self.statusBar()
        if status_bar is not None:
            status_bar.showMessage(f"Watchlist quotes unavailable: {message}", 5000)

    def _on_add_extension(self) -> None:
        """
        Show a menu of dialog-added extension types, open a config dialog
        for the selected type, then add the new extension to state and
        redraw.
        """
        extensions = {name: cls() for name, cls in sorted(all_extensions().items())}
        entries = {
            name: extension
            for name, extension in extensions.items()
            if extension.add_mode() == ChartExtensionAddMode.DIALOG
        }
        if not entries:
            return

        menu = QMenu(self)
        # Map action -> (name, instance) so we can look up the selection.
        actions = {
            menu.addAction(extension.label()): (name, extension)
            for name, extension in entries.items()
        }
        action = menu.exec(QCursor.pos())
        if action not in actions:
            return

        name, extension = actions[action]
        dialog = ExtensionConfigDialog(
            extension_label=extension.label(),
            params=extension.default_params(),
            parent=self,
        )
        if dialog.exec() == ExtensionConfigDialog.DialogCode.Accepted:
            self._state.extensions.append(
                ChartExtensionState(name=name, params=dialog.result_params())
            )
            self._reload_extensions()

    def _drawing_tool_entries(self) -> list[tuple[str, str, ToolIconSpec | None]]:
        tools = {name: cls() for name, cls in all_extensions().items()}
        toolbar = {
            name: tool
            for name, tool in tools.items()
            if tool.add_mode() == ChartExtensionAddMode.TOOLBAR
        }
        # Fixed toolbox order; any tool not listed falls in afterwards, sorted.
        order = [
            "horizontal_line",
            "vertical_line",
            "trend_line",
            "poly_line",
            "fib_retracement",
            "erase",
        ]
        ordered = [name for name in order if name in toolbar]
        ordered += sorted(name for name in toolbar if name not in order)
        return [(name, toolbar[name].label(), toolbar[name].toolbar_icon()) for name in ordered]

    def _on_drawing_tool_selected(self, extension_name: str) -> None:
        if self._drawing_session is not None:
            self._extension_runtime.cancel_drawing(self._drawing_session)
            self._clear_pending_drawing_preview()
            self._drawing_session = None
            self._clear_preview_render()
        self._set_active_drawing_tool(extension_name)

    def _set_active_drawing_tool(self, extension_name: str | None) -> None:
        self._active_drawing_tool = extension_name
        self._chart.set_active_drawing_tool(extension_name)

    def _clear_pending_drawing_preview(self) -> None:
        self._pending_drawing_preview_event = None
        if self._drawing_preview_timer.isActive():
            self._drawing_preview_timer.stop()

    def closeEvent(self, event: object) -> None:
        """Clean up on close."""
        if self._snapshot_timer.isActive():
            self._snapshot_timer.stop()
        if self._snapshot_thread and self._snapshot_thread.isRunning():
            self._snapshot_thread.quit()
            self._snapshot_thread.wait()
        if self._fetch_thread and self._fetch_thread.isRunning():
            self._fetch_thread.quit()
            self._fetch_thread.wait()
        self._cache.close()
        super().closeEvent(event)  # type: ignore[arg-type]
