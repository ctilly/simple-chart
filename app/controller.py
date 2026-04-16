"""
app/controller.py

Central coordinator for SimpleChart.

The controller is the only place that holds references to all four layers
simultaneously (data, indicators, chart, app state). All cross-layer
workflows live here.

Workflows:
  load_symbol()       — user enters a symbol or changes timeframe
  anchor_avwap()      — user clicks a bar to place an AVWAP anchor
  toggle_indicator()  — user clicks a legend label to show/hide
  configure_indicator() — user right-clicks a legend label to edit params
  remove_anchor()     — user removes an AVWAP anchor via context menu

Data fetch strategy:
  1. Check the cache for the requested symbol + timeframe.
  2. If the cache has enough bars, use them directly.
  3. If not (or the cache is stale), fetch from the provider via the
     aggregator and populate the cache.
  4. Build an OHLCVSeries from the cached bars.

Threading:
  Data fetches (network calls to yfinance or Alpaca) run in a background
  QThread to keep the UI responsive. The chart shows a loading state
  while the fetch is in progress. Results are delivered back to the main
  thread via Qt signals.

Default indicator set:
  On first load the controller adds the indicators defined in
  DEFAULT_INDICATORS below. The user can add or remove indicators during
  a session. In a future version this will be persisted to the config.
"""

import copy
from datetime import datetime, timedelta, timezone
from typing import Any

import finplot as fplt
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
from app.state import IndicatorState, State
from app.symbol_bar import SymbolBar
from app.watchlist import WatchlistWidget
from chart.window import ChartWidget
from data.aggregator import Aggregator
from data.cache import Cache
from data.models import AnchorRecord, Bar, OHLCVSeries, Timeframe
from data.provider import get_provider
from indicators.base import ChoiceParam, LINE_STYLE_OPTIONS
from indicators.registry import get as get_indicator, all_indicators
import plugins  # noqa: F401 — triggers registration of all indicators


# ------------------------------------------------------------------
# Default indicator set loaded on every symbol
# ------------------------------------------------------------------
# Each entry is (indicator_name, params). Adjust to taste.

