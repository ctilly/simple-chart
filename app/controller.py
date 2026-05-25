"""
app/controller.py

Central coordinator for SimpleChart.

The controller is the only place that holds references to all four layers
simultaneously (data, indicators, chart, app state). All cross-layer
workflows live here.

Workflows:
  load_symbol()       — user enters a symbol or changes timeframe
  chart action        — user right-clicks a bar to run indicator actions
  toggle_indicator()  — user clicks a legend label to show/hide
  configure_indicator() — user right-clicks a legend label to edit params
  remove_indicator()  — user removes an indicator via legend context menu

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

Initial workspace indicator set:
  On first load the controller adds the indicators defined in
  INITIAL_INDICATORS below. The user can add or remove indicators during
  a session. In a future version this will be persisted to the config.
"""

import copy
from datetime import datetime, timedelta, timezone
from typing import Any

from PyQt6.QtCore import QObject, QThread, pyqtSignal
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

from app.indicator_config import IndicatorConfigDialog
from app.indicator_runtime import ChartExtensionRenderPass, ChartExtensionRuntime
from app.indicator_store import ChartExtensionStore
from app.state import ChartExtensionState, State
from app.symbol_bar import SymbolBar
from app.watchlist import WatchlistWidget
from chart.window import ChartWidget
from data.aggregator import Aggregator
from data.cache import Cache
from data.models import OHLCVSeries, Timeframe
from data.provider import get_provider
from simplechart.api import (
    ChartEvent,
    ChoiceParam,
    DrawingSession,
    DrawingToolResult,
    IndicatorAddMode,
    IndicatorRender,
    LINE_STYLE_OPTIONS,
    RENDER_CHART,
    all_indicators,
)
from simplechart.plugins import load_plugins

load_plugins()


# ------------------------------------------------------------------
# Initial indicator set loaded on every symbol
# ------------------------------------------------------------------
# Each entry is (indicator_name, params). Adjust to taste.

