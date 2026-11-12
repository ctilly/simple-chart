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
  2. Determine the desired history window and apply the provider's limit.
  3. Fetch missing ranges on both sides of the cached coverage interval.
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

import bisect
import contextvars
import copy
import logging
import math
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any

from PyQt6.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QCursor, QGuiApplication
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from app.extension_config import ExtensionConfigDialog
from app.application_settings import ApplicationSettingsDialog
from app.bar_comparison import BarComparisonService
from app.data_quality import (
    SymbolRefreshPlan,
    SymbolRefreshState,
    SymbolRefreshStatus,
)
from app.dialogs import (
    ApplicationMessageDialog,
    MessageKind,
    ask_basis_change_choice,
    show_information,
    show_warning,
)
from app.errors import ErrorContext, report_error
from app.extension_runtime import (
    ChartExtensionRenderPass,
    ChartExtensionRuntime,
    guarded_extension,
)
from app.extension_store import ChartExtensionStore
from app.header_bar import AppHeader
from app.load_timing import LoadTimingLog
from app.state import ChartExtensionState, State
from app.symbol_bar import SymbolBar
from app.watchlist import WatchlistWidget
from app.window_chrome import (
    MainWindowTitleBar,
    WindowResizeController,
    is_wayland_platform,
)
from chart.window import INITIAL_VISIBLE_BARS, ChartWidget
from data.adjustment import (
    BasisChange,
    BasisChangeChoice,
    BasisChangeRecord,
    detect_basis_change,
)
from data.aggregator import Aggregator
from data.cache import Cache
from data.calendar import bars_for_n_days
from data.errors import UserFacingError
from data.models import Bar, Level1Quote, MarketSnapshot, OHLCVSeries, Timeframe
from data.timestamps import ms_to_datetime
from data.session import (
    MARKET_TIMEZONE,
    REGULAR_SESSION_OPEN,
    completed_week_start,
    exchange_day_start as _exchange_day_start,
    daily_session_is_settled,
    expected_last_session_bar_start as _expected_last_session_bar_start,
    is_regular_session_timestamp,
    latest_expected_regular_session as _latest_expected_regular_session,
    regular_session_settled_at as _regular_session_settled_at,
    uses_regular_equity_session as _uses_regular_equity_session,
)
from data.provider import (
    ProviderAvailability,
    ProviderConfigurationError,
    create_provider,
    provider_availability,
)
from data.provider.base import (
    DataProvider,
    HistoryUnavailableError,
    RequestCancelledError,
    UnsupportedTimeframeError,
    background_requests,
)
from data.provider.config import ProviderConnection, YFINANCE_CONNECTION_ID
from data.provider.credentials import (
    CredentialStore,
    CredentialStoreAccess,
    initialize_keyring_credential_store,
)
from simplechart.extensions._guard import (
    call_extension,
    is_disabled,
    set_disable_handler,
    take_notices,
)
from simplechart.api import (
    ChartEvent,
    ChartExtension,
    ChoiceParam,
    DrawingSession,
    DrawingToolResult,
    ChartExtensionAddMode,
    ChartExtensionMutation,
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
    series_arrays,
)
from simplechart.plugins import load_plugins

load_plugins()

_LOGGER = logging.getLogger(__name__)

_EXTENSION_NOTICE_TITLE = "Extension Problem"


# ------------------------------------------------------------------
# Initial extension set loaded on every symbol
# ------------------------------------------------------------------
# Each entry is (extension_name, params). Adjust to taste.

INITIAL_EXTENSIONS: list[tuple[str, dict[str, Any]]] = [
    ("sma", {"days":  5, "color": "#FFA500", "line_width": 1.0, "line_style": ChoiceParam("solid", LINE_STYLE_OPTIONS)}),  # amber
    ("sma", {"days": 20, "color": "#00CED1", "line_width": 1.0, "line_style": ChoiceParam("solid", LINE_STYLE_OPTIONS)}),  # teal
    ("sma", {"days": 50, "color": "#1E90FF", "line_width": 1.0, "line_style": ChoiceParam("solid", LINE_STYLE_OPTIONS)}),  # blue
]

# Intraday data is intentionally bounded to keep the initial request and local
# cache size practical. Daily and weekly history use the fixed start below.
_DEFAULT_LOOKBACK_DAYS = 600
_DAILY_HISTORY_START = datetime(2016, 1, 1, tzinfo=timezone.utc)
_SNAPSHOT_REFRESH_MS = 60_000
_DRAWING_PREVIEW_FRAME_MS = 16
_CHART_LOAD_DEBOUNCE_MS = 100
_MAX_ACTIVE_CHART_LOADS = 2
# Concurrent history chunk requests per range fetch. Also the bound on chunks
# dispatched ahead of the committed run, so at most this many minus one
# completed chunks can be lost to a cancellation or failure.
_HISTORY_FETCH_WORKERS = 4
# Upper bound on the first fetch of a two-phase load when it is widened to
# cover indicator lookbacks; longer lookbacks render as backfill reaches them.
# 125 days covers a 50-day SMA on every intraday timeframe: 65m needs 34
# sessions for the 200-bar initial view plus 50 for the SMA (~125 calendar
# days with holiday margin), fetched as concurrent foreground requests.
_MAX_RECENT_WINDOW_DAYS = 125
# Minimum spacing between interim re-renders while backfill unlocks content.
_HISTORY_REFRESH_INTERVAL_MS = 1000
# Cached bars compared against the provider to detect a changed adjustment
# basis (a split or dividend applied to older history); see
# _basis_validation_bars. The lookbacks reach past holidays.
# Older history (History wave): the oldest start ever requested (providers
# clip it to what they have), the minimum intraday chunk, and outcomes.
_OLDEST_HISTORY_START = datetime(1900, 1, 1, tzinfo=timezone.utc)
_OLDER_HISTORY_CHUNK = timedelta(days=120)
# A chunk's span less session alignment (weekends, holidays): coverage this
# far older than its oldest bar means an older request returned nothing.
_EMPTY_OLDER_SPAN = _OLDER_HISTORY_CHUNK - timedelta(days=7)
_OLDER_MORE = "more"
_OLDER_EXHAUSTED = "exhausted"
_OLDER_RESET = "reset"
_OLDER_HISTORY_ERROR_MS = 10_000
# Below every bar timestamp (UTC ms), including history before 1970.
_OLDEST_TIMESTAMP_MS = -(2**63)
_BASIS_VALIDATION_SESSIONS = 2
# Records of one split or dividend found on different timeframes measure
# its ratio from different bars; within this relative tolerance they are the
# same event and share one answer.
_SAME_BASIS_EVENT_TOLERANCE = 2e-3
_BASIS_VALIDATION_BARS = {Timeframe.DAILY: 10, Timeframe.WEEKLY: 4}
_BASIS_VALIDATION_INTRADAY_LOOKBACK = timedelta(days=10)
_BASIS_VALIDATION_LOOKBACK = {
    Timeframe.DAILY: timedelta(days=31),
    Timeframe.WEEKLY: timedelta(days=60),
}
_ASSET_REFERENCE_MAX_AGE = timedelta(days=30)
_ASSET_REFERENCE_RETRY_DELAY = timedelta(hours=1)
_CHART_TIMEFRAMES = tuple(
    timeframe for timeframe in Timeframe if timeframe != Timeframe.MIN1
)


def _history_start(
    timeframe: Timeframe,
    aggregator: Aggregator,
    end: datetime,
) -> datetime:
    """
    Start of the history window ending at end. Intraday windows start at the
    first session open at or after the lookback cutoff, so a window never
    begins inside a session and splits a synthesized period.
    """
    desired = (
        end - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        if timeframe.is_intraday
        else _DAILY_HISTORY_START
    )
    provider_limit = aggregator.earliest_history_start(timeframe, end)
    cutoff = desired if provider_limit is None else max(desired, provider_limit)
    if not timeframe.is_intraday:
        return cutoff
    return _first_session_open_at_or_after(cutoff)


def _display_history_start(
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    fetch_start: datetime,
    end: datetime,
) -> datetime:
    """
    Start of the bars a chart shows once its history from fetch_start has been
    fetched: the first session open of the contiguously fetched cached history
    when it reaches back past fetch_start, so intraday history older than the
    provider still returns stays visible. Older caches may start mid-session;
    their partial first session is not shown. Floored at the intraday lookback
    cutoff.
    """
    if not timeframe.is_intraday:
        return fetch_start
    coverage = cache.get_bar_fetch_coverage(cache_namespace, symbol, timeframe)
    fetch_start_ms = int(fetch_start.timestamp() * 1000)
    if coverage is None or not coverage[0] < fetch_start_ms <= coverage[1]:
        return fetch_start
    floor = _first_session_open_at_or_after(
        end - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
    )
    coverage_start = _first_session_open_at_or_after(
        ms_to_datetime(coverage[0])
    )
    return min(fetch_start, max(floor, coverage_start))


def _first_session_open_at_or_after(cutoff: datetime) -> datetime:
    """
    The first weekday 09:30 ET at or after cutoff. Weekday holidays are not
    known, so a cutoff on one stays on that day.
    """
    local = cutoff.astimezone(MARKET_TIMEZONE)
    day = local.date()
    if local.time() > REGULAR_SESSION_OPEN:
        day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(
        day,
        REGULAR_SESSION_OPEN,
        tzinfo=MARKET_TIMEZONE,
    ).astimezone(cutoff.tzinfo)


def _history_end(
    timeframe: Timeframe,
    aggregator: Aggregator,
    end: datetime,
) -> datetime:
    return aggregator.latest_history_end(timeframe, end)


def _refresh_history_starts(
    aggregator: Aggregator,
    now: datetime,
) -> dict[Timeframe, datetime]:
    """
    _history_start of every chart timeframe the route serves and of each base
    timeframe it synthesizes from.
    """
    starts: dict[Timeframe, datetime] = {}
    for timeframe in _CHART_TIMEFRAMES:
        try:
            native = aggregator.native_timeframe(timeframe)
        except UnsupportedTimeframeError:
            continue
        for fetched in (timeframe, native):
            starts[fetched] = _history_start(
                fetched,
                aggregator,
                _history_end(fetched, aggregator, now),
            )
    return starts


def _rewind_boundaries(
    history_starts: Mapping[Timeframe, datetime],
) -> dict[Timeframe, int]:
    """
    UTC ms from which a symbol refresh replaces each timeframe's cached bars:
    its history start, or for weekly bars the first week start at or after
    it, so the week bar the refill does not request is kept.
    """
    boundaries: dict[Timeframe, int] = {}
    for timeframe, start in history_starts.items():
        boundary = start
        if timeframe == Timeframe.WEEKLY:
            boundary = completed_week_start(start)
            if boundary != start:
                # Wall-clock arithmetic in the exchange time zone keeps the
                # next week start at local midnight across daylight saving.
                boundary = (
                    boundary.astimezone(MARKET_TIMEZONE) + timedelta(weeks=1)
                ).astimezone(start.tzinfo)
        boundaries[timeframe] = int(boundary.timestamp() * 1000)
    return boundaries


def _recent_history_start(
    timeframe: Timeframe,
    history_start: datetime,
    end: datetime,
    lookback_days: int = 0,
) -> datetime:
    """
    Start of the recent window that fills the chart's initial view.

    Enough calendar days for INITIAL_VISIBLE_BARS bars of the timeframe plus
    lookback_days trading days before them (so indicators with that lookback
    are exact on the initial view), plus a week of holiday margin. Widening
    for the lookback stops at _MAX_RECENT_WINDOW_DAYS. Aligned to a session
    boundary so synthesized bars never start mid-session.
    """
    view_days = math.ceil(INITIAL_VISIBLE_BARS / bars_for_n_days(1, timeframe))
    view_calendar_days = math.ceil(view_days * 7 / 5) + 7
    calendar_days = max(
        view_calendar_days,
        min(
            math.ceil((view_days + lookback_days) * 7 / 5) + 7,
            _MAX_RECENT_WINDOW_DAYS,
        ),
    )
    return max(
        history_start,
        _exchange_day_start(end - timedelta(days=calendar_days)),
    )


def _needs_history_backfill(
    aggregator: Aggregator,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    history_start: datetime,
    recent_start: datetime,
) -> bool:
    """
    True when a load should render its recent window before fetching older
    history: the provider pages history by date and the fetched timeframe's
    cache does not reach the history start. Legacy or already-complete
    synthesized targets stay single-phase; their remaining work is local.
    """
    if not timeframe.is_intraday or recent_start <= history_start:
        return False
    if aggregator.history_page_span(timeframe) is None:
        return False
    history_start_ms = int(history_start.timestamp() * 1000)
    fetched_timeframe = aggregator.native_timeframe(timeframe)
    coverage = cache.get_bar_fetch_coverage(
        cache_namespace,
        symbol,
        fetched_timeframe,
    )
    if coverage is not None and coverage[0] <= history_start_ms:
        return False
    if fetched_timeframe != timeframe:
        target_coverage = cache.get_bar_fetch_coverage(
            cache_namespace,
            symbol,
            timeframe,
        )
        if target_coverage is not None and (
            target_coverage[0] <= history_start_ms
            or _is_legacy_target_coverage(aggregator, timeframe, target_coverage)
        ):
            return False
    return True


@dataclass(frozen=True)
class _DataRoute:
    aggregator: Aggregator
    connection: ProviderConnection

    @property
    def cache_namespace(self) -> str:
        return self.connection.cache_namespace

    @property
    def display_name(self) -> str:
        feed = self.connection.feed
        if feed is None:
            return self.connection.display_name
        return f"{self.connection.display_name} / {feed.display_name}"


@dataclass(frozen=True)
class _StartupRoutes:
    selected: _DataRoute
    yahoo: _DataRoute
    fallback_reason: str | None


@dataclass(frozen=True)
class _SnapshotBatch:
    aggregator: Aggregator
    symbols: list[str]


@dataclass(frozen=True)
class _QuoteRouteIdentity:
    connection: ProviderConnection
    generation: int


@dataclass(frozen=True)
class _SnapshotRequest:
    membership_revision: int
    routes: tuple[tuple[str, _QuoteRouteIdentity], ...]


@dataclass(frozen=True)
class _MaterializationRange:
    range_start_ms: int
    range_end_ms: int
    coverage_start_ms: int
    coverage_end_ms: int


@dataclass(frozen=True)
class _Level1Request:
    symbol: str
    symbol_generation: int
    route: _QuoteRouteIdentity


@dataclass(frozen=True)
class _ChartLoadRequest:
    generation: int
    symbol: str
    timeframe: Timeframe
    connection: ProviderConnection
    # time.monotonic() at the user action; only for the opt-in timing log.
    requested_at: float = field(default=0.0, compare=False)
    # Longest history lookback (trading days) among the chart's extensions;
    # widens the first fetch of a two-phase load.
    lookback_days: int = field(default=0, compare=False)
    # History start a symbol refresh rewound this chart's timeframe to; the
    # reload fetches from it instead of reading the clock again. None reads
    # the clock.
    history_start: datetime | None = None

    @property
    def cache_namespace(self) -> str:
        return self.connection.cache_namespace


@dataclass(frozen=True)
class _SymbolRefresh:
    cache_namespace: str
    symbol: str
    aggregator: Aggregator


@dataclass(frozen=True)
class _RefreshReload:
    """The on-screen reload of a refreshed symbol, until it ends."""

    request: _ChartLoadRequest
    rewound_at: datetime


@dataclass
class _ChartLoadTask:
    request: _ChartLoadRequest
    thread: QThread
    worker: "_FetchWorker | _OlderHistoryWorker"
    cancellation_event: threading.Event
    # An older-history load for the displayed request, not a chart load.
    older: bool = False


def _preserves_ui_state_per_symbol(extension_name: str) -> bool:
    extension = guarded_extension(extension_name)
    if extension is None:
        return False
    return bool(call_extension(extension_name, extension.preserve_ui_state_per_symbol))


def _clock_text(moment: datetime) -> str:
    """Local wall-clock time like "2:04 AM"."""
    local = moment.astimezone()
    meridiem = "AM" if local.hour < 12 else "PM"
    return f"{local.hour % 12 or 12}:{local.minute:02d} {meridiem}"


def _refreshed_off_screen_text(symbol: str, rewound_at: datetime) -> str:
    return (
        f"{symbol} refreshed at {_clock_text(rewound_at)}. "
        "It downloads when you open it."
    )


def _is_yahoo_futures_symbol(symbol: str) -> bool:
    return symbol.strip().upper().endswith("=F")


def _completed_coverage_end(
    symbol: str,
    timeframe: Timeframe,
    endpoint: datetime,
    completed_end: datetime,
) -> datetime:
    if timeframe != Timeframe.DAILY or not _uses_regular_equity_session(symbol):
        return completed_end
    if not daily_session_is_settled(endpoint):
        return completed_end
    session_date, _ = _latest_expected_regular_session(endpoint)
    return _regular_session_settled_at(
        timeframe,
        session_date,
        endpoint.tzinfo,
    )