DEFAULT_INDICATORS: list[tuple[str, dict[str, Any]]] = [
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

        Strategy:
          1. Check what is already cached (newest timestamp).
          2. Fetch only the missing bars from the provider.
          3. Write new bars to the cache.
          4. Read the full requested range from the cache and return it.
        """
        try:
            now = datetime.now(tz=timezone.utc)
            end = now

            newest_ts = self._cache.newest_cached_timestamp(
                self._symbol, self._timeframe
            )

            if newest_ts is not None:
                # Incremental fetch — only request bars newer than cache.
                start = datetime.fromtimestamp(newest_ts / 1000, tz=timezone.utc)
            else:
                # Full fetch — no cache for this symbol/timeframe.
                days_back = self._lookback_days
                start = datetime.fromtimestamp(
                    now.timestamp() - days_back * 86_400,
                    tz=timezone.utc,
                )

            new_bars = self._aggregator.fetch_bars(
                self._symbol, self._timeframe, start, end
            )

            if new_bars:
                self._cache.put_bars(self._symbol, self._timeframe, new_bars)

            # Read the full range from cache regardless of what was fetched.
            lookback_ms = int(
                (now.timestamp() - self._lookback_days * 86_400) * 1000
            )
            bars = self._cache.get_bars(
                self._symbol,
                self._timeframe,
                lookback_ms,
                int(now.timestamp() * 1000),
            )

            series = OHLCVSeries(
                symbol=self._symbol,
                timeframe=self._timeframe,
                bars=bars,
                loaded_range_start=datetime.fromtimestamp(
                    lookback_ms / 1000, tz=timezone.utc
                ),
                loaded_range_end=now,
            )
            self.finished.emit(series)

        except Exception as exc:
            self.error.emit(str(exc))


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
        for name, params in DEFAULT_INDICATORS:
            self._state.indicators.append(
                IndicatorState(name=name, params=dict(params))
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
        )

        # Wire chart interactions.
        self._chart.interactions.on_bar_clicked(self._on_bar_clicked)
        self._chart.interactions.on_bar_right_clicked(self._on_bar_right_clicked)

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
        self._per_symbol_state: dict[str, list[IndicatorState]] = {}

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

        self._chart.plot_manager.clear_all()
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
                IndicatorState(
                    name=s.name,
                    params=copy.deepcopy(s.params),
                    visible=s.visible,
                    series_visibility=copy.deepcopy(s.series_visibility),
                )
                for s in self._state.indicators
                if s.name != "avwap"   # AVWAP state is driven by DB anchors
            ]

        # Restore or initialize indicator state for the arriving symbol.
        if self._loaded_symbol != series.symbol:
            if series.symbol in self._per_symbol_state:
                self._state.indicators = [
                    IndicatorState(
                        name=s.name,
                        params=copy.deepcopy(s.params),
                        visible=s.visible,
                        series_visibility=copy.deepcopy(s.series_visibility),
                    )
                    for s in self._per_symbol_state[series.symbol]
                ]
            else:
                self._state.indicators = [
                    IndicatorState(name=name, params=dict(params))
                    for name, params in DEFAULT_INDICATORS
                ]

        self._loaded_symbol = series.symbol
        self._current_series = series
        self._state.anchors = self._cache.get_anchors(series.symbol)
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

        # If anchors were loaded from DB for this symbol but the avwap
        # IndicatorState was never created (e.g. app restart, first session
        # load), add it now so the anchors get drawn alongside the other
        # indicators rather than appearing as a surprise on the next add.
        if self._state.anchors and self._state.get_indicator("avwap") is None:
            self._state.indicators.append(
                IndicatorState(name="avwap", params={"anchors": []})
            )

        # Inject AVWAP anchors into the avwap indicator's params before
        # computing — the AVWAPIndicator reads them from params["anchors"].
        avwap_state = self._state.get_indicator("avwap")
        if avwap_state is not None:
            avwap_state.params["anchors"] = self._state.anchors

        # For intraday timeframes, load cached daily bars and inject them into
        # each indicator's params so day-based MAs can compute from daily closes
        # rather than from the limited intraday history yfinance provides.
        # Daily bars are always cached from the initial daily-chart load (the
        # default timeframe). Indicators that don't use this key ignore it.
        daily_bars: list[Bar] = []
        if series.timeframe.is_intraday:
            now = datetime.now(tz=timezone.utc)
            start_ms = int(
                (now - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).timestamp() * 1000
            )
            end_ms = int(now.timestamp() * 1000)
            daily_bars = self._cache.get_bars(
                series.symbol, Timeframe.DAILY, start_ms, end_ms
            )

        for ind_state in self._state.indicators:
            ind_state.params["_daily_bars"] = daily_bars
            self._compute_and_draw(ind_state, series)

        fplt.refresh()

    def _compute_and_draw(
        self,
        ind_state: IndicatorState,
        series: OHLCVSeries,
    ) -> None:
        """
        Run compute() for one indicator and push results to the PlotManager
        and legend.
        """
        indicator = get_indicator(ind_state.name)
        result    = indicator.compute(series, ind_state.params)

        # Track which series keys this indicator owns.
        ind_state.series_keys = list(result.keys())
        ind_state.series_visibility = {
            key: ind_state.series_visibility[key]
            for key in ind_state.series_keys
            if key in ind_state.series_visibility
        }

        default_color: str = ind_state.params.get("color", "#ffffff")
        default_width: float = float(ind_state.params.get("line_width", 1.0))
        style_param = ind_state.params.get("line_style", ChoiceParam("solid", LINE_STYLE_OPTIONS))
        default_style: str = style_param.value if isinstance(style_param, ChoiceParam) else str(style_param)
        pm = self._chart.plot_manager

        for series_key, values in result.items():
            # AVWAP series keys encode the anchor timestamp; each anchor has
            # its own color/width/style stored in the AnchorRecord, not in the
            # indicator's params dict.
            if series_key.startswith("avwap_"):
                ts_ms = int(series_key[6:])
                anchor = next(
                    (a for a in self._state.anchors if a.anchor_ts == ts_ms),
                    None,
                )
                color = anchor.color      if anchor else "#00FF88"
                width = anchor.line_width if anchor else 1.0
                style = anchor.line_style if anchor else "solid"
            else:
                color = default_color
                width = default_width
                style = default_style

            visible = ind_state.series_visibility.get(series_key, ind_state.visible)
            pm.update_indicator(series_key, values, color, width, style)
            pm.set_visible(series_key, visible)

            display = _series_key_to_label(series_key, ind_state)
            self._chart.legend.add_indicator(series_key, display, color)
            self._chart.legend.update_color(series_key, color)
            self._chart.legend.set_indicator_visible(series_key, visible)

    # ------------------------------------------------------------------
    # AVWAP anchor workflow
    # ------------------------------------------------------------------

    def _bar_index_to_ts_ms(self, x_pos: float) -> int | None:
        """
        Convert a finplot bar index to a UTC millisecond timestamp.

        finplot uses integer bar indexes on the x-axis (x_indexed=True),
        so clicks return a bar index float, not a Unix timestamp. We clamp
        to valid range and look up the bar's actual timestamp.
        """
        if self._current_series is None or not self._current_series.bars:
            return None
        bars = self._current_series.bars
        idx = max(0, min(int(x_pos), len(bars) - 1))
        return int(bars[idx].timestamp.timestamp() * 1000)

    def _on_bar_clicked(self, x_pos: float) -> None:
        """Left-click: reserved for future use (e.g. bar detail). No action."""
        pass

    def _on_bar_right_clicked(self, x_pos: float) -> None:
        """Right-click on a bar: show context menu."""
        utc_ts_ms = self._bar_index_to_ts_ms(x_pos)
        if utc_ts_ms is None:
            return
        menu = QMenu(self)
        add_avwap = menu.addAction("Add AVWAP here")
        action = menu.exec(QCursor.pos())
        if action == add_avwap:
            self._add_avwap_anchor(utc_ts_ms)

    def _add_avwap_anchor(self, utc_ts_ms: int) -> None:
        """
        Create, persist, and draw a new AVWAP anchor.

        1. Build an AnchorRecord with a default label (ISO date) and color.
        2. Persist it via the cache (sets anchor_id).
        3. Add it to state.anchors.
        4. Ensure the avwap IndicatorState exists, then recompute.
        """
        label = datetime.fromtimestamp(
            utc_ts_ms / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d")

        from chart.styles import AVWAP_PALETTE
        color = AVWAP_PALETTE[len(self._state.anchors) % len(AVWAP_PALETTE)]

        record = AnchorRecord(
            symbol=self._state.symbol,  # type: ignore[arg-type]
            anchor_ts=utc_ts_ms,
            label=label,
            color=color,
        )
        persisted = self._cache.put_anchor(record)
        self._state.anchors.append(persisted)

        # Ensure the avwap indicator is in the active set.
        if self._state.get_indicator("avwap") is None:
            self._state.indicators.append(
                IndicatorState(name="avwap", params={"anchors": []})
            )

        # Recompute all indicators with the updated anchor list.
        # We need the current series — re-read it from the cache.
        self._reload_indicators()

    def _reload_indicators(self) -> None:
        """
        Re-read bars from cache and recompute all indicators.
        Used after anchor add/remove or param changes — does not re-fetch
        from the provider.
        """
        if self._state.symbol is None:
            return
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
        pm.draw_candles(series)
        pm.draw_volume(series)

        # Inject anchors and recompute.
        avwap_state = self._state.get_indicator("avwap")
        if avwap_state is not None:
            avwap_state.params["anchors"] = self._state.anchors

        daily_bars_reload: list[Bar] = []
        if series.timeframe.is_intraday:
            now_r = datetime.now(tz=timezone.utc)
            start_ms_r = int(
                (now_r - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).timestamp() * 1000
            )
            daily_bars_reload = self._cache.get_bars(
                series.symbol, Timeframe.DAILY, start_ms_r,
                int(now_r.timestamp() * 1000),
            )

        for ind_state in self._state.indicators:
            ind_state.params["_daily_bars"] = daily_bars_reload
            self._compute_and_draw(ind_state, series)
        fplt.refresh()

    # ------------------------------------------------------------------
    # Indicator toggle and configuration
    # ------------------------------------------------------------------

    def _on_indicator_toggled(self, series_key: str) -> None:
        """Show or hide all plot lines belonging to the indicator."""
        ind_state = self._state.get_indicator_by_series_key(series_key)
        if ind_state is None:
            return
        if series_key.startswith("avwap_"):
            visible = not ind_state.series_visibility.get(series_key, ind_state.visible)
            ind_state.series_visibility[series_key] = visible
            self._chart.plot_manager.set_visible(series_key, visible)
            self._chart.legend.set_indicator_visible(series_key, visible)
            return

        ind_state.visible = not ind_state.visible
        pm = self._chart.plot_manager
        for key in ind_state.series_keys:
            pm.set_visible(key, ind_state.visible)
            self._chart.legend.set_indicator_visible(key, ind_state.visible)

    def _on_indicator_configure(self, series_key: str) -> None:
        """Open the config dialog for the indicator owning series_key."""
        if series_key.startswith("avwap_"):
            self._configure_avwap_anchor(series_key)
            return

        ind_state = self._state.get_indicator_by_series_key(series_key)
        if ind_state is None:
            return
        indicator = get_indicator(ind_state.name)
        dialog = IndicatorConfigDialog(
            indicator_label=indicator.label(),
            params=ind_state.params,
            parent=self,
        )
        if dialog.exec() == IndicatorConfigDialog.DialogCode.Accepted:
            ind_state.params = dialog.result_params()
            self._reload_indicators()

    def _configure_avwap_anchor(self, series_key: str) -> None:
        """
        Open a config dialog for a single AVWAP anchor.

        AVWAP anchor color lives in AnchorRecord, not in the indicator's
        params dict, so we can't use the generic indicator config path.
        We build a one-field params dict {"color": anchor.color}, let the
        user edit it, then persist the change via cache.update_anchor().
        """
        ts_ms = int(series_key[6:])
        anchor = next(
            (a for a in self._state.anchors if a.anchor_ts == ts_ms), None
        )
        if anchor is None:
            return

        dialog = IndicatorConfigDialog(
            indicator_label=f"AVWAP {anchor.label}",
            params={
                "color":      anchor.color,
                "line_width": anchor.line_width,
                "line_style": ChoiceParam(anchor.line_style, LINE_STYLE_OPTIONS),
            },
            parent=self,
        )
        if dialog.exec() == IndicatorConfigDialog.DialogCode.Accepted:
            result = dialog.result_params()
            updated = AnchorRecord(
                symbol=anchor.symbol,
                anchor_ts=anchor.anchor_ts,
                label=anchor.label,
                color=result["color"],
                line_width=result["line_width"],
                line_style=result["line_style"].value,
                anchor_id=anchor.anchor_id,
            )
            self._cache.update_anchor(updated)
            self._state.anchors = [
                updated if a.anchor_ts == ts_ms else a
                for a in self._state.anchors
            ]
            self._reload_indicators()

    def _on_indicator_remove(self, series_key: str) -> None:
        """
        Remove an indicator or a single AVWAP anchor from the chart.

        For AVWAP series (series_key starts with "avwap_"): deletes the
        specific anchor from the DB and removes only that line. If no
        anchors remain, the avwap IndicatorState is also removed.

        For other indicators: removes all plot lines belonging to the
        indicator and drops its IndicatorState.
        """
        if series_key.startswith("avwap_"):
            ts_ms = int(series_key[6:])
            anchor = next(
                (a for a in self._state.anchors if a.anchor_ts == ts_ms), None
            )
            if anchor is None:
                return
            self._cache.delete_anchor(anchor.anchor_id)  # type: ignore[arg-type]
            self._state.anchors = [
                a for a in self._state.anchors if a.anchor_ts != ts_ms
            ]
            avwap_state = self._state.get_indicator("avwap")
            if avwap_state is not None:
                avwap_state.series_visibility.pop(series_key, None)
            self._chart.plot_manager.remove_indicator(series_key)
            self._chart.legend.remove_indicator(series_key)
            if not self._state.anchors:
                # No anchors left — drop the avwap IndicatorState entirely.
                self._state.indicators = [
                    s for s in self._state.indicators if s.name != "avwap"
                ]
        else:
            ind_state = self._state.get_indicator_by_series_key(series_key)
            if ind_state is None:
                return
            for key in ind_state.series_keys:
                self._chart.plot_manager.remove_indicator(key)
                self._chart.legend.remove_indicator(key)
            self._state.indicators = [
                s for s in self._state.indicators if s is not ind_state
            ]

        fplt.refresh()

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
        # avwap is added via right-click on the chart, not this menu.
        _EXCLUDE = {"avwap"}

        registry = all_indicators()
        entries = {
            name: cls
            for name, cls in sorted(registry.items())
            if name not in _EXCLUDE
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
                IndicatorState(name=name, params=dialog.result_params())
            )
            self._reload_indicators()

    def closeEvent(self, event: object) -> None:  # type: ignore[override]
        """Clean up on close."""
        if self._fetch_thread and self._fetch_thread.isRunning():
            self._fetch_thread.quit()
            self._fetch_thread.wait()
        self._cache.close()
        super().closeEvent(event)  # type: ignore[arg-type]


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------

def _series_key_to_label(series_key: str, ind_state: IndicatorState) -> str:
    """
    Build a short human-readable legend label from a series key.

    "sma_50"               →  "SMA 50"
    "ema_20"               →  "EMA 20"
    "avwap_1704067200000"  →  "AVWAP 2024-01-01"
    """
    if series_key.startswith("sma_"):
        return f"SMA {series_key[4:]}"
    if series_key.startswith("ema_"):
        return f"EMA {series_key[4:]}"
    if series_key.startswith("avwap_"):
        ts_ms = int(series_key[6:])
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        return f"AVWAP {dt.strftime('%Y-%m-%d')}"
    return series_key