INITIAL_INDICATORS: list[tuple[str, dict[str, Any]]] = [
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
        loaded_range_start=datetime.fromtimestamp(
            lookback_ms / 1000, tz=timezone.utc
        ),
        loaded_range_end=now,
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
        self._indicator_store = ChartExtensionStore(self._state, self._cache)
        self._indicator_runtime = ChartExtensionRuntime(
            self._state,
            self._cache,
            self._indicator_store,
            _DEFAULT_LOOKBACK_DAYS,
        )
        self._active_drawing_tool: str | None = None
        self._drawing_session: DrawingSession | None = None
        self._preview_keys: set[str] = set()
        for name, params in INITIAL_INDICATORS:
            self._state.indicators.append(
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

        # Top-level layout: watchlist sidebar on the left, chart area on the right.
        main_layout = QHBoxLayout(frame)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Watchlist sidebar
        self._watchlist = WatchlistWidget(
            symbols=self._cache.get_watchlist(),
            on_add=self._on_watchlist_add,
            on_remove=self._on_watchlist_remove,
            parent=frame,
        )
        self._watchlist.symbol_selected.connect(self._on_watchlist_symbol_selected)
        main_layout.addWidget(self._watchlist)

        # Chart area: symbol bar on top, chart below
        chart_area = QWidget()
        chart_layout = QVBoxLayout(chart_area)
        chart_layout.setContentsMargins(0, 0, 0, 0)
        chart_layout.setSpacing(0)

        self._symbol_bar = SymbolBar()
        self._chart      = ChartWidget()

        chart_layout.addWidget(self._symbol_bar)
        chart_layout.addWidget(self._chart)
        main_layout.addWidget(chart_area)

        self.setCentralWidget(frame)

        # Wire legend callbacks now that the controller exists.
        self._chart.wire_legend(
            on_toggle=self._on_indicator_toggled,
            on_configure=self._on_indicator_configure,
            on_remove=self._on_indicator_remove,
            on_add=self._on_add_indicator,
            on_drawing_tool=self._on_drawing_tool_selected,
            drawing_tools=self._drawing_tool_entries(),
        )

        # Wire chart interactions.
        self._chart.interactions.on_bar_clicked(self._on_bar_clicked)
        self._chart.interactions.on_bar_right_clicked(self._on_bar_right_clicked)
        self._chart.interactions.on_mouse_move(self._on_mouse_move)
        self._chart.interactions.on_drag_start(self._on_drag_start)
        self._chart.interactions.on_drag_move(self._on_drag_move)
        self._chart.interactions.on_drag_finish(self._on_drag_finish)
        self._chart.interactions.on_drag_cancel(self._on_drag_cancel)
        self._chart.on_cancel(self._on_cancel_shortcut)

        # Wire symbol bar signals.
        self._symbol_bar.symbol_changed.connect(self._on_symbol_changed)
        self._symbol_bar.timeframe_changed.connect(self._on_timeframe_changed)

        # Active fetch thread — kept as an attribute to prevent GC.
        self._fetch_thread: QThread | None = None
        self._fetch_worker: _FetchWorker | None = None

        # Most recently loaded series — used to convert bar index to timestamp
        # when the user clicks a bar (finplot's x-axis is indexed, not time-based).
        self._current_series: OHLCVSeries | None = None

        # The symbol for which self._state.indicators currently holds state.
        # Used to save/restore per-symbol indicator state on symbol switch.
        self._loaded_symbol: str | None = None
        self._per_symbol_state: dict[str, list[ChartExtensionState]] = {}

        # Load the initial symbol on startup: first watchlist entry, or SPY.
        watchlist = self._cache.get_watchlist()
        initial_symbol = watchlist[0] if watchlist else "SPY"
        self._state.symbol = initial_symbol
        self._symbol_bar.set_symbol(initial_symbol)
        self._load()

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
        # Save indicator state for the symbol we're leaving.
        if self._loaded_symbol is not None and self._loaded_symbol != series.symbol:
            self._per_symbol_state[self._loaded_symbol] = [
                ChartExtensionState(
                    name=s.name,
                    params=copy.deepcopy(s.params),
                    visible=s.visible,
                    series_visibility=copy.deepcopy(s.series_visibility),
                )
                for s in self._state.indicators
                if all_indicators()[s.name]().preserve_ui_state_per_symbol()
            ]

        # Restore or initialize indicator state for the arriving symbol.
        if self._loaded_symbol != series.symbol:
            if series.symbol in self._per_symbol_state:
                self._state.indicators = [
                    ChartExtensionState(
                        name=s.name,
                        params=copy.deepcopy(s.params),
                        visible=s.visible,
                        series_visibility=copy.deepcopy(s.series_visibility),
                    )
                    for s in self._per_symbol_state[series.symbol]
                ]
            else:
                self._state.indicators = [
                    ChartExtensionState(name=name, params=dict(params))
                    for name, params in INITIAL_INDICATORS
                ]

        self._loaded_symbol = series.symbol
        self._current_series = series
        self._indicator_store.load_for_symbol(series.symbol)
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
        Draw candles, volume, and all active indicators for the series.
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

        render_passes = self._indicator_runtime.render_all(series)
        for render_pass in render_passes:
            self._draw_indicator_render(render_pass)
        self._remove_stale_indicator_renders(render_passes)

        pm.refresh()

    def _draw_indicator_render(
        self,
        render_pass: ChartExtensionRenderPass,
    ) -> None:
        """
        Push one runtime render result to the PlotManager and legend.
        """
        # For panel indicators, claim a slot before drawing. If all three
        # slots are occupied, warn the user and skip this indicator.
        if render_pass.render_target != RENDER_CHART:
            try:
                self._chart.ensure_indicator_panel(render_pass.render_target)
            except RuntimeError as exc:
                QMessageBox.warning(self, "Panel Limit Reached", str(exc))
                return

        ind_state = render_pass.state
        render = render_pass.render
        pm = self._chart.plot_manager

        for series_render in render.series:
            series_key = series_render.key
            # Reference lines (e.g. RSI overbought/oversold) are drawn as
            # ordinary render series but excluded from the legend.
            if "_ref_" in series_key:
                visible = ind_state.series_visibility.get(series_key, ind_state.visible)
                pm.update_indicator(
                    series_key,
                    series_render.values,
                    series_render.color,
                    series_render.line_width,
                    series_render.line_style,
                    series_render.render_target,
                )
                pm.set_visible(series_key, visible)
                continue

            visible = ind_state.series_visibility.get(series_key, ind_state.visible)
            visible = visible and series_render.visible
            pm.update_indicator(
                series_key,
                series_render.values,
                series_render.color,
                series_render.line_width,
                series_render.line_style,
                series_render.render_target,
            )
            pm.set_visible(series_key, visible)

            self._chart.legend.add_indicator(series_key, series_render.label, series_render.color)
            self._chart.legend.update_color(series_key, series_render.color)
            self._chart.legend.set_indicator_visible(series_key, visible)

        for segment in render.segments:
            visible = ind_state.series_visibility.get(segment.key, ind_state.visible)
            visible = visible and segment.visible
            segment.visible = visible
            pm.update_horizontal_segment(segment)
            if "_ref_" in segment.key:
                continue
            self._chart.legend.add_indicator(segment.key, segment.label, segment.color)
            self._chart.legend.update_color(segment.key, segment.color)
            self._chart.legend.set_indicator_visible(segment.key, visible)

        for line in render.vertical_lines:
            visible = ind_state.series_visibility.get(line.key, ind_state.visible)
            line.visible = visible and line.visible
            pm.update_vertical_line(line)

        marker_keys = {marker.key for marker in render.markers}
        for marker in render.markers:
            visible = ind_state.series_visibility.get(marker.key, ind_state.visible)
            marker.visible = marker.visible and visible
            pm.update_marker(marker)
        for series_key in ind_state.series_keys:
            if series_key not in marker_keys:
                pm.remove_marker(series_key)

    def _draw_drag_render(self, render_pass: ChartExtensionRenderPass) -> None:
        pm = self._chart.plot_manager
        price_vb: Any = pm.price_viewbox()
        price_vb.win._isMouseLeftDrag = True
        with pm.preserving_viewport():
            ind_state = render_pass.state
            rendered_keys: set[str] = set()
            for series_render in render_pass.render.series:
                rendered_keys.add(series_render.key)
                visible = ind_state.series_visibility.get(series_render.key, ind_state.visible)
                visible = visible and series_render.visible
                pm.update_indicator(
                    series_render.key,
                    series_render.values,
                    series_render.color,
                    series_render.line_width,
                    series_render.line_style,
                    series_render.render_target,
                )
                pm.set_visible(series_render.key, visible)
                self._chart.legend.add_indicator(
                    series_render.key,
                    series_render.label,
                    series_render.color,
                )
                self._chart.legend.update_color(series_render.key, series_render.color)
                self._chart.legend.set_indicator_visible(series_render.key, visible)

            for segment in render_pass.render.segments:
                rendered_keys.add(segment.key)
                visible = ind_state.series_visibility.get(segment.key, ind_state.visible)
                visible = visible and segment.visible
                segment.visible = visible
                pm.update_horizontal_segment(segment)
                if "_ref_" in segment.key:
                    continue
                self._chart.legend.add_indicator(segment.key, segment.label, segment.color)
                self._chart.legend.update_color(segment.key, segment.color)
                self._chart.legend.set_indicator_visible(segment.key, visible)

            for line in render_pass.render.vertical_lines:
                rendered_keys.add(line.key)
                visible = ind_state.series_visibility.get(line.key, ind_state.visible)
                line.visible = visible and line.visible
                pm.update_vertical_line(line)

            marker_keys = {marker.key for marker in render_pass.render.markers}
            for marker in render_pass.render.markers:
                visible = ind_state.series_visibility.get(marker.key, ind_state.visible)
                marker.visible = marker.visible and visible
                pm.update_marker(marker)
            for series_key in rendered_keys:
                if series_key not in marker_keys:
                    pm.remove_marker(series_key)

    def _draw_preview_render(self, render_pass: ChartExtensionRenderPass) -> None:
        pm = self._chart.plot_manager
        new_keys = self._render_keys(render_pass.render)
        for stale_key in self._preview_keys - new_keys:
            pm.remove_indicator(stale_key)
            pm.remove_marker(stale_key)

        with pm.preserving_viewport():
            for series_render in render_pass.render.series:
                pm.update_indicator(
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
            for line in render_pass.render.vertical_lines:
                pm.update_vertical_line(line)
            for marker in render_pass.render.markers:
                pm.update_marker(marker)
        self._preview_keys = new_keys
        pm.refresh(preserve_view=True)

    def _clear_preview_render(self) -> None:
        if not self._preview_keys:
            return
        pm = self._chart.plot_manager
        with pm.preserving_viewport():
            for key in self._preview_keys:
                pm.remove_indicator(key)
                pm.remove_marker(key)
        self._preview_keys.clear()
        pm.refresh(preserve_view=True)

    def _render_keys(self, render: IndicatorRender) -> set[str]:
        keys = {series.key for series in render.series}
        keys.update(segment.key for segment in render.segments)
        keys.update(line.key for line in render.vertical_lines)
        keys.update(marker.key for marker in render.markers)
        return keys

    # ------------------------------------------------------------------
    # Chart interaction workflow
    # ------------------------------------------------------------------

    def _on_bar_clicked(self, x_pos: float, y_pos: float) -> None:
        if self._current_series is None or self._active_drawing_tool is None:
            return
        event = self._indicator_runtime.chart_event(self._current_series, x_pos, y_pos)
        if self._drawing_session is None:
            result = self._indicator_runtime.start_drawing(
                self._active_drawing_tool,
                self._current_series,
                event,
            )
            self._handle_drawing_result(result)
            return
        result = self._indicator_runtime.advance_drawing(
            self._current_series,
            self._drawing_session,
            event,
        )
        self._handle_drawing_result(result)

    def _on_mouse_move(self, x_pos: float, y_pos: float) -> None:
        if self._current_series is None or self._drawing_session is None:
            return
        event = self._indicator_runtime.chart_event(self._current_series, x_pos, y_pos)
        render_pass = self._indicator_runtime.preview_drawing(
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
                    name=self._drawing_session.indicator_name if self._drawing_session else "",
                    params=self._drawing_session.working_params if self._drawing_session else {},
                    series_keys=list(self._render_keys(result.render)),
                ),
                render=result.render,
                render_target=RENDER_CHART,
            )
            self._draw_preview_render(render_pass)
        if result.cancel or result.done:
            self._drawing_session = None
            self._clear_preview_render()
            if result.deactivate_tool:
                self._active_drawing_tool = None
            if result.mutation is not None:
                self._reload_indicators(draw_bars=False, preserve_view=True)
            return
        self._drawing_session = result.session

    def _on_cancel_shortcut(self) -> None:
        if self._drawing_session is None:
            return
        mutation = self._indicator_runtime.cancel_drawing(self._drawing_session)
        self._drawing_session = None
        self._clear_preview_render()
        if mutation is not None:
            self._reload_indicators(draw_bars=False, preserve_view=True)

    def _on_drag_start(self, x_pos: float, y_pos: float) -> bool:
        if self._current_series is None:
            return False
        event = self._indicator_runtime.chart_event(
            self._current_series,
            x_pos,
            y_pos,
        )
        return bool(self._indicator_runtime.begin_drag(self._current_series, event))

    def _on_drag_move(self, x_pos: float, y_pos: float) -> None:
        if self._current_series is None:
            return
        event = self._indicator_runtime.chart_event(self._current_series, x_pos, y_pos)
        render_pass = self._indicator_runtime.drag_to(self._current_series, event)
        if render_pass is not None:
            self._draw_drag_render(render_pass)

    def _on_drag_finish(self, x_pos: float, y_pos: float) -> None:
        if self._current_series is None:
            return
        event = self._indicator_runtime.chart_event(self._current_series, x_pos, y_pos)
        render_pass = self._indicator_runtime.drag_to(self._current_series, event)
        if render_pass is not None:
            self._draw_drag_render(render_pass)
        self._indicator_runtime.finish_drag(self._current_series, event)
        self._reload_indicators(draw_bars=False, preserve_view=True)

    def _on_drag_cancel(self) -> None:
        self._indicator_runtime.cancel_drag()
        self._reload_indicators(draw_bars=False, preserve_view=True)

    def _on_bar_right_clicked(self, x_pos: float, y_pos: float) -> None:
        """Right-click on a bar: show context menu."""
        if self._current_series is None:
            return
        event = self._indicator_runtime.chart_event(
            self._current_series,
            x_pos,
            y_pos,
            button="right",
        )
        if self._drawing_session is None and self._show_drawing_context_menu(event):
            return
        actions = self._indicator_runtime.context_actions(self._current_series, event)
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
                self._indicator_runtime.apply_action(
                    self._current_series,
                    action,
                    event,
                )
                self._reload_indicators()
                return

    def _show_drawing_context_menu(self, event: ChartEvent) -> bool:
        if self._current_series is None:
            return False
        hit = self._indicator_runtime.drawing_hit_test(self._current_series, event)
        if hit is None:
            return False

        menu = QMenu(self)
        configure_action = None
        if self._indicator_runtime.config_request(hit.handle_key) is not None:
            configure_action = menu.addAction("Configure...")
        remove_action = menu.addAction("Remove")
        selected = menu.exec(QCursor.pos())

        if configure_action is not None and selected == configure_action:
            self._on_indicator_configure(hit.handle_key)
            return True
        if selected == remove_action:
            self._on_indicator_remove(hit.handle_key)
            return True
        return True

    def _reload_indicators(
        self,
        *,
        draw_bars: bool = True,
        preserve_view: bool = True,
    ) -> None:
        """
        Re-read bars from cache and recompute all indicators.
        Used after indicator action/remove/config changes — does not re-fetch
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
        # _bar_index aligned, which also ensures indicator color changes
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

        render_passes = self._indicator_runtime.render_all(series)

        if preserve_view:
            with pm.preserving_viewport():
                for render_pass in render_passes:
                    self._draw_indicator_render(render_pass)
                self._remove_stale_indicator_renders(render_passes)
        else:
            for render_pass in render_passes:
                self._draw_indicator_render(render_pass)
            self._remove_stale_indicator_renders(render_passes)
        pm.refresh(preserve_view=preserve_view)

    def _remove_stale_indicator_renders(
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
        active_marker_keys: set[str] = {
            marker.key
            for render_pass in render_passes
            for marker in render_pass.render.markers
        }
        pm = self._chart.plot_manager
        for series_key in pm.active_series_keys():
            if series_key not in active_series_keys:
                pm.remove_indicator(series_key)
                self._chart.legend.remove_indicator(series_key)
        for marker_key in pm.active_marker_keys():
            if marker_key not in active_marker_keys:
                pm.remove_marker(marker_key)
        pm.scrub_orphan_markers(active_marker_keys)

    # ------------------------------------------------------------------
    # Indicator toggle and configuration
    # ------------------------------------------------------------------

    def _on_indicator_toggled(self, series_key: str) -> None:
        """Show or hide all render items belonging to the indicator."""
        result = self._indicator_runtime.toggle_visibility(series_key)
        if result is None:
            return
        pm = self._chart.plot_manager
        keys, visible = result
        for key in keys:
            pm.set_visible(key, visible)
            self._chart.legend.set_indicator_visible(key, visible)

    def _on_indicator_configure(self, series_key: str) -> None:
        """Open the config dialog for the indicator owning series_key."""
        request = self._indicator_runtime.config_request(series_key)
        if request is None:
            return
        dialog = IndicatorConfigDialog(
            indicator_label=request.label,
            params=request.params,
            parent=self,
        )
        if dialog.exec() == IndicatorConfigDialog.DialogCode.Accepted:
            self._indicator_runtime.apply_config(series_key, dialog.result_params())
            self._reload_indicators(draw_bars=False)

    def _on_indicator_remove(self, series_key: str) -> None:
        """
        Remove an indicator-owned render item from the chart.
        """
        removal = self._indicator_runtime.remove(series_key)
        if removal is None:
            return
        pm = self._chart.plot_manager
        with pm.preserving_viewport():
            for key in removal.series_keys:
                pm.remove_indicator(key)
                self._chart.legend.remove_indicator(key)
            if removal.release_panel:
                self._chart.release_indicator_panel(removal.render_target)

        self._reload_indicators(draw_bars=False, preserve_view=True)

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

    def _on_watchlist_remove(self, symbol: str) -> None:
        """Remove a watchlist entry from DB and UI."""
        self._cache.remove_from_watchlist(symbol)
        self._watchlist.remove_symbol(symbol)

    def _on_add_indicator(self) -> None:
        """
        Show a menu of available indicator types, then open a config dialog
        for the selected type. On accept, add the new indicator to state and
        redraw.
        """
        registry = all_indicators()
        entries = {
            name: cls
            for name, cls in sorted(registry.items())
            if cls().add_mode() == IndicatorAddMode.DIALOG
        }
        if not entries:
            return

        menu = QMenu(self)
        # Map action -> (name, class) so we can look up the selection.
        actions = {
            menu.addAction(cls().label()): (name, cls)
            for name, cls in entries.items()
        }
        action = menu.exec(QCursor.pos())
        if action not in actions:
            return

        name, cls = actions[action]
        indicator = cls()
        dialog = IndicatorConfigDialog(
            indicator_label=indicator.label(),
            params=indicator.default_params(),
            parent=self,
        )
        if dialog.exec() == IndicatorConfigDialog.DialogCode.Accepted:
            self._state.indicators.append(
                ChartExtensionState(name=name, params=dialog.result_params())
            )
            self._reload_indicators()

    def _drawing_tool_entries(self) -> list[tuple[str, str]]:
        registry = all_indicators()
        return [
            (name, cls().label())
            for name, cls in sorted(registry.items())
            if cls().add_mode() == IndicatorAddMode.TOOLBAR
        ]

    def _on_drawing_tool_selected(self, indicator_name: str) -> None:
        if self._drawing_session is not None:
            self._indicator_runtime.cancel_drawing(self._drawing_session)
            self._drawing_session = None
            self._clear_preview_render()
        self._active_drawing_tool = indicator_name

    def closeEvent(self, event: object) -> None:
        """Clean up on close."""
        if self._fetch_thread and self._fetch_thread.isRunning():
            self._fetch_thread.quit()
            self._fetch_thread.wait()
        self._cache.close()
        super().closeEvent(event)  # type: ignore[arg-type]