def _cached_tail_refresh_start(
    aggregator: Aggregator,
    symbol: str,
    timeframe: Timeframe,
    history_start: datetime,
    coverage_end: datetime,
    newest_cached: datetime | None,
    endpoint: datetime,
) -> datetime | None:
    coverage_overlap = aggregator.refresh_start(timeframe, coverage_end)
    if (
        timeframe == Timeframe.WEEKLY
        or not _uses_regular_equity_session(symbol)
    ):
        return coverage_overlap

    session_date, session_active = _latest_expected_regular_session(endpoint)
    if session_active:
        return coverage_overlap

    expected_last_bar = _expected_last_session_bar_start(
        timeframe,
        session_date,
        endpoint.tzinfo,
    )
    settled_at = _regular_session_settled_at(
        timeframe,
        session_date,
        endpoint.tzinfo,
    )
    coverage_is_settled = (
        coverage_end >= settled_at
        if timeframe == Timeframe.DAILY
        else coverage_end > settled_at
    )
    if (
        newest_cached is not None
        and newest_cached >= expected_last_bar
        and coverage_is_settled
    ):
        return None
    if newest_cached is None:
        if timeframe == Timeframe.DAILY:
            return max(history_start, min(coverage_overlap, expected_last_bar))
        return coverage_overlap
    newest_overlap = aggregator.refresh_start(timeframe, newest_cached)
    return max(history_start, min(coverage_overlap, newest_overlap))


def _route_for_symbol(
    symbol: str,
    selected_route: _DataRoute,
    yahoo_route: _DataRoute,
) -> _DataRoute:
    return yahoo_route if _is_yahoo_futures_symbol(symbol) else selected_route


def _provider_availability_by_name(
    connections: Iterable[ProviderConnection],
    credential_access: CredentialStoreAccess,
) -> dict[str, ProviderAvailability]:
    return {
        connection.provider_name: provider_availability(
            connection.provider_name,
            credential_access,
        )
        for connection in connections
    }


def _build_startup_routes(
    cache: Cache,
    connection_id: str,
    credential_store: CredentialStore,
    availability_by_name: Mapping[str, ProviderAvailability],
) -> _StartupRoutes:
    selected_connection = cache.get_provider_connection(connection_id)
    if selected_connection is None:
        raise ValueError(f"Unknown provider connection: {connection_id}")

    yahoo_connection = cache.get_provider_connection(YFINANCE_CONNECTION_ID)
    if yahoo_connection is None:
        raise RuntimeError("The Yahoo Finance connection is not configured.")
    yahoo_availability = availability_by_name.get(yahoo_connection.provider_name)
    if yahoo_availability is None or not yahoo_availability.available:
        reason = (
            "Provider availability was not checked."
            if yahoo_availability is None
            else yahoo_availability.reason
        )
        raise ProviderConfigurationError(f"Yahoo Finance is unavailable: {reason}")

    yahoo_route = _DataRoute(
        Aggregator(create_provider(yahoo_connection, credential_store)),
        yahoo_connection,
    )
    if selected_connection.connection_id == YFINANCE_CONNECTION_ID:
        return _StartupRoutes(yahoo_route, yahoo_route, None)

    selected_availability = availability_by_name.get(
        selected_connection.provider_name
    )
    if selected_availability is None or not selected_availability.available:
        reason = (
            "Provider availability was not checked."
            if selected_availability is None
            else selected_availability.reason
        )
        return _StartupRoutes(
            yahoo_route,
            yahoo_route,
            f"{selected_connection.display_name} is unavailable: {reason}",
        )

    try:
        selected_provider = create_provider(selected_connection, credential_store)
    except ProviderConfigurationError as exc:
        # The configuration message names the provider itself.
        context = ErrorContext(f"Connecting to {selected_connection.display_name}")
        return _StartupRoutes(
            yahoo_route,
            yahoo_route,
            report_error(_LOGGER, exc, context),
        )
    return _StartupRoutes(
        _DataRoute(Aggregator(selected_provider), selected_connection),
        yahoo_route,
        None,
    )


class _FetchWorker(QObject):
    """
    Fetches one chart request and reports UI results separately from cleanup.

    Success and error signals include the request and are suppressed after
    cancellation. The completed signal fires exactly once on every terminal
    path so the controller can stop the thread and release task ownership.
    """

    recent_ready: pyqtSignal = pyqtSignal(object, object)
    # (request, earliest contiguous cached bar timestamp in UTC ms) after each
    # committed backfill chunk; carries no bars.
    history_progress: pyqtSignal = pyqtSignal(object, object)
    finished: pyqtSignal = pyqtSignal(object, object)
    error: pyqtSignal = pyqtSignal(object, str)
    completed: pyqtSignal = pyqtSignal()

    def __init__(
        self,
        db_path: str,
        credential_store: CredentialStore,
        request: _ChartLoadRequest,
        cancellation_event: threading.Event | None = None,
        load_timing: LoadTimingLog | None = None,
    ) -> None:
        super().__init__()
        self._db_path = db_path
        self._credential_store = credential_store
        self._request = request
        self._cancellation_event = cancellation_event or threading.Event()
        self._load_timing = load_timing or LoadTimingLog(None)

    def _record_daily_done(self) -> None:
        self._record_step("daily refs done")

    def _emit_history_progress(self, earliest_ms: int) -> None:
        if not self._cancellation_event.is_set():
            self.history_progress.emit(self._request, earliest_ms)

    def _record_step(self, step: str) -> None:
        self._load_timing.record(
            self._request.generation,
            self._request.symbol,
            self._request.timeframe.value,
            step,
            self._request.requested_at,
        )

    def run(self) -> None:
        """
        Fetch bars and return an OHLCVSeries.

        Intraday loads also refresh daily bars so day-based indicators have
        reference data without requiring the user to visit the daily chart.

        When older history is missing from a date-paged provider, the recent
        window that fills the initial view (widened for the chart's indicator
        lookbacks) is fetched and emitted through recent_ready first;
        history_progress reports each committed older chunk, and the full
        history window is emitted through finished.
        """
        try:
            self._record_step("worker started")
            _check_chart_load_cancelled(self._cancellation_event)
            provider = create_provider(
                self._request.connection,
                self._credential_store,
            )
            self._record_step("provider ready")
            aggregator = Aggregator(provider)
            now = datetime.now(tz=timezone.utc)
            with Cache(self._db_path) as cache:
                try:
                    series = self._fetch_chart_series(aggregator, cache, now)
                except _BasisReset:
                    # The provider moved this paged timeframe's history to a
                    # new adjustment basis and its cache was reset: load it
                    # again as a cold load, recent window first.
                    series = self._fetch_chart_series(aggregator, cache, now)
            _check_chart_load_cancelled(self._cancellation_event)
            # Built here, off the UI thread; rendering reuses them.
            series_arrays(series)
            self._record_step("history fetched")
            self.finished.emit(self._request, series)
        except _ChartLoadCancelled:
            pass

        except Exception as exc:
            if not self._cancellation_event.is_set():
                request = self._request
                context = ErrorContext(
                    f"Loading {request.symbol} {request.timeframe.value} history",
                )
                self.error.emit(request, report_error(_LOGGER, exc, context))
        finally:
            self.completed.emit()

    def _fetch_chart_series(
        self,
        aggregator: Aggregator,
        cache: Cache,
        now: datetime,
    ) -> OHLCVSeries:
        """
        Fetch the request's series: in one phase when the cache already
        reaches the history start, otherwise the recent window first
        (emitted through recent_ready), then older history in the
        background.
        """
        symbol = self._request.symbol
        timeframe = self._request.timeframe
        cache_namespace = self._request.cache_namespace
        history_end = _history_end(timeframe, aggregator, now)
        history_start = (
            self._request.history_start
            if self._request.history_start is not None
            else _history_start(timeframe, aggregator, history_end)
        )
        recent_start = _recent_history_start(
            timeframe,
            history_start,
            history_end,
            self._request.lookback_days,
        )
        if _needs_history_backfill(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            timeframe,
            history_start,
            recent_start,
        ):
            recent = _fetch_series_with_concurrent_references(
                aggregator,
                self._db_path,
                cache,
                cache_namespace,
                symbol,
                timeframe,
                now,
                history_start=recent_start,
                cancellation_event=self._cancellation_event,
                on_references_done=self._record_daily_done,
            )
            _check_chart_load_cancelled(self._cancellation_event)
            self._record_step("recent fetched")
            if recent.bars:
                series_arrays(recent)
                self.recent_ready.emit(self._request, recent)
            # Older history yields to foreground requests (other chart
            # loads, quotes) and stops sending once cancelled.
            with background_requests(self._cancellation_event):
                series = _fetch_series(
                    aggregator,
                    cache,
                    cache_namespace,
                    symbol,
                    timeframe,
                    now,
                    history_start=history_start,
                    cancellation_event=self._cancellation_event,
                    on_history_progress=self._emit_history_progress,
                )
        else:
            series = _fetch_series_with_concurrent_references(
                aggregator,
                self._db_path,
                cache,
                cache_namespace,
                symbol,
                timeframe,
                now,
                history_start=history_start,
                cancellation_event=self._cancellation_event,
                on_references_done=self._record_daily_done,
            )
        return series


class _OlderHistoryWorker(QObject):
    """
    Loads history older than the displayed series: from the cache first,
    then from the provider, at background request priority.

    finished carries (request, series or None, outcome) with outcome one of
    _OLDER_MORE, _OLDER_EXHAUSTED or _OLDER_RESET; it and error are
    suppressed after cancellation. completed fires exactly once.
    """

    finished: pyqtSignal = pyqtSignal(object, object, str)
    error: pyqtSignal = pyqtSignal(object, str)
    completed: pyqtSignal = pyqtSignal()

    def __init__(
        self,
        db_path: str,
        credential_store: CredentialStore,
        request: _ChartLoadRequest,
        displayed_start: datetime,
        displayed_end: datetime,
        bars_wanted: int,
        cancellation_event: threading.Event,
    ) -> None:
        super().__init__()
        self._db_path = db_path
        self._credential_store = credential_store
        self._request = request
        self._displayed_start = displayed_start
        self._displayed_end = displayed_end
        self._bars_wanted = bars_wanted
        self._cancellation_event = cancellation_event

    def run(self) -> None:
        request = self._request
        try:
            _check_chart_load_cancelled(self._cancellation_event)
            aggregator = Aggregator(
                create_provider(request.connection, self._credential_store)
            )
            with background_requests(self._cancellation_event), Cache(
                self._db_path
            ) as cache:
                series, outcome = _fetch_older_history(
                    aggregator,
                    cache,
                    request.cache_namespace,
                    request.symbol,
                    request.timeframe,
                    self._displayed_start,
                    self._displayed_end,
                    self._bars_wanted,
                    datetime.now(tz=timezone.utc),
                    self._cancellation_event,
                )
            _check_chart_load_cancelled(self._cancellation_event)
            if series is not None:
                series_arrays(series)
            self.finished.emit(request, series, outcome)
        except _ChartLoadCancelled:
            pass
        except Exception as exc:
            if not self._cancellation_event.is_set():
                context = ErrorContext(
                    f"Loading older {request.symbol} "
                    f"{request.timeframe.value} history",
                )
                self.error.emit(request, report_error(_LOGGER, exc, context))
        finally:
            self.completed.emit()


def _fetch_older_history(
    aggregator: Aggregator,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    displayed_start: datetime,
    displayed_end: datetime,
    bars_wanted: int,
    now: datetime,
    cancellation_event: threading.Event | None,
) -> tuple[OHLCVSeries | None, str]:
    """
    Extend the displayed series [displayed_start, displayed_end] back by at
    least bars_wanted bars where history exists: cached contiguous history
    first, then the provider (never before its history limit). Returns the
    extended series (None when nothing older exists) and the outcome:
    _OLDER_RESET when the provider now returns the oldest cached bars on
    another adjustment basis (the cache is reset here and a normal load
    downloads it again), _OLDER_EXHAUSTED when no older history exists, else
    _OLDER_MORE.
    """
    native = aggregator.native_timeframe(timeframe)
    want = _older_history_start(timeframe, displayed_start, bars_wanted)
    limit = aggregator.earliest_history_start(native, now)
    if limit is None:
        floor = _OLDEST_HISTORY_START
    elif native.is_intraday:
        # Loads start at the first session open after the limit; the sliver
        # before it holds no bars and is not requested.
        floor = _first_session_open_at_or_after(limit)
    else:
        floor = limit
    floor_ms = int(floor.timestamp() * 1000)
    coverage = cache.get_bar_fetch_coverage(cache_namespace, symbol, native)
    if coverage is None:
        return None, _OLDER_EXHAUSTED
    want_ms = int(want.timestamp() * 1000)
    if (
        want_ms < coverage[0]
        and not _older_history_exhausted(cache, cache_namespace, symbol, native, coverage, floor_ms)
    ):
        seam = _seam_validation_bars(cache, cache_namespace, symbol, native, coverage[0])
        if seam:
            # The seam range has cached bars, so the provider has them too:
            # a refusal here is an error, not missing history.
            fetched = aggregator.fetch_bars(
                symbol,
                native,
                seam[0].timestamp,
                seam[-1].timestamp + _bar_span(native),
            )
            change = detect_basis_change(seam, fetched)
            if change is not None:
                # A normal load validates only the newest sessions, so the
                # reset happens here; the load then starts cold.
                _reset_for_basis_change(
                    aggregator,
                    cache,
                    cache_namespace,
                    symbol,
                    native,
                    change,
                    _history_start(native, aggregator, now),
                    now,
                )
                return None, _OLDER_RESET
        _check_chart_load_cancelled(cancellation_event)
        coverage_start = ms_to_datetime(coverage[0])
        try:
            _fetch_bar_range(
                aggregator,
                cache,
                cache_namespace,
                symbol,
                native,
                ms_to_datetime(max(want_ms, floor_ms)),
                coverage_start,
                coverage_end=coverage_start,
                cancellation_event=cancellation_event,
            )
        except HistoryUnavailableError:
            # The provider has no older history for this range.
            return None, _OLDER_EXHAUSTED
        coverage = cache.get_bar_fetch_coverage(cache_namespace, symbol, native)
        assert coverage is not None
    exhausted = _older_history_exhausted(
        cache, cache_namespace, symbol, native, coverage, floor_ms,
    )
    if native != timeframe:
        # From a session open, so no synthesized bar is built from part of a
        # session (an older cache may start mid-session).
        rebuild_start = _first_session_open_at_or_after(
            ms_to_datetime(max(want_ms, coverage[0]))
        )
        _materialize_synthesized_history(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            timeframe,
            int(rebuild_start.timestamp() * 1000),
        )
        target_coverage = cache.get_bar_fetch_coverage(cache_namespace, symbol, timeframe)
        if target_coverage is not None:
            coverage = target_coverage
    coverage_start = ms_to_datetime(coverage[0])
    if timeframe.is_intraday:
        coverage_start = _first_session_open_at_or_after(coverage_start)
    display_start = max(want, coverage_start)
    if display_start >= displayed_start:
        return None, _OLDER_EXHAUSTED
    series = OHLCVSeries(
        symbol,
        timeframe,
        cache.get_bars(
            cache_namespace,
            symbol,
            timeframe,
            int(display_start.timestamp() * 1000),
            int(displayed_end.timestamp() * 1000),
        ),
    )
    if not series.bars or series.bars[0].timestamp >= displayed_start:
        # Nothing older exists (before a listing, or nothing cached).
        return None, _OLDER_EXHAUSTED
    if exhausted and display_start == coverage_start:
        return series, _OLDER_EXHAUSTED
    return series, _OLDER_MORE


def _older_history_start(
    timeframe: Timeframe,
    displayed_start: datetime,
    bars_wanted: int,
) -> datetime:
    """
    Start of the next older chunk: intraday at least _OLDER_HISTORY_CHUNK
    before the displayed start, more when the view wants more bars, at a
    session open; daily and weekly everything (they are one request).
    """
    if not timeframe.is_intraday:
        return _OLDEST_HISTORY_START
    days = math.ceil(bars_wanted / bars_for_n_days(1, timeframe) * 7 / 5) + 7
    span = max(_OLDER_HISTORY_CHUNK, timedelta(days=days))
    return _first_session_open_at_or_after(displayed_start - span)


def _older_history_exhausted(
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    coverage: tuple[int, int],
    floor_ms: int,
) -> bool:
    """
    True when no older history can be fetched: coverage reaches the
    provider's floor, or an older request returned nothing (the oldest bar in
    coverage is about a chunk after its start: before the listing or the
    provider's oldest data). Derived from the cache, so it holds across
    restarts; holidays and halts shorter than _EMPTY_OLDER_SPAN do not count.
    """
    if coverage[0] <= floor_ms:
        return True
    oldest = cache.oldest_cached_timestamp(
        cache_namespace,
        symbol,
        timeframe,
        coverage[0],
    )
    if oldest is None:
        return True
    return oldest - coverage[0] >= _EMPTY_OLDER_SPAN.total_seconds() * 1000


def _seam_validation_bars(
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    coverage_start_ms: int,
) -> list[Bar]:
    """
    The oldest cached bars, compared against the provider before older bars
    are joined to them: the first cached session intraday, the first ten
    daily or four weekly bars.
    """
    lookback = (
        _BASIS_VALIDATION_INTRADAY_LOOKBACK
        if timeframe.is_intraday
        else _BASIS_VALIDATION_LOOKBACK[timeframe]
    )
    bars = cache.get_raw_bars(
        cache_namespace,
        symbol,
        timeframe,
        coverage_start_ms,
        coverage_start_ms + int(lookback.total_seconds() * 1000),
    )
    if not timeframe.is_intraday:
        return bars[: _BASIS_VALIDATION_BARS[timeframe]]
    if not bars:
        return []
    first_session = _exchange_day_start(bars[0].timestamp)
    return [
        bar for bar in bars if _exchange_day_start(bar.timestamp) == first_session
    ]


def _bar_span(timeframe: Timeframe) -> timedelta:
    if timeframe == Timeframe.WEEKLY:
        return timedelta(days=7)
    if timeframe == Timeframe.DAILY:
        return timedelta(days=1)
    minutes = timeframe.minutes
    assert minutes is not None
    return timedelta(minutes=minutes)


class _ChartLoadCancelled(Exception):
    pass


class _BasisReset(Exception):
    """
    A paged timeframe's cache was reset for a new adjustment basis; the
    chart load starts over so the re-download renders recent bars first.
    """


def _check_chart_load_cancelled(
    cancellation_event: threading.Event | None,
) -> None:
    if cancellation_event is not None and cancellation_event.is_set():
        raise _ChartLoadCancelled


class _SnapshotWorker(QObject):
    finished: pyqtSignal = pyqtSignal(object, object)
    error: pyqtSignal = pyqtSignal(object, str)
    completed: pyqtSignal = pyqtSignal()

    def __init__(
        self,
        batches: list[_SnapshotBatch],
        request: _SnapshotRequest,
        cancellation_event: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self._batches = list(batches)
        self._request = request
        self._cancellation_event = cancellation_event or threading.Event()

    def run(self) -> None:
        try:
            snapshots: dict[str, MarketSnapshot] = {}
            for batch in self._batches:
                _check_data_worker_cancelled(self._cancellation_event)
                snapshots.update(batch.aggregator.fetch_snapshots(batch.symbols))
            _check_data_worker_cancelled(self._cancellation_event)
            self.finished.emit(self._request, snapshots)
        except _DataWorkerCancelled:
            pass
        except Exception as exc:
            if not self._cancellation_event.is_set():
                context = ErrorContext("Loading watchlist quotes")
                self.error.emit(self._request, report_error(_LOGGER, exc, context))
        finally:
            self.completed.emit()


class _Level1Worker(QObject):
    finished: pyqtSignal = pyqtSignal(object, object)
    error: pyqtSignal = pyqtSignal(object, str)
    completed: pyqtSignal = pyqtSignal()

    def __init__(
        self,
        aggregator: Aggregator,
        reference_aggregator: Aggregator,
        db_path: str,
        request: _Level1Request,
        refresh_reference: bool,
        now: datetime,
        cancellation_event: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self._aggregator = aggregator
        self._reference_aggregator = reference_aggregator
        self._db_path = db_path
        self._request = request
        self._refresh_reference = refresh_reference
        self._now = now
        self._cancellation_event = cancellation_event or threading.Event()

    def run(self) -> None:
        try:
            _check_data_worker_cancelled(self._cancellation_event)
            with Cache(self._db_path) as cache:
                _check_data_worker_cancelled(self._cancellation_event)
                quote = self._aggregator.fetch_level1(self._request.symbol)
                _check_data_worker_cancelled(self._cancellation_event)
                if quote is not None:
                    known_reference_name = (
                        quote.company_name
                        if self._aggregator is self._reference_aggregator
                        else None
                    )
                    company_name = _resolve_company_name(
                        cache,
                        self._reference_aggregator,
                        self._request.symbol,
                        self._now,
                        self._refresh_reference,
                        known_reference_name,
                    )
                    _check_data_worker_cancelled(self._cancellation_event)
                    if company_name != quote.company_name:
                        quote = replace(quote, company_name=company_name)
            self.finished.emit(self._request, quote)
        except _DataWorkerCancelled:
            pass
        except Exception as exc:
            if not self._cancellation_event.is_set():
                request = self._request
                context = ErrorContext(f"Loading the {request.symbol} quote")
                self.error.emit(request, report_error(_LOGGER, exc, context))
        finally:
            self.completed.emit()


class _ProviderValidationWorker(QObject):
    finished: pyqtSignal = pyqtSignal()
    error: pyqtSignal = pyqtSignal(str)
    completed: pyqtSignal = pyqtSignal()

    def __init__(
        self,
        provider: DataProvider,
        provider_name: str,
        cancellation_event: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._provider_name = provider_name
        self._cancellation_event = cancellation_event or threading.Event()

    def run(self) -> None:
        try:
            _check_data_worker_cancelled(self._cancellation_event)
            _validate_provider_connection(self._provider)
            _check_data_worker_cancelled(self._cancellation_event)
            self.finished.emit()
        except _DataWorkerCancelled:
            pass
        except Exception as exc:
            if not self._cancellation_event.is_set():
                context = ErrorContext(f"Testing {self._provider_name}")
                self.error.emit(report_error(_LOGGER, exc, context))
        finally:
            self.completed.emit()


class _DataWorkerCancelled(Exception):
    pass


def _check_data_worker_cancelled(
    cancellation_event: threading.Event | None,
) -> None:
    if cancellation_event is not None and cancellation_event.is_set():
        raise _DataWorkerCancelled


class _MissingSnapshotError(UserFacingError, RuntimeError):
    pass


def _validate_provider_connection(provider: DataProvider) -> None:
    if provider.fetch_level1("SPY") is None:
        raise _MissingSnapshotError(
            "The provider returned no SPY market-data snapshot."
        )


def _resolve_company_name(
    cache: Cache,
    reference_aggregator: Aggregator,
    symbol: str,
    now: datetime,
    refresh_reference: bool,
    known_reference_name: str | None = None,
) -> str | None:
    reference = cache.get_asset_reference(symbol)
    if (
        reference is not None
        and reference.refreshed_at >= now - _ASSET_REFERENCE_MAX_AGE
    ):
        return reference.company_name
    if not refresh_reference:
        return (
            reference.company_name
            if reference is not None
            else _normalized_company_name(known_reference_name)
        )

    company_name = _normalized_company_name(known_reference_name)
    if company_name is None:
        company_name = _normalized_company_name(
            reference_aggregator.fetch_company_name(symbol)
        )
    if company_name is not None:
        cache.put_asset_reference(symbol, company_name, now)
        return company_name
    return None if reference is None else reference.company_name


def _asset_reference_refresh_due(
    cache: Cache,
    symbol: str,
    now: datetime,
    last_attempt: datetime | None,
) -> bool:
    reference = cache.get_asset_reference(symbol)
    if (
        reference is not None
        and reference.refreshed_at >= now - _ASSET_REFERENCE_MAX_AGE
    ):
        return False
    return (
        last_attempt is None
        or last_attempt <= now - _ASSET_REFERENCE_RETRY_DELAY
    )


def _normalized_company_name(name: str | None) -> str | None:
    if name is None:
        return None
    normalized = name.strip()
    return normalized or None


def _fetch_series_with_references(
    aggregator: Aggregator,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    now: datetime,
    *,
    history_start: datetime | None = None,
    cancellation_event: threading.Event | None = None,
) -> OHLCVSeries:
    _check_chart_load_cancelled(cancellation_event)
    if timeframe.is_intraday:
        _refresh_daily_references(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            now,
            cancellation_event,
        )
        _check_chart_load_cancelled(cancellation_event)
    return _fetch_series(
        aggregator,
        cache,
        cache_namespace,
        symbol,
        timeframe,
        now,
        history_start=history_start,
        cancellation_event=cancellation_event,
    )


def _refresh_daily_references(
    aggregator: Aggregator,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    now: datetime,
    cancellation_event: threading.Event | None,
) -> None:
    daily_end = _history_end(Timeframe.DAILY, aggregator, now)
    daily_start = _history_start(Timeframe.DAILY, aggregator, daily_end)
    _fetch_and_cache_bars(
        aggregator,
        cache,
        cache_namespace,
        symbol,
        Timeframe.DAILY,
        daily_start,
        daily_end,
        cancellation_event=cancellation_event,
    )


def _fetch_series_with_concurrent_references(
    aggregator: Aggregator,
    db_path: str,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    now: datetime,
    *,
    history_start: datetime | None = None,
    cancellation_event: threading.Event | None = None,
    on_references_done: Callable[[], None] | None = None,
) -> OHLCVSeries:
    """
    Like _fetch_series_with_references, but the daily reference refresh runs
    concurrently with the intraday fetch so the chart waits for the slower of
    the two instead of their sum.

    The reference thread owns its own Cache connection. Both branches finish
    before this returns or raises, so daily references are committed before
    any series is delivered. When both fail, the intraday error is raised;
    only one error ever leaves this function.
    """
    _check_chart_load_cancelled(cancellation_event)
    if not timeframe.is_intraday:
        return _fetch_series(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            timeframe,
            now,
            history_start=history_start,
            cancellation_event=cancellation_event,
        )

    def refresh_references() -> None:
        with Cache(db_path) as reference_cache:
            _refresh_daily_references(
                aggregator,
                reference_cache,
                cache_namespace,
                symbol,
                now,
                cancellation_event,
            )
        if on_references_done is not None:
            on_references_done()

    # Leaving the with-block waits for the reference thread on every path.
    with ThreadPoolExecutor(max_workers=1) as executor:
        references = executor.submit(
            contextvars.copy_context().run,
            refresh_references,
        )
        series = _fetch_series(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            timeframe,
            now,
            history_start=history_start,
            cancellation_event=cancellation_event,
        )
    references.result()
    _check_chart_load_cancelled(cancellation_event)
    return series


def _fetch_series(
    aggregator: Aggregator,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    now: datetime,
    *,
    history_start: datetime | None = None,
    cancellation_event: threading.Event | None = None,
    on_history_progress: Callable[[int], None] | None = None,
) -> OHLCVSeries:
    """
    Fetch the history window from history_start (by default the full history
    start) and return its bars. A window that starts at or before the full
    history start shows the contiguous cached history before it as well; a
    narrower window, such as the recent phase of a two-phase load, is shown
    as requested.
    """
    _check_chart_load_cancelled(cancellation_event)
    history_end = _history_end(timeframe, aggregator, now)
    full_start = _history_start(timeframe, aggregator, history_end)
    if history_start is None:
        history_start = full_start
    series: OHLCVSeries | None = None
    if aggregator.native_timeframe(timeframe) != timeframe:
        series = _fetch_synthesized_series(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            timeframe,
            history_start,
            history_end,
            cancellation_event=cancellation_event,
        )
    else:
        _fetch_and_cache_bars(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            timeframe,
            history_start,
            history_end,
            cancellation_event=cancellation_event,
            on_history_progress=on_history_progress,
        )
        _check_chart_load_cancelled(cancellation_event)
    display_start = history_start
    if history_start <= full_start:
        if aggregator.native_timeframe(timeframe) != timeframe:
            _materialize_older_cached_history(
                aggregator,
                cache,
                cache_namespace,
                symbol,
                timeframe,
                history_start,
                history_end,
            )
            _check_chart_load_cancelled(cancellation_event)
        display_start = _display_history_start(
            cache,
            cache_namespace,
            symbol,
            timeframe,
            history_start,
            history_end,
        )
    if series is not None and display_start == history_start:
        return series
    bars = cache.get_bars(
        cache_namespace,
        symbol,
        timeframe,
        int(display_start.timestamp() * 1000),
        int(history_end.timestamp() * 1000),
    )
    return OHLCVSeries(
        symbol=symbol,
        timeframe=timeframe,
        bars=bars,
    )


def _choose_synthesized_base_start(
    requested_start_ms: int,
    history_start: datetime,
    existing_first_timestamp: datetime | None,
    target_coverage_start_ms: int | None,
    target_coverage_is_legacy: bool,
    base_coverage_start: datetime | None,
    base_coverage_end: datetime | None,
    target_refresh_start: datetime | None,
) -> datetime | None:
    base_history_start = history_start
    target_covers_head = False
    if (
        existing_first_timestamp is not None
        and target_coverage_start_ms is not None
    ):
        target_covers_head = target_coverage_start_ms <= requested_start_ms
        if (
            target_covers_head
            and target_refresh_start is None
            and (base_coverage_start is None or not target_coverage_is_legacy)
        ):
            return None
        if target_covers_head and target_refresh_start is not None:
            base_history_start = max(history_start, target_refresh_start)

    preserved_target_covers_head = target_covers_head or (
        existing_first_timestamp is not None
        and existing_first_timestamp <= history_start
    )
    if (
        preserved_target_covers_head
        and base_coverage_start is not None
        and base_coverage_end is not None
    ):
        if target_refresh_start is None:
            base_history_start = max(history_start, base_coverage_start)
        elif base_history_start > base_coverage_end:
            base_history_start = max(history_start, base_coverage_end)
    return base_history_start


def _materialization_range(
    source_start_ms: int,
    source_end_ms: int,
    target_end_ms: int,
    target_coverage: tuple[int, int] | None,
    target_coverage_is_legacy: bool,
    has_existing_target: bool,
) -> _MaterializationRange:
    rebuild_start_ms = source_start_ms
    if target_coverage is not None and not target_coverage_is_legacy:
        target_start_ms, previous_end_ms = target_coverage
        if source_start_ms >= target_start_ms:
            rebuild_start_ms = min(previous_end_ms, target_end_ms)
    aligned_start_ms = max(
        source_start_ms,
        int(
            _exchange_day_start(
                ms_to_datetime(rebuild_start_ms)
            ).timestamp()
            * 1000
        ),
    )
    coverage_start_ms = min(
        aligned_start_ms,
        (
            target_coverage[0]
            if target_coverage is not None and has_existing_target
            else aligned_start_ms
        ),
    )
    return _MaterializationRange(
        aligned_start_ms,
        source_end_ms,
        coverage_start_ms,
        target_end_ms,
    )


def _fetch_synthesized_series(
    aggregator: Aggregator,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    history_start: datetime,
    history_end: datetime,
    *,
    cancellation_event: threading.Event | None = None,
) -> OHLCVSeries:
    _check_chart_load_cancelled(cancellation_event)
    requested_start_ms = int(history_start.timestamp() * 1000)
    requested_end_ms = int(history_end.timestamp() * 1000)
    existing = cache.get_bars(
        cache_namespace,
        symbol,
        timeframe,
        requested_start_ms,
        requested_end_ms,
    )
    target_coverage = cache.get_bar_fetch_coverage(
        cache_namespace,
        symbol,
        timeframe,
    )
    base_timeframe = aggregator.native_timeframe(timeframe)
    base_coverage = cache.get_bar_fetch_coverage(
        cache_namespace,
        symbol,
        base_timeframe,
    )
    target_refresh_start: datetime | None = None
    target_coverage_is_legacy = False
    if target_coverage is not None:
        target_coverage_is_legacy = _is_legacy_target_coverage(
            aggregator,
            timeframe,
            target_coverage,
        )
    if existing and target_coverage is not None:
        target_coverage_end = ms_to_datetime(target_coverage[1])
        target_refresh_start = _cached_tail_refresh_start(
            aggregator,
            symbol,
            timeframe,
            history_start,
            target_coverage_end,
            existing[-1].timestamp,
            history_end,
        )
    base_coverage_start = (
        ms_to_datetime(base_coverage[0])
        if base_coverage is not None
        else None
    )
    base_coverage_end = (
        ms_to_datetime(base_coverage[1])
        if base_coverage is not None
        else None
    )
    base_history_start = _choose_synthesized_base_start(
        requested_start_ms,
        history_start,
        existing[0].timestamp if existing else None,
        target_coverage[0] if target_coverage is not None else None,
        target_coverage_is_legacy,
        base_coverage_start,
        base_coverage_end,
        target_refresh_start,
    )
    if base_history_start is None:
        return OHLCVSeries(symbol, timeframe, existing)
    try:
        if _fetch_and_cache_bars(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            base_timeframe,
            base_history_start,
            history_end,
            cancellation_event=cancellation_event,
        ):
            # The base moved to a new adjustment basis, and this timeframe's
            # bars and coverage were reset with it: plan the load again from
            # the reset cache, which also fetches the base from history_start.
            return _fetch_synthesized_series(
                aggregator,
                cache,
                cache_namespace,
                symbol,
                timeframe,
                history_start,
                history_end,
                cancellation_event=cancellation_event,
            )
    except HistoryUnavailableError:
        if base_coverage is None:
            if not existing:
                raise
            return OHLCVSeries(symbol, timeframe, existing)

    base_coverage = cache.get_bar_fetch_coverage(
        cache_namespace,
        symbol,
        base_timeframe,
    )
    if base_coverage is None:
        return OHLCVSeries(symbol, timeframe, existing)
    source_start_ms = max(requested_start_ms, base_coverage[0])
    source_end_ms = min(requested_end_ms, base_coverage[1])
    if source_start_ms > source_end_ms:
        return OHLCVSeries(symbol, timeframe, existing)

    completed_target_end = aggregator.completed_history_end(
        timeframe,
        ms_to_datetime(source_end_ms),
    )
    target_end_ms = int(completed_target_end.timestamp() * 1000)
    materialization_range = _materialization_range(
        source_start_ms,
        source_end_ms,
        target_end_ms,
        target_coverage,
        target_coverage_is_legacy,
        bool(existing),
    )
    raw_bars = cache.get_raw_bars(
        cache_namespace,
        symbol,
        base_timeframe,
        materialization_range.range_start_ms,
        materialization_range.range_end_ms,
    )
    if _uses_regular_equity_session(symbol):
        raw_bars = [bar for bar in raw_bars if _is_regular_session_bar(bar)]
    _check_chart_load_cancelled(cancellation_event)
    materialized = aggregator.synthesize_cached_bars(raw_bars, timeframe)
    _check_chart_load_cancelled(cancellation_event)
    if not materialized:
        return OHLCVSeries(symbol, timeframe, existing)

    cache.replace_materialized_bars(
        cache_namespace,
        symbol,
        timeframe,
        materialization_range.range_start_ms,
        materialization_range.range_end_ms,
        materialized,
        materialization_range.coverage_start_ms,
        materialization_range.coverage_end_ms,
    )
    bars = cache.get_bars(
        cache_namespace,
        symbol,
        timeframe,
        requested_start_ms,
        requested_end_ms,
    )
    return OHLCVSeries(symbol, timeframe, bars)


def _materialize_older_cached_history(
    aggregator: Aggregator,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    fetch_start: datetime,
    end: datetime,
) -> None:
    """
    Build a synthesized timeframe's bars from the base timeframe's contiguous
    cached history older than the target's own coverage, so both show the
    same history depth. Local only; the rebuild ends at the first whole
    session of the target's coverage and the coverage start moves back to the
    base's displayed start.
    """
    target_coverage = cache.get_bar_fetch_coverage(
        cache_namespace,
        symbol,
        timeframe,
    )
    if target_coverage is None:
        return
    base_timeframe = aggregator.native_timeframe(timeframe)
    start_ms = int(
        _display_history_start(
            cache,
            cache_namespace,
            symbol,
            base_timeframe,
            fetch_start,
            end,
        ).timestamp()
        * 1000
    )
    if not target_coverage[0] <= int(fetch_start.timestamp() * 1000):
        return
    _materialize_synthesized_history(
        aggregator,
        cache,
        cache_namespace,
        symbol,
        timeframe,
        start_ms,
    )


def _materialize_synthesized_history(
    aggregator: Aggregator,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    start_ms: int,
) -> None:
    """
    Build a synthesized timeframe's bars from start_ms to the first whole
    session of its coverage from the base timeframe's cached raw bars, and
    move its coverage start back to start_ms. Local only.
    """
    target_coverage = cache.get_bar_fetch_coverage(
        cache_namespace,
        symbol,
        timeframe,
    )
    if target_coverage is None or start_ms >= target_coverage[0]:
        return
    base_timeframe = aggregator.native_timeframe(timeframe)
    rebuild_end = _first_session_open_at_or_after(
        ms_to_datetime(target_coverage[0])
    )
    end_ms = int(rebuild_end.timestamp() * 1000) - 1
    raw_bars = cache.get_raw_bars(
        cache_namespace,
        symbol,
        base_timeframe,
        start_ms,
        end_ms,
    )
    if _uses_regular_equity_session(symbol):
        raw_bars = [bar for bar in raw_bars if _is_regular_session_bar(bar)]
    materialized = aggregator.synthesize_cached_bars(raw_bars, timeframe)
    if not materialized:
        return
    cache.replace_materialized_bars(
        cache_namespace,
        symbol,
        timeframe,
        start_ms,
        end_ms,
        materialized,
        start_ms,
        target_coverage[1],
    )


def _is_legacy_target_coverage(
    aggregator: Aggregator,
    timeframe: Timeframe,
    target_coverage: tuple[int, int],
) -> bool:
    previous_end_ms = target_coverage[1]
    previous_end = ms_to_datetime(previous_end_ms)
    aligned_previous_end_ms = int(
        aggregator.completed_history_end(timeframe, previous_end).timestamp() * 1000
    )
    return aligned_previous_end_ms != previous_end_ms


def _is_regular_session_bar(bar: Bar) -> bool:
    return is_regular_session_timestamp(bar.timestamp)


def _fetch_and_cache_bars(
    aggregator: Aggregator,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    history_start: datetime,
    now: datetime,
    *,
    cancellation_event: threading.Event | None = None,
    on_history_progress: Callable[[int], None] | None = None,
) -> bool:
    """
    Fetch the timeframe's history from history_start to now into the cache.
    True when the provider moved the cached history to a new adjustment
    basis: the timeframe and those built from it were reset (see
    Cache.begin_basis_change) and the history was downloaded again. A paged
    timeframe raises _BasisReset after the reset instead, so the chart load
    starts over and renders recent bars first.
    """
    _check_chart_load_cancelled(cancellation_event)
    if aggregator.native_timeframe(timeframe) != timeframe:
        raise ValueError(
            f"Synthesized timeframe {timeframe.value} must use the cache path."
        )
    requested_start_ms = int(history_start.timestamp() * 1000)
    requested_end_ms = int(now.timestamp() * 1000)
    completed_end = _completed_coverage_end(
        symbol,
        timeframe,
        now,
        aggregator.completed_history_end(timeframe, now),
    )
    coverage = cache.get_bar_fetch_coverage(
        cache_namespace,
        symbol,
        timeframe,
    )
    if coverage is None:
        oldest_ts = cache.oldest_cached_timestamp(
            cache_namespace,
            symbol,
            timeframe,
        )
        newest_ts = cache.newest_cached_timestamp(
            cache_namespace,
            symbol,
            timeframe,
        )
        if oldest_ts is None or newest_ts is None:
            _fetch_bar_range(
                aggregator,
                cache,
                cache_namespace,
                symbol,
                timeframe,
                history_start,
                now,
                coverage_end=completed_end,
                cancellation_event=cancellation_event,
                on_history_progress=on_history_progress,
            )
            return False
        _fetch_bar_range(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            timeframe,
            history_start,
            now,
            coverage_end=completed_end,
            replace_coverage=True,
            cancellation_event=cancellation_event,
            on_history_progress=on_history_progress,
        )
        return False

    coverage_start_ms, coverage_end_ms = coverage
    coverage_end = ms_to_datetime(coverage_end_ms)
    normalized_coverage_end = aggregator.completed_history_end(
        timeframe,
        coverage_end,
    )
    normalized_coverage_end_ms = int(
        normalized_coverage_end.timestamp() * 1000
    )
    if normalized_coverage_end_ms != coverage_end_ms:
        cache.replace_bar_fetch_coverage(
            cache_namespace,
            symbol,
            timeframe,
            coverage_start_ms,
            normalized_coverage_end_ms,
        )
        coverage_end_ms = normalized_coverage_end_ms

    if coverage_end_ms < requested_start_ms:
        _fetch_bar_range(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            timeframe,
            history_start,
            now,
            coverage_end=completed_end,
            replace_coverage=True,
            cancellation_event=cancellation_event,
            on_history_progress=on_history_progress,
        )
        return False
    # The tail is refreshed before older history is fetched, so a changed
    # adjustment basis is detected before anything is written.
    if (
        requested_end_ms > coverage_end_ms
        or (
            timeframe == Timeframe.DAILY
            and _uses_regular_equity_session(symbol)
        )
    ):
        newest_ts = cache.newest_cached_timestamp(
            cache_namespace,
            symbol,
            timeframe,
        )
        newest_cached = (
            None
            if newest_ts is None
            else ms_to_datetime(newest_ts)
        )
        refresh_start = _cached_tail_refresh_start(
            aggregator,
            symbol,
            timeframe,
            history_start,
            ms_to_datetime(coverage_end_ms),
            newest_cached,
            now,
        )
        if refresh_start is not None and _refresh_cached_tail(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            timeframe,
            history_start,
            refresh_start,
            coverage_start_ms,
            coverage_end_ms,
            now,
            completed_end,
            cancellation_event,
        ):
            if aggregator.history_page_span(timeframe) is not None:
                # A paged re-download is long; the chart load starts over
                # so it renders the recent window first.
                raise _BasisReset
            _check_chart_load_cancelled(cancellation_event)
            redownload_start = history_start
            if aggregator.earliest_history_start(timeframe, now) is None:
                # One request: keep the whole cached depth.
                redownload_start = min(
                    history_start,
                    ms_to_datetime(coverage_start_ms),
                )
            _fetch_bar_range(
                aggregator,
                cache,
                cache_namespace,
                symbol,
                timeframe,
                redownload_start,
                now,
                coverage_end=completed_end,
                cancellation_event=cancellation_event,
                on_history_progress=on_history_progress,
            )
            return True
    if requested_start_ms < coverage_start_ms:
        _fetch_bar_range(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            timeframe,
            history_start,
            ms_to_datetime(coverage_start_ms),
            coverage_end=ms_to_datetime(coverage_start_ms),
            cancellation_event=cancellation_event,
            on_history_progress=on_history_progress,
        )
    return False


def _refresh_cached_tail(
    aggregator: Aggregator,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    history_start: datetime,
    refresh_start: datetime,
    coverage_start_ms: int,
    coverage_end_ms: int,
    now: datetime,
    completed_end: datetime,
    cancellation_event: threading.Event | None,
) -> bool:
    """
    Fetch the cached tail from refresh_start, together with the settled
    validation bars before it, in one request.

    When the provider returns the validation bars on a new adjustment basis,
    the timeframe's cached history from the provider window start is reset
    for a re-download and True is returned; nothing fetched is written.
    Otherwise the tail is written as before and False is returned.
    """
    _check_chart_load_cancelled(cancellation_event)
    validation_bars = _basis_validation_bars(
        cache,
        cache_namespace,
        symbol,
        timeframe,
        coverage_start_ms,
        coverage_end_ms,
    )
    # The provider window start; a pinned refresh reload may start earlier.
    # A synthesized load passes its base a later history start, so the
    # window, not history_start, bounds the validation bars requested:
    # the provider may refuse a range reaching before it.
    window_start = min(history_start, _history_start(timeframe, aggregator, now))
    request_start = (
        min(refresh_start, max(validation_bars[0].timestamp, window_start))
        if validation_bars
        else refresh_start
    )
    try:
        fetched = aggregator.fetch_bars(symbol, timeframe, request_start, now)
    except HistoryUnavailableError:
        return False
    change = detect_basis_change(validation_bars, fetched)
    if change is not None:
        _reset_for_basis_change(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            timeframe,
            change,
            window_start,
            now,
        )
        return True
    tail = [bar for bar in fetched if bar.timestamp >= refresh_start]
    if not tail:
        return False
    cache.put_bars(cache_namespace, symbol, timeframe, tail)
    cache.extend_bar_fetch_coverage(
        cache_namespace,
        symbol,
        timeframe,
        int(refresh_start.timestamp() * 1000),
        int(completed_end.timestamp() * 1000),
    )
    return False


def _reset_for_basis_change(
    aggregator: Aggregator,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    change: BasisChange,
    window_start: datetime,
    now: datetime,
) -> None:
    """
    Reset a native timeframe's cache (and its dependents') for a new
    adjustment basis; the next fetch downloads it again from window_start.
    """
    if aggregator.earliest_history_start(timeframe, now) is None:
        # The provider still returns all of it: reset everything and download
        # it again (on demand beyond the initial window).
        boundary_ms = _OLDEST_TIMESTAMP_MS
        shown_from_ms = _OLDEST_TIMESTAMP_MS
    else:
        # Older history the provider no longer returns is kept and recorded
        # for the user to rescale or discard.
        boundary_ms = int(window_start.timestamp() * 1000)
        shown_from_ms = int(
            _first_session_open_at_or_after(
                now - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
            ).timestamp()
            * 1000
        )
    cache.begin_basis_change(
        cache_namespace,
        symbol,
        timeframe,
        _dependent_timeframes(aggregator, timeframe),
        change,
        boundary_ms,
        shown_from_ms,
    )


def _basis_validation_bars(
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    coverage_start_ms: int,
    coverage_end_ms: int,
) -> list[Bar]:
    """
    The settled cached bars compared against the provider for a basis
    change: intraday, the last two cached sessions before the coverage end
    (the previous full session and the settled part of the last one); daily,
    the last ten bars; weekly, the last four. Bars at or after the coverage
    end may be partial and are excluded.
    """
    lookback = (
        _BASIS_VALIDATION_INTRADAY_LOOKBACK
        if timeframe.is_intraday
        else _BASIS_VALIDATION_LOOKBACK[timeframe]
    )
    bars = cache.get_raw_bars(
        cache_namespace,
        symbol,
        timeframe,
        max(coverage_start_ms, coverage_end_ms - int(lookback.total_seconds() * 1000)),
        coverage_end_ms - 1,
    )
    if not timeframe.is_intraday:
        return bars[-_BASIS_VALIDATION_BARS[timeframe]:]
    sessions = sorted({_exchange_day_start(bar.timestamp) for bar in bars})
    if not sessions:
        return []
    first_session = sessions[-_BASIS_VALIDATION_SESSIONS:][0]
    return [bar for bar in bars if bar.timestamp >= first_session]


def _basis_change_message(symbol: str, record: BasisChangeRecord) -> str:
    if record.volume_factor != 1.0:
        split = Fraction(record.volume_factor).limit_denominator(100)
        event = f"a {split.numerator}-for-{split.denominator} split"
    else:
        event = "dividends"
    return (
        f"{symbol}'s price history was adjusted by its data source for "
        f"{event} (older prices \u00d7 {record.ratio:.4g}). Cached history "
        "older than the source still provides is on the old basis. Rescale "
        "it to match (keeps it), or discard it?"
    )


def _dependent_timeframes(
    aggregator: Aggregator,
    timeframe: Timeframe,
) -> list[Timeframe]:
    """
    Timeframes whose cached bars are built from, or summarize the same
    sessions as, timeframe and go stale with it: synthesized timeframes for
    their base, and weekly for daily.
    """
    if timeframe == Timeframe.DAILY:
        return [Timeframe.WEEKLY]
    dependents: list[Timeframe] = []
    for candidate in _CHART_TIMEFRAMES:
        try:
            native = aggregator.native_timeframe(candidate)
        except UnsupportedTimeframeError:
            continue
        if candidate != timeframe and native == timeframe:
            dependents.append(candidate)
    return dependents


def _fetch_bar_range(
    aggregator: Aggregator,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    *,
    coverage_end: datetime | None = None,
    replace_coverage: bool = False,
    require_bars: bool = False,
    cancellation_event: threading.Event | None = None,
    on_history_progress: Callable[[int], None] | None = None,
) -> None:
    _check_chart_load_cancelled(cancellation_event)
    page_span = aggregator.history_page_span(timeframe)
    if not require_bars and page_span is not None and end - start > page_span:
        _fetch_bar_range_chunked(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            timeframe,
            _history_chunks(start, end, page_span),
            int((end if coverage_end is None else coverage_end).timestamp() * 1000),
            replace_coverage,
            cancellation_event,
            on_history_progress,
        )
        return
    new_bars = aggregator.fetch_bars(symbol, timeframe, start, end)
    if new_bars:
        cache.put_bars(cache_namespace, symbol, timeframe, new_bars)
    elif require_bars:
        return
    coverage_writer = (
        cache.replace_bar_fetch_coverage
        if replace_coverage
        else cache.extend_bar_fetch_coverage
    )
    coverage_writer(
        cache_namespace,
        symbol,
        timeframe,
        int(start.timestamp() * 1000),
        int((end if coverage_end is None else coverage_end).timestamp() * 1000),
    )


def _history_chunks(
    start: datetime,
    end: datetime,
    span: timedelta,
) -> list[tuple[datetime, datetime]]:
    """Split [start, end] into page-span chunks, newest first."""
    chunks: list[tuple[datetime, datetime]] = []
    chunk_end = end
    while chunk_end > start:
        chunk_start = max(start, chunk_end - span)
        chunks.append((chunk_start, chunk_end))
        chunk_end = chunk_start
    return chunks


def _fetch_bar_range_chunked(
    aggregator: Aggregator,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    chunks: list[tuple[datetime, datetime]],
    coverage_end_ms: int,
    replace_coverage: bool,
    cancellation_event: threading.Event | None,
    on_progress: Callable[[int], None] | None = None,
) -> None:
    """
    Fetch newest-first chunks concurrently and commit them in order.

    Pool threads only call the provider; this thread owns the cache. Coverage
    grows only over the contiguous run of completed chunks that ends at the
    range end, so it never claims an unfetched gap. On failure or cancellation
    the run completed so far stays committed and the next load resumes from it.
    on_progress, when given, receives the start (UTC ms) of the contiguous
    committed run after each commit that extends it.
    """
    results: dict[int, list[Bar]] = {}
    in_flight: dict[Future[list[Bar]], int] = {}
    committed = 0
    next_chunk = 0
    replace_next = replace_coverage
    failure: Exception | None = None

    def commit_contiguous() -> None:
        nonlocal committed, replace_next
        while committed in results:
            bars = results.pop(committed)
            if bars:
                cache.put_bars(cache_namespace, symbol, timeframe, bars)
            coverage_writer = (
                cache.replace_bar_fetch_coverage
                if replace_next
                else cache.extend_bar_fetch_coverage
            )
            chunk_start_ms = int(chunks[committed][0].timestamp() * 1000)
            coverage_writer(
                cache_namespace,
                symbol,
                timeframe,
                chunk_start_ms,
                coverage_end_ms,
            )
            replace_next = False
            committed += 1
            if on_progress is not None:
                on_progress(chunk_start_ms)

    def is_cancelled() -> bool:
        return cancellation_event is not None and cancellation_event.is_set()

    executor = ThreadPoolExecutor(max_workers=_HISTORY_FETCH_WORKERS)
    try:
        while committed < len(chunks) and not is_cancelled():
            while (
                next_chunk < len(chunks)
                and next_chunk - committed < _HISTORY_FETCH_WORKERS
            ):
                chunk_start, chunk_end = chunks[next_chunk]
                # Each task runs in a copy of this context so the caller's
                # request priority (foreground or background) applies to it.
                future = executor.submit(
                    contextvars.copy_context().run,
                    aggregator.fetch_bars,
                    symbol,
                    timeframe,
                    chunk_start,
                    chunk_end,
                )
                in_flight[future] = next_chunk
                next_chunk += 1
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                index = in_flight.pop(future)
                try:
                    results[index] = future.result()
                except Exception as exc:
                    failure = exc
            if failure is not None:
                break
            commit_contiguous()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    for future, index in in_flight.items():
        if not future.cancelled() and future.exception() is None:
            results[index] = future.result()
    commit_contiguous()
    if failure is not None and not (
        isinstance(failure, RequestCancelledError) and is_cancelled()
    ):
        raise failure
    _check_chart_load_cancelled(cancellation_event)


class MainWindow(QMainWindow):
    """
    The application's main window.

    Layout:
        ┌─────────────────────────────────┐
        │  MainWindowTitleBar (Wayland)   │
        ├─────────────────────────────────┤
        │  SymbolBar (symbol + timeframe) │
        ├─────────────────────────────────┤
        │  ChartWidget (chart + legend)   │
        └─────────────────────────────────┘
    """

    # (cache namespace, symbol) after a symbol refresh rewinds its cache.
    symbol_rewound: pyqtSignal = pyqtSignal(str, str)
    # (cache namespace, symbol) each time its refresh status changes.
    symbol_refresh_status_changed: pyqtSignal = pyqtSignal(str, str)

    def __init__(
        self,
        db_path: str,
        provider_connection_id: str | None = None,
    ) -> None:
        super().__init__()
        self._uses_custom_window_chrome = is_wayland_platform(
            QGuiApplication.platformName()
        )
        if self._uses_custom_window_chrome:
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle("Simple Chart")
        self.resize(1400, 800)

        # ------------------------------------------------------------------
        # Data layer
        # ------------------------------------------------------------------
        self._db_path = db_path
        self._cache = Cache(db_path)
        self._load_timing = LoadTimingLog.from_environment(
            Path(db_path).parent / "diagnostics",
            os.environ,
        )
        connection_id = (
            provider_connection_id
            if provider_connection_id is not None
            else self._cache.get_active_provider_connection_id()
        )
        connection = self._cache.get_provider_connection(connection_id)
        if connection is None:
            raise ValueError(f"Unknown provider connection: {connection_id}")
        self._credential_access = initialize_keyring_credential_store()
        self._credential_store = self._credential_access.store
        self._provider_availability = _provider_availability_by_name(
            self._cache.get_provider_connections(),
            self._credential_access,
        )
        startup_routes = _build_startup_routes(
            self._cache,
            connection.connection_id,
            self._credential_store,
            self._provider_availability,
        )
        self._selected_route = startup_routes.selected
        self._yahoo_route = startup_routes.yahoo
        self._startup_provider_fallback_reason = startup_routes.fallback_reason

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
            self._selected_route.cache_namespace,
        )
        self._active_drawing_tool: str | None = None
        self._drawing_session: DrawingSession | None = None
        self._preview_keys: set[str] = set()
        # Extensions turned off since the last cleanup pass.
        self._turned_off_extensions: set[str] = set()
        self._extension_cleanup_scheduled = False
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
        frame_border_width = 3 if self._uses_custom_window_chrome else 5
        frame.setStyleSheet(
            "QFrame#appFrame {"
            " background: #ffffff;"
            f" border: {frame_border_width}px solid #9d978d;"
            "}"
        )

        # Top-level layout: app header above the watchlist/chart workspace.
        root_layout = QVBoxLayout(frame)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self._window_title_bar: MainWindowTitleBar | None = None
        self._window_resize_controller: WindowResizeController | None = None
        if self._uses_custom_window_chrome:
            self._window_title_bar = MainWindowTitleBar(self, frame)
            root_layout.addWidget(self._window_title_bar)
            self._window_resize_controller = WindowResizeController(
                frame,
                frame_border_width,
            )

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
        self._chart.older_history_wanted.connect(self._on_older_history_wanted)

        # Wire symbol bar signals.
        self._symbol_bar.symbol_changed.connect(self._on_symbol_changed)
        self._symbol_bar.timeframe_changed.connect(self._on_timeframe_changed)
        self._symbol_bar.settings_requested.connect(self._on_application_settings)

        # Chart-load tasks are retained until their QThreads finish.
        self._chart_load_generation = 0
        self._current_chart_request: _ChartLoadRequest | None = None
        self._terminal_chart_generation: int | None = None
        self._pending_chart_load: _ChartLoadRequest | None = None
        self._chart_load_tasks: dict[int, _ChartLoadTask] = {}
        # Refreshes waiting for their symbol's chart loads to finish, by
        # (cache namespace, symbol). No load for that symbol dispatches
        # until its cache is rewound.
        self._symbol_refreshes: dict[tuple[str, str], _SymbolRefresh] = {}
        # Latest refresh status line by (cache namespace, symbol).
        self._symbol_refresh_statuses: dict[
            tuple[str, str], SymbolRefreshStatus
        ] = {}
        self._refresh_reload: _RefreshReload | None = None
        # Answers to the basis-change question this session, by (cache
        # namespace, symbol): (ratio of the event, choice or None when
        # dismissed).
        self._basis_change_answers: dict[
            tuple[str, str],
            list[tuple[float, BasisChangeChoice | None]],
        ] = {}
        # Request whose recent window is displayed while its older history
        # loads; None once the displayed series covers the full window.
        self._partial_history_request: _ChartLoadRequest | None = None
        # Whether the series on screen covers the full history window. Changes
        # only when new bars are drawn, so a pending replacement load cannot
        # make a displayed recent window look complete.
        self._displayed_history_complete = True
        # Full-history result that arrived during a drag or drawing session.
        self._held_history_merge: tuple[_ChartLoadRequest, OHLCVSeries] | None = None
        # An older-history result held while a drag or drawing session runs.
        self._held_older_merge: tuple[_ChartLoadRequest, OHLCVSeries] | None = None
        # The displayed request whose older history is exhausted or failed.
        self._older_history_stopped: _ChartLoadRequest | None = None
        # Interim re-render while backfill unlocks deferred extension content:
        # the start (UTC ms) of the latest committed contiguous history, a
        # timer spacing re-renders, and whether one waits for an interaction.
        self._history_progress_ms: int | None = None
        self._last_history_refresh = 0.0
        self._history_refresh_deferred = False
        self._history_refresh_timer = QTimer(self)
        self._history_refresh_timer.setSingleShot(True)
        self._history_refresh_timer.timeout.connect(self._refresh_partial_history)
        self._quote_refreshed_generation: int | None = None
        self._chart_load_timer = QTimer(self)
        self._chart_load_timer.setSingleShot(True)
        self._chart_load_timer.setInterval(_CHART_LOAD_DEBOUNCE_MS)
        self._chart_load_timer.timeout.connect(self._dispatch_pending_chart_load)

        self._snapshot_thread: QThread | None = None
        self._snapshot_worker: _SnapshotWorker | None = None
        self._snapshot_request: _SnapshotRequest | None = None
        self._snapshot_refresh_pending = False
        self._watchlist_membership_revision = 0
        self._level1_thread: QThread | None = None
        self._level1_worker: _Level1Worker | None = None
        self._level1_request: _Level1Request | None = None
        self._level1_refresh_pending = False
        self._level1_symbol: str | None = None
        self._level1_symbol_generation = 0
        self._quote_route_generation = 0
        self._stopping_data_workers = False
        self._closing = False
        self._close_finalized = False
        self._close_finalize_scheduled = False
        self._snapshot_cancellation_event: threading.Event | None = None
        self._level1_cancellation_event: threading.Event | None = None
        self._validation_cancellation_event: threading.Event | None = None
        self._asset_reference_refresh_attempts: dict[str, datetime] = {}
        self._validation_thread: QThread | None = None
        self._validation_worker: _ProviderValidationWorker | None = None
        self._pending_route: _DataRoute | None = None
        self._snapshot_timer = QTimer(self)
        self._snapshot_timer.setInterval(_SNAPSHOT_REFRESH_MS)
        self._snapshot_timer.timeout.connect(self._refresh_watchlist_snapshots)
        self._snapshot_timer.timeout.connect(self._refresh_level1)

        # Most recently loaded series — used to convert bar index to timestamp
        # when the user clicks a bar (finplot's x-axis is indexed, not time-based).
        self._current_series: OHLCVSeries | None = None
        # True from _load() until its series is displayed. _current_series
        # still holds the previous load's bars then, so chart input is ignored.
        self._chart_load_pending = False
        self._bar_correction_notice: str | None = None
        # Status-bar text from extension renders (ChartExtensionRender.notice).
        self._extension_notice: str | None = None
        self._history_status_message: str | None = None

        # The symbol for which self._state.extensions currently holds state.
        # Used to save/restore per-symbol extension state on symbol switch.
        self._loaded_symbol: str | None = None
        self._per_symbol_state: dict[str, list[ChartExtensionState]] = {}

        # Extension failures are cleaned up here and announced once the
        # window is up, along with plugins skipped at load.
        set_disable_handler(self._on_extensions_turned_off)
        self._schedule_extension_cleanup()

        # Load the initial symbol on startup: first watchlist entry, or SPY.
        watchlist = self._cache.get_watchlist()
        initial_symbol = watchlist[0] if watchlist else "SPY"
        self._state.symbol = initial_symbol
        self._symbol_bar.set_symbol(initial_symbol)
        if self._startup_provider_fallback_reason is not None:
            status_bar = self.statusBar()
            if status_bar is not None:
                status_bar.showMessage(
                    f"{self._startup_provider_fallback_reason} Using Yahoo "
                    "Finance for this session.",
                )
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

    def _route_for_symbol(self, symbol: str) -> _DataRoute:
        return _route_for_symbol(symbol, self._selected_route, self._yahoo_route)

    def _load(self, history_start: datetime | None = None) -> None:
        """
        Kick off a background fetch for the current symbol and timeframe.
        Clears the chart immediately so the user sees a clean slate while
        loading, rather than stale data from the previous symbol.
        history_start pins the reload after a symbol refresh to the start
        the refresh rewound to.
        """
        if self._stopping_data_workers or self._state.symbol is None:
            return
        requested_at = time.monotonic()

        self._end_interaction_sessions()
        self._chart_load_pending = True
        self._chart.clear_all()
        self._chart.legend.clear_all()
        self._app_header.set_symbol(self._state.symbol)
        quote_refreshed = self._level1_symbol != self._state.symbol
        if quote_refreshed:
            self._refresh_level1()
        route = self._route_for_symbol(self._state.symbol)
        self._symbol_bar.set_data_source(route.display_name, "pending")

        request = self._make_chart_load_request(
            self._state.symbol,
            self._state.timeframe,
            route.connection,
            requested_at,
            history_start,
        )
        for task in self._chart_load_tasks.values():
            task.cancellation_event.set()
        self._record_load_step(request, "load requested")
        # A symbol change already requested a fresh quote; the chart render
        # need not request another one for this load.
        self._quote_refreshed_generation = (
            request.generation if quote_refreshed else None
        )
        # A newer load replaced the refresh reload.
        self._drop_refresh_reload()
        self._current_chart_request = request
        self._terminal_chart_generation = None
        self._partial_history_request = None
        self._held_history_merge = None
        self._held_older_merge = None
        self._extension_runtime.set_older_history_may_arrive(True)
        self._reset_history_refresh()
        self._clear_history_status_message()
        self._pending_chart_load = request
        self._chart_load_timer.start()

    def _make_chart_load_request(
        self,
        symbol: str,
        timeframe: Timeframe,
        connection: ProviderConnection,
        requested_at: float = 0.0,
        history_start: datetime | None = None,
    ) -> _ChartLoadRequest:
        self._chart_load_generation += 1
        return _ChartLoadRequest(
            generation=self._chart_load_generation,
            symbol=symbol,
            timeframe=timeframe,
            connection=connection,
            requested_at=requested_at or time.monotonic(),
            lookback_days=self._extension_lookback_days(symbol),
            history_start=history_start,
        )

    def _extension_lookback_days(self, symbol: str) -> int:
        """
        Longest history lookback among the extensions the symbol's chart will
        show, mirroring the extension set _on_fetch_done restores for it.
        """
        if symbol == self._loaded_symbol:
            entries = [(s.name, s.params) for s in self._state.extensions]
        elif symbol in self._per_symbol_state:
            entries = [(s.name, s.params) for s in self._per_symbol_state[symbol]]
        else:
            entries = list(INITIAL_EXTENSIONS)
        lookbacks: list[int | None] = []
        for name, params in entries:
            extension = guarded_extension(name)
            if extension is not None:
                lookbacks.append(
                    call_extension(name, extension.history_lookback_days, params)
                )
        return max(
            (days for days in lookbacks if days is not None),
            default=0,
        )

    def _dispatch_pending_chart_load(self) -> None:
        request = self._pending_chart_load
        if (
            self._stopping_data_workers
            or request is None
            or len(self._chart_load_tasks) >= _MAX_ACTIVE_CHART_LOADS
            or (request.cache_namespace, request.symbol) in self._symbol_refreshes
            # A cancelled older-history load may still commit a chunk; the
            # symbol's next load waits for it so writers never overlap.
            or any(
                task.older
                and (task.request.cache_namespace, task.request.symbol)
                == (request.cache_namespace, request.symbol)
                for task in self._chart_load_tasks.values()
            )
        ):
            return
        self._pending_chart_load = None
        self._start_chart_load(request)

    def _start_chart_load(self, request: _ChartLoadRequest) -> None:
        if self._stopping_data_workers:
            return
        route = self._route_for_symbol(request.symbol)
        if route.connection != request.connection:
            if (
                self._current_chart_request == request
                and self._terminal_chart_generation != request.generation
                and self._state.symbol == request.symbol
                and self._state.timeframe == request.timeframe
            ):
                self._terminal_chart_generation = request.generation
                self._end_refresh_reload(request, True)
                self._on_fetch_error(
                    "The data source changed before the chart load started."
                )
            return

        self._record_load_step(request, "dispatched")
        cancellation_event = threading.Event()
        worker = _FetchWorker(
            db_path=self._db_path,
            credential_store=self._credential_store,
            request=request,
            cancellation_event=cancellation_event,
            load_timing=self._load_timing,
        )
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.recent_ready.connect(self._on_chart_recent_ready)
        worker.history_progress.connect(self._on_chart_history_progress)
        worker.finished.connect(self._on_chart_fetch_done)
        worker.error.connect(self._on_chart_fetch_error)
        worker.completed.connect(thread.quit)
        worker.completed.connect(worker.deleteLater)
        thread.finished.connect(
            lambda generation=request.generation: self._on_chart_thread_finished(
                generation
            )
        )

        self._chart_load_tasks[request.generation] = _ChartLoadTask(
            request,
            thread,
            worker,
            cancellation_event,
        )
        thread.start()

    def _on_chart_thread_finished(self, generation: int) -> None:
        task = self._chart_load_tasks.pop(generation, None)
        if task is None:
            return
        task.thread.deleteLater()
        self._rewind_drained_symbols()
        if not self._stopping_data_workers and not self._chart_load_timer.isActive():
            self._dispatch_pending_chart_load()
        self._maybe_finalize_close()
        QTimer.singleShot(0, self._check_older_history)

    def connect_symbol_rewound(self, slot: Callable[[str, str], None]) -> None:
        self.symbol_rewound.connect(slot)

    def symbol_refresh_status(
        self,
        cache_namespace: str,
        symbol: str,
    ) -> SymbolRefreshStatus | None:
        return self._symbol_refresh_statuses.get((cache_namespace, symbol))

    def connect_symbol_refresh_status(
        self,
        slot: Callable[[str, str], None],
    ) -> None:
        self.symbol_refresh_status_changed.connect(slot)

    def _set_symbol_refresh_status(
        self,
        cache_namespace: str,
        symbol: str,
        text: str,
        state: SymbolRefreshState = "done",
    ) -> None:
        self._symbol_refresh_statuses[(cache_namespace, symbol)] = (
            SymbolRefreshStatus(state, text)
        )
        self.symbol_refresh_status_changed.emit(cache_namespace, symbol)

    def _end_refresh_reload(
        self,
        request: _ChartLoadRequest,
        failed: bool,
    ) -> None:
        """
        Report the end of a terminal request if it is the refresh reload.
        A failure never shows the error text, which can echo credentials.
        """
        reload = self._refresh_reload
        if reload is None or reload.request.generation != request.generation:
            return
        self._refresh_reload = None
        if failed:
            text = (
                f"{request.symbol} refresh stopped: the download failed. "
                "The missing data downloads on the next load."
            )
        else:
            text = (
                f"{request.symbol} refreshed at "
                f"{_clock_text(datetime.now(tz=timezone.utc))}. "
                "Other timeframes update when you view them."
            )
        self._set_symbol_refresh_status(
            request.cache_namespace,
            request.symbol,
            text,
            "failed" if failed else "done",
        )

    def _drop_refresh_reload(self) -> None:
        """
        End an unfinished refresh reload that will not complete; the
        rewound data downloads when the symbol is next shown.
        """
        reload = self._refresh_reload
        if reload is None:
            return
        self._refresh_reload = None
        self._set_symbol_refresh_status(
            reload.request.cache_namespace,
            reload.request.symbol,
            _refreshed_off_screen_text(reload.request.symbol, reload.rewound_at),
        )

    def plan_symbol_refresh(
        self,
        cache_namespace: str,
        symbol: str,
    ) -> SymbolRefreshPlan | None:
        """
        What refreshing the symbol's cached data from cache_namespace's
        source would replace, as of now. None when no route in this session
        serves that namespace. The rewind computes its own boundaries when it
        runs.
        """
        route = next(
            (
                route
                for route in (self._selected_route, self._yahoo_route)
                if route.cache_namespace == cache_namespace
            ),
            None,
        )
        if route is None:
            return None
        now = datetime.now(tz=timezone.utc)
        history_starts = _refresh_history_starts(route.aggregator, now)
        intraday_days = max(
            (now - start).days
            for timeframe, start in history_starts.items()
            if timeframe.is_intraday
        )

        hides_older_intraday = any(
            route.aggregator.earliest_history_start(timeframe, now) is not None
            for timeframe in history_starts
            if timeframe.is_intraday
        )

        def refresh() -> None:
            self._refresh_symbol(
                _SymbolRefresh(cache_namespace, symbol, route.aggregator)
            )

        return SymbolRefreshPlan(
            intraday_days,
            history_starts[Timeframe.DAILY].date(),
            hides_older_intraday,
            refresh,
        )

    def _refresh_symbol(self, refresh: _SymbolRefresh) -> None:
        """
        Hold new chart loads for the symbol, cancel its running ones, and
        rewind its cache once their threads have finished.
        """
        key = (refresh.cache_namespace, refresh.symbol)
        self._symbol_refreshes[key] = refresh
        for task in self._chart_load_tasks.values():
            if (task.request.cache_namespace, task.request.symbol) == key:
                task.cancellation_event.set()
        self._rewind_drained_symbols()
        if key in self._symbol_refreshes:
            self._set_symbol_refresh_status(
                *key,
                "Waiting for the current chart load to finish…",
                state="running",
            )

    def _rewind_drained_symbols(self) -> None:
        """
        Rewind each refreshing symbol that no chart load is still writing,
        then reload it if it is on screen. The reload fetches from the
        history start the rewind used, so a session the rewind deleted is
        never skipped because the clock moved on.
        """
        loading = {
            (task.request.cache_namespace, task.request.symbol)
            for task in self._chart_load_tasks.values()
        }
        for key, refresh in tuple(self._symbol_refreshes.items()):
            if key in loading:
                continue
            del self._symbol_refreshes[key]
            now = datetime.now(tz=timezone.utc)
            history_starts = _refresh_history_starts(refresh.aggregator, now)
            # Coverage of every rewound timeframe, native and synthesized, is
            # removed: the reload is a cold load from a session start.
            try:
                self._cache.rewind_symbol_history(
                    refresh.cache_namespace,
                    refresh.symbol,
                    _rewind_boundaries(history_starts),
                    (),
                )
            except sqlite3.Error:
                # The rewind is one transaction, so the cache is unchanged
                # and loads for the symbol may continue.
                self._set_symbol_refresh_status(
                    *key,
                    f"{refresh.symbol} refresh stopped: the cached data could "
                    "not be cleared. Try again.",
                    state="failed",
                )
                continue
            self.symbol_rewound.emit(refresh.cache_namespace, refresh.symbol)
            if (
                not self._stopping_data_workers
                and self._state.symbol == refresh.symbol
                and self._route_for_symbol(refresh.symbol).cache_namespace
                == refresh.cache_namespace
            ):
                self._load(history_starts[self._state.timeframe])
                request = self._current_chart_request
                assert request is not None
                self._refresh_reload = _RefreshReload(request, now)
                self._set_symbol_refresh_status(
                    *key,
                    f"Refreshing {refresh.symbol}…",
                    state="running",
                )
            else:
                self._set_symbol_refresh_status(
                    *key,
                    _refreshed_off_screen_text(refresh.symbol, now),
                )

    def _is_current_chart_request(self, request: _ChartLoadRequest) -> bool:
        if (
            self._stopping_data_workers
            or self._current_chart_request != request
            or self._terminal_chart_generation == request.generation
            or self._state.symbol != request.symbol
            or self._state.timeframe != request.timeframe
        ):
            return False
        return self._route_for_symbol(request.symbol).connection == request.connection

    def _on_chart_recent_ready(
        self,
        request: _ChartLoadRequest,
        series: OHLCVSeries,
    ) -> None:
        """
        Render the recent window while older history loads. Not terminal:
        the same request later delivers the full window or an error.
        """
        if not self._is_current_chart_request(request):
            return
        self._record_load_step(request, "recent delivered")
        self._partial_history_request = request
        self._on_fetch_done(series, history_complete=False)
        self._record_render_steps(request)

    def _on_chart_history_progress(
        self,
        request: _ChartLoadRequest,
        earliest_ms: int,
    ) -> None:
        """
        Older history committed back to earliest_ms. Re-render the displayed
        interim window only when that reaches content still waiting on it.
        """
        if (
            request != self._partial_history_request
            or not self._is_current_chart_request(request)
        ):
            return
        self._history_progress_ms = earliest_ms
        pending_ms = self._extension_runtime.pending_history_start_ms
        if pending_ms is not None and earliest_ms <= pending_ms:
            self._schedule_history_refresh()

    def _schedule_history_refresh(self) -> None:
        """Run one interim re-render, at most once per refresh interval."""
        if self._history_refresh_timer.isActive():
            return
        elapsed_ms = (time.monotonic() - self._last_history_refresh) * 1000
        self._history_refresh_timer.start(
            max(0, int(_HISTORY_REFRESH_INTERVAL_MS - elapsed_ms))
        )

    def _reset_history_refresh(self) -> None:
        self._history_refresh_timer.stop()
        self._history_progress_ms = None
        self._history_refresh_deferred = False

    def _refresh_partial_history(self) -> None:
        """
        Redraw the displayed interim window extended back to the committed
        history, so extensions whose history has arrived appear before the
        full window completes.
        """
        request = self._partial_history_request
        start_ms = self._history_progress_ms
        displayed = self._current_series
        if (
            request is None
            or start_ms is None
            or displayed is None
            or not displayed.bars
            or self._stopping_data_workers
            or not self._is_current_chart_request(request)
        ):
            return
        if self._drawing_session is not None or self._extension_runtime.drag_active:
            # Drag and drawing sessions resolve bar indexes against the
            # displayed series; retry when the interaction ends.
            self._history_refresh_deferred = True
            return
        bars = self._cache.get_bars(
            request.cache_namespace,
            request.symbol,
            request.timeframe,
            start_ms,
            int(displayed.bars[-1].timestamp.timestamp() * 1000),
        )
        if len(bars) <= len(displayed.bars):
            return
        self._last_history_refresh = time.monotonic()
        self._apply_history_merge(
            OHLCVSeries(request.symbol, request.timeframe, bars),
            history_complete=False,
        )
        self._record_load_step(request, "interim history merged")

    def _on_chart_fetch_done(
        self,
        request: _ChartLoadRequest,
        series: OHLCVSeries,
    ) -> None:
        if not self._is_current_chart_request(request):
            return
        self._record_load_step(request, "history delivered")
        self._terminal_chart_generation = request.generation
        self._end_refresh_reload(request, False)
        if self._partial_history_request != request:
            self._on_fetch_done(series)
            self._record_render_steps(request)
            return
        self._held_history_merge = (request, series)
        self._flush_held_history_merge()

    def _on_chart_fetch_error(
        self,
        request: _ChartLoadRequest,
        message: str,
    ) -> None:
        if not self._is_current_chart_request(request):
            return
        self._record_load_step(request, "error delivered")
        self._terminal_chart_generation = request.generation
        self._end_refresh_reload(request, True)
        if self._partial_history_request != request:
            self._on_fetch_error(message)
            return
        # The recent chart stays usable, so report without a dialog. Indicators
        # that need full history stay hidden; the next load resumes the fetch.
        route = self._route_for_symbol(request.symbol)
        self._symbol_bar.set_data_source(route.display_name, "error")
        status_bar = self.statusBar()
        if status_bar is not None:
            self._history_status_message = (
                f"Older history could not be loaded: {message} "
                "It will be retried on the next load."
            )
            status_bar.showMessage(self._history_status_message)

    def _flush_held_history_merge(self) -> None:
        """
        Apply a held full-history result once no drag or drawing session is
        active, if its request is still the displayed one. Drag and drawing
        sessions resolve bar indexes against the current series, so the series
        must not change under them.
        """
        interacting = (
            self._drawing_session is not None
            or self._extension_runtime.drag_active
        )
        # An interim re-render skipped during the interaction runs now.
        if self._history_refresh_deferred and not interacting:
            self._history_refresh_deferred = False
            self._schedule_history_refresh()
        held_older = self._held_older_merge
        if held_older is not None and not interacting:
            self._held_older_merge = None
            if self._older_result_applies(held_older[0]):
                self._apply_history_merge(held_older[1])
        held = self._held_history_merge
        if held is None or interacting:
            return
        self._held_history_merge = None
        request, series = held
        if (
            self._stopping_data_workers
            or request != self._current_chart_request
            or request != self._partial_history_request
            or self._state.symbol != request.symbol
            or self._state.timeframe != request.timeframe
            or self._route_for_symbol(request.symbol).connection != request.connection
        ):
            return
        self._apply_history_merge(series)
        self._record_load_step(request, "merge done")

    def _record_load_step(self, request: _ChartLoadRequest, step: str) -> None:
        self._load_timing.record(
            request.generation,
            request.symbol,
            request.timeframe.value,
            step,
            request.requested_at,
        )

    def _record_render_steps(self, request: _ChartLoadRequest) -> None:
        """Record render completion and the next event-loop turn after it."""
        if not self._load_timing.enabled:
            return
        self._record_load_step(request, "render done")
        QTimer.singleShot(
            0,
            lambda: self._record_load_step(request, "event loop resumed"),
        )

    def _apply_history_merge(
        self,
        series: OHLCVSeries,
        history_complete: bool = True,
    ) -> None:
        """
        Replace the displayed series with one that reaches further back (the
        full history window, or while it loads a longer interim window),
        keeping the view on the same bars (finplot x is bar-index based, so
        the view shifts right by the number of prepended bars).
        """
        previous = self._current_series
        prepended = 0
        if previous is not None and previous.bars:
            prepended = bisect.bisect_left(
                [bar.timestamp for bar in series.bars],
                previous.bars[0].timestamp,
            )
        if history_complete:
            self._partial_history_request = None
            self._reset_history_refresh()
        self._displayed_history_complete = history_complete
        self._current_series = series
        pm = self._chart.plot_manager
        with pm.preserving_viewport(x_shift=float(prepended)):
            pm.draw_candles(series)
            pm.draw_volume(series)
            render_passes = self._extension_runtime.render_all(
                series,
                history_complete,
            )
            for render_pass in render_passes:
                self._draw_extension_render(render_pass)
            self._remove_stale_extension_renders(render_passes)
        self._update_extension_notice(render_passes)
        pm.refresh(preserve_view=True)
        self._update_bar_correction_notice(series)
        if history_complete:
            route = self._route_for_symbol(series.symbol)
            self._symbol_bar.set_data_source(route.display_name, "connected")
            self._clear_history_status_message()
            self._resolve_basis_changes(route, series.symbol)
            QTimer.singleShot(0, self._check_older_history)

    def _clear_history_status_message(self) -> None:
        status_bar = self.statusBar()
        if (
            status_bar is not None
            and self._history_status_message is not None
            and status_bar.currentMessage() == self._history_status_message
        ):
            status_bar.clearMessage()
        self._history_status_message = None

    def _on_fetch_done(
        self,
        series: OHLCVSeries,
        history_complete: bool = True,
    ) -> None:
        """
        Called on the main thread with a freshly loaded series: the full
        window, or the recent window while older history loads
        (history_complete=False).
        """
        self._displayed_history_complete = history_complete
        if history_complete:
            self._partial_history_request = None
        # Save extension state for the symbol we're leaving.
        if self._loaded_symbol is not None and self._loaded_symbol != series.symbol:
            self._per_symbol_state[self._loaded_symbol] = [
                ChartExtensionState(
                    name=s.name,
                    params=copy.deepcopy(s.params),
                    visible=s.visible,
                    series_visibility=copy.deepcopy(s.series_visibility),
                )
                for s in self._state.extensions
                if _preserves_ui_state_per_symbol(s.name)
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
        self._chart_load_pending = False
        route = self._route_for_symbol(series.symbol)
        self._symbol_bar.set_data_source(
            route.display_name,
            "connected" if history_complete else "history",
        )
        self._extension_runtime.set_cache_namespace(route.cache_namespace)
        self._extension_store.load_for_symbol(series.symbol)
        self._render(series, history_complete)
        self._update_bar_correction_notice(series)
        if history_complete:
            self._clear_history_status_message()
        else:
            status_bar = self.statusBar()
            if status_bar is not None:
                self._history_status_message = "Loading indicators..."
                status_bar.showMessage(self._history_status_message)
        self._symbol_bar.set_symbol(series.symbol)
        self._watchlist.set_active_symbol(series.symbol)
        request = self._current_chart_request
        if (
            request is None
            or self._quote_refreshed_generation != request.generation
        ):
            self._refresh_level1()
        if history_complete:
            self._resolve_basis_changes(route, series.symbol)
            QTimer.singleShot(0, self._check_older_history)

    def _check_older_history(self) -> None:
        """
        Load older history when the view nears the oldest bar, or when an
        anchored extension waits on bars older than the loaded ones. Runs
        again after each load, merge and finished task, so a chain of loads
        continues until the view or anchor is covered or nothing older exists.
        """
        bars_wanted = self._chart.older_history_bars_wanted()
        pending_ms = self._extension_runtime.pending_history_start_ms
        series = self._current_series
        if (
            pending_ms is not None
            and series is not None
            and series.bars
            and self._displayed_history_complete
        ):
            gap = series.bars[0].timestamp - ms_to_datetime(pending_ms)
            if gap > timedelta(0):
                bars_per_day = max(1, bars_for_n_days(1, series.timeframe))
                bars_wanted = max(
                    bars_wanted,
                    math.ceil(gap.days * 5 / 7 * bars_per_day) + bars_per_day,
                )
        if bars_wanted > 0:
            self._on_older_history_wanted(bars_wanted)

    def _on_older_history_wanted(self, bars_wanted: int) -> None:
        """
        Load history older than the displayed series when the view nears its
        oldest bar. One at a time, only for a complete, settled chart that no
        other load or symbol refresh is writing.
        """
        request = self._current_chart_request
        series = self._current_series
        if (
            self._stopping_data_workers
            or request is None
            or series is None
            or not series.bars
            or series.symbol != request.symbol
            or series.timeframe != request.timeframe
            or not self._displayed_history_complete
            or self._chart_load_pending
            or self._pending_chart_load is not None
            or self._held_history_merge is not None
            or self._held_older_merge is not None
            or self._older_history_stopped == request
            or (request.cache_namespace, request.symbol) in self._symbol_refreshes
            or any(
                (task.request.cache_namespace, task.request.symbol)
                == (request.cache_namespace, request.symbol)
                for task in self._chart_load_tasks.values()
            )
            or not self._older_result_applies(request)
        ):
            return
        cancellation_event = threading.Event()
        worker = _OlderHistoryWorker(
            self._db_path,
            self._credential_store,
            request,
            series.bars[0].timestamp,
            series.bars[-1].timestamp,
            bars_wanted,
            cancellation_event,
        )
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_older_history_done)
        worker.error.connect(self._on_older_history_error)
        worker.completed.connect(thread.quit)
        worker.completed.connect(worker.deleteLater)
        generation = request.generation
        thread.finished.connect(
            lambda: self._on_chart_thread_finished(generation)
        )
        self._chart_load_tasks[generation] = _ChartLoadTask(
            request,
            thread,
            worker,
            cancellation_event,
            older=True,
        )
        status_bar = self.statusBar()
        if status_bar is not None:
            self._history_status_message = "Loading older history..."
            status_bar.showMessage(self._history_status_message)
        thread.start()

    def _older_result_applies(self, request: _ChartLoadRequest) -> bool:
        return (
            not self._stopping_data_workers
            and request == self._current_chart_request
            and self._state.symbol == request.symbol
            and self._state.timeframe == request.timeframe
            and self._route_for_symbol(request.symbol).connection
            == request.connection
            and self._current_series is not None
        )

    def _on_older_history_done(
        self,
        request: _ChartLoadRequest,
        series: OHLCVSeries | None,
        outcome: str,
    ) -> None:
        if not self._older_result_applies(request):
            return
        self._clear_history_status_message()
        if outcome == _OLDER_RESET:
            # The provider moved this history to a new adjustment basis; a
            # normal load detects and resets it.
            self._load()
            return
        if outcome == _OLDER_EXHAUSTED:
            self._older_history_stopped = request
            # Anchors older than all history now compute from the first bar.
            self._extension_runtime.set_older_history_may_arrive(False)
            if series is None:
                self._reload_extensions(draw_bars=False)
        if series is None:
            return
        if (
            self._drawing_session is not None
            or self._extension_runtime.drag_active
        ):
            self._held_older_merge = (request, series)
            return
        self._apply_history_merge(series)

    def _on_older_history_error(
        self,
        request: _ChartLoadRequest,
        message: str,
    ) -> None:
        if not self._older_result_applies(request):
            return
        self._clear_history_status_message()
        # Not retried for this chart; changing symbol or timeframe, or
        # reloading, tries again.
        self._older_history_stopped = request
        status_bar = self.statusBar()
        if status_bar is not None:
            status_bar.showMessage(message, _OLDER_HISTORY_ERROR_MS)

    def _resolve_basis_changes(self, route: _DataRoute, symbol: str) -> None:
        """
        Rescale or discard the symbol's cached history that its data source
        moved to a new adjustment basis but no longer returns. Only a
        timeframe downloaded again since its reset (coverage starts at its
        newest record's boundary) is resolved; the others wait. The
        remembered choice applies; otherwise the user is asked once per
        symbol and event this session, and the answer also applies when the
        same event is found on another timeframe. A rescale reloads the
        chart to show the history again.
        """
        cache_namespace = route.cache_namespace
        newest: dict[Timeframe, BasisChangeRecord] = {}
        for record in self._cache.get_basis_changes(cache_namespace, symbol):
            current = newest.get(record.timeframe)
            if current is None or record.boundary_ms > current.boundary_ms:
                newest[record.timeframe] = record
        ready: list[BasisChangeRecord] = []
        for timeframe, record in newest.items():
            coverage = self._cache.get_bar_fetch_coverage(
                cache_namespace,
                symbol,
                timeframe,
            )
            if coverage is not None and coverage[0] == record.boundary_ms:
                ready.append(record)
        if not ready:
            return
        choices: dict[Timeframe, BasisChangeChoice] = {}
        remembered = self._cache.get_basis_change_choice()
        answers = self._basis_change_answers.setdefault((cache_namespace, symbol), [])
        unasked: list[BasisChangeRecord] = []
        for record in ready:
            if remembered is not None:
                choices[record.timeframe] = remembered
                continue
            answered = [
                choice
                for ratio, choice in answers
                if math.isclose(ratio, record.ratio, rel_tol=_SAME_BASIS_EVENT_TOLERANCE)
            ]
            if not answered:
                unasked.append(record)
            elif answered[0] is not None:
                choices[record.timeframe] = answered[0]
        if unasked:
            # Recorded as dismissed before asking: the modal question runs a
            # nested event loop in which another load may resolve again.
            first = len(answers)
            answers.extend((record.ratio, None) for record in unasked)
            answer = ask_basis_change_choice(
                self,
                _basis_change_message(symbol, unasked[0]),
            )
            if answer is not None:
                answers[first:first + len(unasked)] = [
                    (record.ratio, answer[0]) for record in unasked
                ]
                if answer[1]:
                    self._cache.set_basis_change_choice(answer[0])
                for record in unasked:
                    choices[record.timeframe] = answer[0]
        try:
            for timeframe, choice in choices.items():
                dependents = _dependent_timeframes(route.aggregator, timeframe)
                if choice == BasisChangeChoice.RESCALE:
                    self._cache.rescale_basis_changes(
                        cache_namespace,
                        symbol,
                        timeframe,
                        dependents,
                    )
                else:
                    self._cache.discard_basis_changes(
                        cache_namespace,
                        symbol,
                        timeframe,
                        dependents,
                    )
        except sqlite3.Error as exc:
            # Each timeframe changes in one transaction; what is left is
            # still recorded and is offered again on the next load.
            self._basis_change_answers.pop((cache_namespace, symbol), None)
            context = ErrorContext(f"Updating {symbol}'s older cached history")
            show_warning(self, "Cache Update Failed", report_error(_LOGGER, exc, context))
            return
        if (
            BasisChangeChoice.RESCALE in choices.values()
            and self._state.symbol == symbol
        ):
            self._load()

    def _on_fetch_error(self, message: str) -> None:
        if self._state.symbol is not None:
            route = self._route_for_symbol(self._state.symbol)
            self._symbol_bar.set_data_source(route.display_name, "error")
        show_warning(self, "Load Error", message)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self, series: OHLCVSeries, history_complete: bool = True) -> None:
        """
        Draw candles, volume, and all active extensions for the series.
        Called after a successful fetch, and after a timeframe switch.
        """
        if not series.bars:
            show_warning(
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

        render_passes = self._extension_runtime.render_all(series, history_complete)
        for render_pass in render_passes:
            self._draw_extension_render(render_pass)
        self._remove_stale_extension_renders(render_passes)
        self._update_extension_notice(render_passes)

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
                context = ErrorContext("Adding the indicator panel")
                show_warning(
                    self,
                    "Panel Limit Reached",
                    report_error(_LOGGER, exc, context),
                )
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
            self._draw_vertical_line(vline, ind_state, update_legend)
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
        update_legend: bool = True,
    ) -> None:
        visible = ind_state.series_visibility.get(line.key, ind_state.visible)
        line.visible = visible and line.visible
        self._chart.plot_manager.update_vertical_line(line)
        if not update_legend:
            return
        self._chart.legend.add_extension(line.key, line.label, line.color)
        self._chart.legend.update_color(line.key, line.color)
        self._chart.legend.set_extension_visible(line.key, line.visible)

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

    def _interactive_series(self) -> OHLCVSeries | None:
        """The displayed series chart input resolves against; None while a
        load is replacing it."""
        return None if self._chart_load_pending else self._current_series

    def _menu_choice_applies(self, request: _ChartLoadRequest | None) -> bool:
        """
        QMenu.exec runs a nested event loop. The chosen action still applies
        while the same load is displayed (a history merge may have replaced
        the series), not once another load has started.
        """
        return (
            self._current_chart_request is request
            and self._interactive_series() is not None
        )

    def _end_interaction_sessions(self) -> None:
        """
        A new load replaces the displayed bars: end any drag or drawing in
        progress so it cannot resolve against, or write into, the next load.
        The active drawing tool stays selected.
        """
        self._chart.interactions.cancel_drag()
        self._end_drawing_session()

    def _end_drawing_session(self) -> ChartExtensionMutation | None:
        if self._drawing_session is None:
            return None
        mutation = self._extension_runtime.cancel_drawing(self._drawing_session)
        self._clear_pending_drawing_preview()
        self._drawing_session = None
        self._clear_preview_render()
        return mutation

    def _on_bar_clicked(self, x_pos: float, y_pos: float, px_x: float, px_y: float) -> None:
        series = self._interactive_series()
        if series is None or self._active_drawing_tool is None:
            return
        event = self._extension_runtime.chart_event(
            series, x_pos, y_pos,
            pixel_size_x=px_x, pixel_size_y=px_y,
        )
        if self._drawing_session is None:
            result = self._extension_runtime.start_drawing(
                self._active_drawing_tool,
                series,
                event,
            )
            self._handle_drawing_result(result)
            return
        result = self._extension_runtime.advance_drawing(
            series,
            self._drawing_session,
            event,
        )
        self._handle_drawing_result(result)

    def _on_mouse_move(self, x_pos: float, y_pos: float, px_x: float, px_y: float) -> None:
        series = self._interactive_series()
        if series is None or self._drawing_session is None:
            return
        self._pending_drawing_preview_event = self._extension_runtime.chart_event(
            series, x_pos, y_pos,
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
            self._flush_held_history_merge()
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
        mutation = self._end_drawing_session()
        self._set_active_drawing_tool(None)
        if mutation is not None:
            self._reload_extensions(draw_bars=False, preserve_view=True)
        self._flush_held_history_merge()

    def _on_drag_start(self, x_pos: float, y_pos: float, px_x: float, px_y: float) -> bool:
        series = self._interactive_series()
        if series is None:
            return False
        event = self._extension_runtime.chart_event(
            series,
            x_pos,
            y_pos,
            pixel_size_x=px_x,
            pixel_size_y=px_y,
        )
        return bool(self._extension_runtime.begin_drag(series, event))

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
        self._flush_held_history_merge()

    def _on_drag_cancel(self) -> None:
        self._extension_runtime.cancel_drag()
        self._reload_extensions(draw_bars=False, preserve_view=True)
        self._flush_held_history_merge()

    def _on_bar_right_clicked(self, x_pos: float, y_pos: float, px_x: float, px_y: float) -> None:
        """Right-click on a bar: show context menu."""
        series = self._interactive_series()
        if series is None:
            return
        event = self._extension_runtime.chart_event(
            series,
            x_pos,
            y_pos,
            button="right",
            pixel_size_x=px_x,
            pixel_size_y=px_y,
        )
        if self._drawing_session is None and self._show_drawing_context_menu(series, event):
            return
        actions = self._extension_runtime.context_actions(series, event)
        if not actions:
            return
        menu = QMenu(self)
        qactions = [
            (menu.addAction(action.label), action)
            for action in actions
        ]
        request = self._current_chart_request
        selected = menu.exec(QCursor.pos())
        if not self._menu_choice_applies(request):
            return
        for qaction, action in qactions:
            if selected == qaction:
                # The click-time series: the event's bar index refers to it.
                self._extension_runtime.apply_action(
                    series,
                    action,
                    event,
                )
                # Actions change extension state, never bars.
                self._reload_extensions(draw_bars=False)
                return

    def _show_drawing_context_menu(self, series: OHLCVSeries, event: ChartEvent) -> bool:
        hit = self._extension_runtime.drawing_hit_test(series, event)
        if hit is None:
            return False

        menu = QMenu(self)
        configure_action = None
        if self._extension_runtime.config_request(hit.handle_key) is not None:
            configure_action = menu.addAction("Configure...")
        remove_action = menu.addAction("Remove")
        request = self._current_chart_request
        selected = menu.exec(QCursor.pos())
        if not self._menu_choice_applies(request):
            return True

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
        if not draw_bars and self._current_series is None:
            # Nothing is shown yet; the pending load renders the extensions.
            return
        if not draw_bars and self._current_series is not None:
            series = self._current_series
        else:
            now = datetime.now(tz=timezone.utc)
            route = self._route_for_symbol(self._state.symbol)
            history_end = _history_end(
                self._state.timeframe,
                route.aggregator,
                now,
            )
            history_start = _display_history_start(
                self._cache,
                route.cache_namespace,
                self._state.symbol,
                self._state.timeframe,
                _history_start(
                    self._state.timeframe,
                    route.aggregator,
                    history_end,
                ),
                history_end,
            )
            if (
                self._current_series is not None
                and self._current_series.bars
                and self._current_series.symbol == self._state.symbol
                and self._current_series.timeframe == self._state.timeframe
            ):
                # Keep the displayed range: older bars stored by a running
                # backfill would shift every bar index, and older history
                # loaded by panning must stay shown.
                history_start = self._current_series.bars[0].timestamp
            bars = self._cache.get_bars(
                route.cache_namespace,
                self._state.symbol,
                self._state.timeframe,
                int(history_start.timestamp() * 1000),
                int(history_end.timestamp() * 1000),
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

        render_passes = self._extension_runtime.render_all(
            series,
            self._displayed_history_complete,
        )

        if preserve_view:
            with pm.preserving_viewport():
                for render_pass in render_passes:
                    self._draw_extension_render(render_pass)
                self._remove_stale_extension_renders(render_passes)
        else:
            for render_pass in render_passes:
                self._draw_extension_render(render_pass)
            self._remove_stale_extension_renders(render_passes)
        self._update_extension_notice(render_passes)
        pm.refresh(preserve_view=preserve_view)
        if draw_bars:
            self._update_bar_correction_notice(series)
        # A new or edited anchored extension may wait on older bars.
        if self._extension_runtime.pending_history_start_ms is not None:
            QTimer.singleShot(0, self._check_older_history)

    def _update_bar_correction_notice(self, series: OHLCVSeries) -> None:
        status_bar = self.statusBar()
        if status_bar is None:
            return
        if not series.bars:
            if status_bar.currentMessage() == self._bar_correction_notice:
                status_bar.clearMessage()
            self._bar_correction_notice = None
            return

        route = self._route_for_symbol(series.symbol)
        conflict_count = self._cache.count_bar_correction_conflicts(
            route.cache_namespace,
            series.symbol,
            series.timeframe,
            int(series.bars[0].timestamp.timestamp() * 1000),
            int(series.bars[-1].timestamp.timestamp() * 1000),
        )
        if conflict_count == 0:
            if status_bar.currentMessage() == self._bar_correction_notice:
                status_bar.clearMessage()
            self._bar_correction_notice = None
            return

        noun = "bar correction" if conflict_count == 1 else "bar corrections"
        verb = "needs" if conflict_count == 1 else "need"
        self._bar_correction_notice = (
            f"{conflict_count} {noun} {verb} review in Settings > Data Quality."
        )
        status_bar.showMessage(self._bar_correction_notice)

    def _update_extension_notice(
        self,
        render_passes: list[ChartExtensionRenderPass],
    ) -> None:
        """
        Show what extensions report about content they did not draw (for
        example an AVWAP anchored too far back for an intraday chart), and
        clear it once no extension reports anything.
        """
        status_bar = self.statusBar()
        if status_bar is None:
            return
        notices = [
            render_pass.render.notice
            for render_pass in render_passes
            if render_pass.render.notice
        ]
        notice = " ".join(notices) if notices else None
        if notice is None:
            if (
                self._extension_notice is not None
                and status_bar.currentMessage() == self._extension_notice
            ):
                status_bar.clearMessage()
        elif notice != status_bar.currentMessage():
            status_bar.showMessage(notice)
        self._extension_notice = notice

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

    def _on_application_settings(self) -> None:
        if self._closing or self._stopping_data_workers:
            return
        if self._validation_thread is not None:
            show_information(
                self,
                "Connection Test In Progress",
                "Wait for the current provider connection test to finish.",
            )
            return
        current_route = (
            None
            if self._state.symbol is None
            else self._route_for_symbol(self._state.symbol)
        )
        comparison_service = BarComparisonService(
            self._credential_store,
            self._cache.get_provider_connections(),
            self._provider_availability,
            preferred_connection_id=(
                self._selected_route.connection.connection_id
            ),
        )
        dialog = ApplicationSettingsDialog(
            self._cache,
            self._credential_store,
            self._provider_availability,
            self,
            active_connection_id=self._selected_route.connection.connection_id,
            current_cache_namespace=(
                None if current_route is None else current_route.cache_namespace
            ),
            current_symbol=self._state.symbol,
            current_timeframe=self._state.timeframe,
            comparison_service=comparison_service,
            symbol_refresh=self,
        )
        result = dialog.exec()
        if dialog.bars_changed():
            self._reload_extensions()
        if result == QDialog.DialogCode.Accepted:
            self._request_provider_activation(dialog.selected_connection_id())

    def _request_provider_activation(self, connection_id: str) -> None:
        if (
            self._closing
            or self._stopping_data_workers
            or self._validation_thread is not None
        ):
            return
        connection = self._cache.get_provider_connection(connection_id)
        if connection is None:
            raise RuntimeError(f"Unknown provider connection: {connection_id}")

        if connection.connection_id == YFINANCE_CONNECTION_ID:
            self._commit_provider_route(self._yahoo_route)
            return

        availability = self._provider_availability.get(connection.provider_name)
        if availability is None or not availability.available:
            reason = (
                "Provider availability was not checked."
                if availability is None
                else availability.reason
            )
            status_bar = self.statusBar()
            if status_bar is not None:
                status_bar.showMessage(
                    f"Could not activate {connection.display_name}: {reason}",
                    10000,
                )
            return

        try:
            provider = create_provider(connection, self._credential_store)
        except Exception as exc:
            context = ErrorContext(f"Configuring {connection.display_name}")
            message = report_error(_LOGGER, exc, context)
            show_warning(
                self,
                "Connection Failed",
                f"Could not configure {connection.display_name}.\n\n{message}",
            )
            return

        self._pending_route = _DataRoute(Aggregator(provider), connection)
        cancellation_event = threading.Event()
        worker = _ProviderValidationWorker(
            provider,
            connection.display_name,
            cancellation_event,
        )
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_provider_validation_done)
        worker.error.connect(self._on_provider_validation_error)
        worker.completed.connect(thread.quit)
        worker.completed.connect(worker.deleteLater)
        thread.finished.connect(self._on_validation_thread_finished)

        self._validation_thread = thread
        self._validation_worker = worker
        self._validation_cancellation_event = cancellation_event
        status_bar = self.statusBar()
        if status_bar is not None:
            status_bar.showMessage(f"Testing {connection.display_name}...")
        thread.start()

    def _on_provider_validation_done(self) -> None:
        if self._closing or self._stopping_data_workers:
            return
        route = self._pending_route
        self._pending_route = None
        if route is None:
            return
        self._commit_provider_route(route)

    def _on_provider_validation_error(self, message: str) -> None:
        if self._closing or self._stopping_data_workers:
            return
        route = self._pending_route
        self._pending_route = None
        status_bar = self.statusBar()
        if status_bar is not None:
            status_bar.clearMessage()
        display_name = "the selected provider"
        if route is not None:
            display_name = route.connection.display_name
        show_warning(
            self,
            "Connection Failed",
            f"Could not activate {display_name}.\n\n{message}",
        )

    def _commit_provider_route(self, route: _DataRoute) -> None:
        if self._closing or self._stopping_data_workers:
            return
        self._quote_route_generation += 1
        self._selected_route = route
        self._cache.set_active_provider_connection_id(route.connection.connection_id)
        status_bar = self.statusBar()
        if status_bar is not None:
            status_bar.showMessage(
                f"Using {route.connection.display_name}",
                5000,
            )
        if self._state.symbol is not None:
            active_route = self._route_for_symbol(self._state.symbol)
            self._extension_runtime.set_cache_namespace(
                active_route.cache_namespace
            )
            self._load()
        self._refresh_watchlist_snapshots()
        self._refresh_level1()

    def _stop_data_workers(self) -> None:
        self._stopping_data_workers = True
        self._snapshot_refresh_pending = False
        self._level1_refresh_pending = False
        if self._snapshot_timer.isActive():
            self._snapshot_timer.stop()
        if self._chart_load_timer.isActive():
            self._chart_load_timer.stop()
        if self._drawing_preview_timer.isActive():
            self._drawing_preview_timer.stop()
        self._pending_chart_load = None
        self._held_history_merge = None
        self._held_older_merge = None
        self._reset_history_refresh()
        self._drop_refresh_reload()
        for task in tuple(self._chart_load_tasks.values()):
            task.cancellation_event.set()
        if self._snapshot_cancellation_event is not None:
            self._snapshot_cancellation_event.set()
        if self._level1_cancellation_event is not None:
            self._level1_cancellation_event.set()
        if self._validation_cancellation_event is not None:
            self._validation_cancellation_event.set()

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
        self._watchlist_membership_revision += 1
        self._refresh_watchlist_snapshots()

    def _on_watchlist_remove(self, symbol: str) -> None:
        """Remove a watchlist entry from DB and UI."""
        self._cache.remove_from_watchlist(symbol)
        self._watchlist.remove_symbol(symbol)
        self._watchlist_membership_revision += 1
        self._refresh_watchlist_snapshots()

    def _on_watchlist_reorder(self, symbols: list[str]) -> None:
        """Persist reordered watchlist symbols."""
        self._cache.reorder_watchlist(symbols)

    def _quote_route_identity(self, symbol: str) -> _QuoteRouteIdentity:
        return _QuoteRouteIdentity(
            self._route_for_symbol(symbol).connection,
            0 if _is_yahoo_futures_symbol(symbol) else self._quote_route_generation,
        )

    def _make_snapshot_request(self) -> _SnapshotRequest:
        return _SnapshotRequest(
            self._watchlist_membership_revision,
            tuple(
                (symbol, self._quote_route_identity(symbol))
                for symbol in sorted(self._watchlist.symbols())
            ),
        )

    def _refresh_watchlist_snapshots(self) -> None:
        if self._closing or self._stopping_data_workers:
            return
        request = self._make_snapshot_request()
        if self._snapshot_thread is not None:
            if request != self._snapshot_request:
                self._snapshot_refresh_pending = True
            return
        self._snapshot_refresh_pending = False
        symbols = self._watchlist.symbols()
        if not symbols:
            return

        symbols_by_namespace: dict[str, tuple[Aggregator, list[str]]] = {}
        for symbol in symbols:
            route = self._route_for_symbol(symbol)
            existing = symbols_by_namespace.get(route.cache_namespace)
            if existing is None:
                symbols_by_namespace[route.cache_namespace] = (
                    route.aggregator,
                    [symbol],
                )
            else:
                existing[1].append(symbol)
        cancellation_event = threading.Event()
        worker = _SnapshotWorker(
            [
                _SnapshotBatch(aggregator, batch_symbols)
                for aggregator, batch_symbols in symbols_by_namespace.values()
            ],
            request,
            cancellation_event,
        )
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_watchlist_snapshots)
        worker.error.connect(self._on_watchlist_snapshot_error)
        worker.completed.connect(thread.quit)
        worker.completed.connect(worker.deleteLater)
        thread.finished.connect(self._on_snapshot_thread_finished)

        self._snapshot_request = request
        self._snapshot_thread = thread
        self._snapshot_worker = worker
        self._snapshot_cancellation_event = cancellation_event
        thread.start()

    def _on_watchlist_snapshots(
        self,
        request: _SnapshotRequest,
        snapshots: dict[str, MarketSnapshot],
    ) -> None:
        if (
            self._closing
            or self._stopping_data_workers
            or request.membership_revision != self._watchlist_membership_revision
        ):
            return
        current_symbols = set(self._watchlist.symbols())
        routes = dict(request.routes)
        self._watchlist.set_percent_changes(
            {
                symbol: snapshot.change_percent
                for symbol, snapshot in snapshots.items()
                if symbol in current_symbols
                and routes.get(symbol) == self._quote_route_identity(symbol)
            }
        )

    def _refresh_level1(self) -> None:
        if self._closing or self._stopping_data_workers:
            return
        symbol = self._state.symbol
        if symbol != self._level1_symbol:
            self._level1_symbol = symbol
            self._level1_symbol_generation += 1
        if symbol is None:
            return
        request = _Level1Request(
            symbol, self._level1_symbol_generation, self._quote_route_identity(symbol),
        )
        if self._level1_thread is not None:
            if request != self._level1_request:
                self._level1_refresh_pending = True
            return
        self._level1_refresh_pending = False
        route = self._route_for_symbol(symbol)
        now = datetime.now(tz=timezone.utc)
        last_attempt = self._asset_reference_refresh_attempts.get(symbol)
        refresh_reference = _asset_reference_refresh_due(
            self._cache,
            symbol,
            now,
            last_attempt,
        )
        if refresh_reference:
            self._asset_reference_refresh_attempts[symbol] = now
        cancellation_event = threading.Event()
        worker = _Level1Worker(
            route.aggregator,
            self._yahoo_route.aggregator,
            self._db_path,
            request,
            refresh_reference,
            now,
            cancellation_event,
        )
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_level1)
        worker.error.connect(self._on_level1_error)
        worker.completed.connect(thread.quit)
        worker.completed.connect(worker.deleteLater)
        thread.finished.connect(self._on_level1_thread_finished)

        self._level1_request = request
        self._level1_thread = thread
        self._level1_worker = worker
        self._level1_cancellation_event = cancellation_event
        thread.start()

    def _is_current_level1_request(self, request: _Level1Request) -> bool:
        return (
            not self._closing
            and not self._stopping_data_workers
            and request.symbol == self._state.symbol
            and request.symbol_generation == self._level1_symbol_generation
            and request.route == self._quote_route_identity(request.symbol)
        )

    def _on_level1(self, request: _Level1Request, quote: Level1Quote | None) -> None:
        if (
            not self._is_current_level1_request(request)
            or quote is None
            or quote.symbol != request.symbol
        ):
            return
        self._app_header.set_quote(quote)

    def _on_level1_error(self, request: _Level1Request, message: str) -> None:
        if not self._is_current_level1_request(request):
            return
        status_bar = self.statusBar()
        if status_bar is not None:
            status_bar.showMessage(f"Quote unavailable: {message}", 5000)

    def _on_watchlist_snapshot_error(
        self, request: _SnapshotRequest, message: str,
    ) -> None:
        if (
            self._closing
            or self._stopping_data_workers
            or request != self._make_snapshot_request()
        ):
            return
        status_bar = self.statusBar()
        if status_bar is not None:
            status_bar.showMessage(f"Watchlist quotes unavailable: {message}", 5000)

    def _on_snapshot_thread_finished(self) -> None:
        thread = self._snapshot_thread
        assert thread is not None
        thread.deleteLater()
        self._snapshot_thread = None
        self._snapshot_worker = None
        self._snapshot_request = None
        self._snapshot_cancellation_event = None
        if (
            self._snapshot_refresh_pending
            and not self._closing
            and not self._stopping_data_workers
        ):
            self._refresh_watchlist_snapshots()
        self._maybe_finalize_close()

    def _on_level1_thread_finished(self) -> None:
        thread = self._level1_thread
        assert thread is not None
        thread.deleteLater()
        self._level1_thread = None
        self._level1_worker = None
        self._level1_request = None
        self._level1_cancellation_event = None
        if (
            self._level1_refresh_pending
            and not self._closing
            and not self._stopping_data_workers
        ):
            self._refresh_level1()
        self._maybe_finalize_close()

    def _on_validation_thread_finished(self) -> None:
        thread = self._validation_thread
        if thread is None:
            return
        thread.deleteLater()
        self._validation_thread = None
        self._validation_worker = None
        self._validation_cancellation_event = None
        if self._closing or self._stopping_data_workers:
            self._pending_route = None
        self._maybe_finalize_close()

    def _maybe_finalize_close(self) -> None:
        if not self._closing or self._close_finalized or self._close_finalize_scheduled:
            return
        if (
            self._chart_load_tasks
            or self._snapshot_thread is not None
            or self._level1_thread is not None
            or self._validation_thread is not None
        ):
            return
        self._close_finalize_scheduled = True
        QTimer.singleShot(0, self._finalize_close)

    def _finalize_close(self) -> None:
        self._close_finalize_scheduled = False
        if (
            not self._closing
            or self._close_finalized
            or self._chart_load_tasks
            or self._snapshot_thread is not None
            or self._level1_thread is not None
            or self._validation_thread is not None
        ):
            return
        self._pending_route = None
        set_disable_handler(None)
        self._cache.close()
        self._load_timing.close()
        self._close_finalized = True
        self.close()

    def _on_extensions_turned_off(self, names: frozenset[str]) -> None:
        """
        Runs inside the failing extension call: clear the host's interaction
        state for the turned-off extensions now, without calling plugin
        code, and redraw and announce once the call has unwound.
        """
        session = self._drawing_session
        if (
            session is not None and session.extension_name in names
        ) or self._active_drawing_tool in names:
            self._clear_pending_drawing_preview()
            self._drawing_session = None
            self._clear_preview_render()
            self._set_active_drawing_tool(None)
        self._extension_runtime.release_turned_off_drag()
        self._turned_off_extensions.update(names)
        self._schedule_extension_cleanup()

    def _schedule_extension_cleanup(self) -> None:
        if self._extension_cleanup_scheduled:
            return
        self._extension_cleanup_scheduled = True
        QTimer.singleShot(0, self._finish_extension_cleanup)

    def _finish_extension_cleanup(self) -> None:
        self._extension_cleanup_scheduled = False
        names = self._turned_off_extensions
        self._turned_off_extensions = set()
        if self._closing or self._stopping_data_workers:
            return
        if names and self._current_series is not None and self._state.symbol is not None:
            # Re-rendering without the turned-off extensions removes their
            # series, markers and legend entries.
            self._reload_extensions(draw_bars=False, preserve_view=True)
            self._release_unused_panels(names)
            self._flush_held_history_merge()
        self._show_extension_notices()

    def _release_unused_panels(self, names: set[str]) -> None:
        runtime = self._extension_runtime
        in_use = {
            runtime.rendered_target(state.name)
            for state in self._state.extensions
            if not is_disabled(state.name)
        }
        for name in names:
            target = runtime.rendered_target(name)
            if target is not None and target != RENDER_CHART and target not in in_use:
                self._chart.release_extension_panel(target)

    def _show_extension_notices(self) -> None:
        notices = take_notices()
        if not notices:
            return
        # Non-blocking: the chart stays usable while the notice is open.
        dialog = ApplicationMessageDialog(
            _EXTENSION_NOTICE_TITLE,
            "\n".join(notices),
            MessageKind.WARNING,
            self,
        )
        dialog.setModal(False)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()

    def _on_add_extension(self) -> None:
        """
        Show a menu of dialog-added extension types, open a config dialog
        for the selected type, then add the new extension to state and
        redraw.
        """
        entries: dict[str, tuple[ChartExtension, str]] = {}
        for name in sorted(all_extensions()):
            extension = guarded_extension(name)
            if extension is None:
                continue
            add_mode = call_extension(name, extension.add_mode)
            if add_mode != ChartExtensionAddMode.DIALOG:
                continue
            label = call_extension(name, extension.label)
            if label is not None:
                entries[name] = (extension, label)
        if not entries:
            return

        menu = QMenu(self)
        # Map action -> (name, instance) so we can look up the selection.
        actions = {
            menu.addAction(label): (name, extension)
            for name, (extension, label) in entries.items()
        }
        action = menu.exec(QCursor.pos())
        if action not in actions:
            return

        name, extension = actions[action]
        extension_label = call_extension(name, extension.label)
        params = call_extension(name, extension.default_params)
        if extension_label is None or params is None:
            return
        dialog = ExtensionConfigDialog(
            extension_label=extension_label,
            params=params,
            parent=self,
        )
        if (
            dialog.exec() == ExtensionConfigDialog.DialogCode.Accepted
            and not is_disabled(name)
        ):
            self._state.extensions.append(
                ChartExtensionState(name=name, params=dialog.result_params())
            )
            self._reload_extensions(draw_bars=False)

    def _drawing_tool_entries(self) -> list[tuple[str, str, ToolIconSpec | None]]:
        toolbar: dict[str, ChartExtension] = {}
        for name in all_extensions():
            tool = guarded_extension(name)
            if tool is None:
                continue
            if call_extension(name, tool.add_mode) == ChartExtensionAddMode.TOOLBAR:
                toolbar[name] = tool
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
        entries: list[tuple[str, str, ToolIconSpec | None]] = []
        for name in ordered:
            label = call_extension(name, toolbar[name].label)
            icon = call_extension(name, toolbar[name].toolbar_icon)
            if label is not None and not is_disabled(name):
                entries.append((name, label, icon))
        return entries

    def _on_drawing_tool_selected(self, extension_name: str) -> None:
        if is_disabled(extension_name):
            # A turned-off tool stays off until restart; keep the palette on
            # the current tool.
            self._chart.set_active_drawing_tool(self._active_drawing_tool)
            return
        if self._drawing_session is not None:
            self._extension_runtime.cancel_drawing(self._drawing_session)
            self._clear_pending_drawing_preview()
            self._drawing_session = None
            self._clear_preview_render()
        if is_disabled(extension_name):
            # Cancelling the session turned this tool off.
            self._chart.set_active_drawing_tool(self._active_drawing_tool)
        else:
            self._set_active_drawing_tool(extension_name)
        self._flush_held_history_merge()

    def _set_active_drawing_tool(self, extension_name: str | None) -> None:
        self._active_drawing_tool = extension_name
        self._chart.set_active_drawing_tool(extension_name)

    def _clear_pending_drawing_preview(self) -> None:
        self._pending_drawing_preview_event = None
        if self._drawing_preview_timer.isActive():
            self._drawing_preview_timer.stop()

    def closeEvent(self, event: QCloseEvent | None) -> None:
        if self._close_finalized:
            super().closeEvent(event)
            return
        if event is not None:
            event.ignore()
        if self._closing:
            return
        self._closing = True
        for widget in (
            self._app_header,
            self._watchlist,
            self._symbol_bar,
            self._chart,
        ):
            widget.setEnabled(False)
        status_bar = self.statusBar()
        if status_bar is not None:
            status_bar.showMessage("Closing...")
        self._stop_data_workers()
        self._maybe_finalize_close()
