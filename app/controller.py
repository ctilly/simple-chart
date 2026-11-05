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

import copy
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from PyQt6.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QCursor, QGuiApplication
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
from app.dialogs import show_information, show_warning
from app.extension_runtime import ChartExtensionRenderPass, ChartExtensionRuntime
from app.extension_store import ChartExtensionStore
from app.header_bar import AppHeader
from app.state import ChartExtensionState, State
from app.symbol_bar import SymbolBar
from app.watchlist import WatchlistWidget
from app.window_chrome import (
    MainWindowTitleBar,
    WindowResizeController,
    is_wayland_platform,
)
from chart.window import ChartWidget
from data.aggregator import Aggregator
from data.cache import Cache
from data.models import Bar, Level1Quote, MarketSnapshot, OHLCVSeries, Timeframe
from data.provider import (
    ProviderAvailability,
    ProviderConfigurationError,
    create_provider,
    provider_availability,
)
from data.provider.base import DataProvider, HistoryUnavailableError
from data.provider.config import ProviderConnection, YFINANCE_CONNECTION_ID
from data.provider.credentials import (
    CredentialStore,
    CredentialStoreAccess,
    initialize_keyring_credential_store,
)
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

# Intraday data is intentionally bounded to keep the initial request and local
# cache size practical. Daily and weekly history use the fixed start below.
_DEFAULT_LOOKBACK_DAYS = 600
_DAILY_HISTORY_START = datetime(2016, 1, 1, tzinfo=timezone.utc)
_SNAPSHOT_REFRESH_MS = 60_000
_DRAWING_PREVIEW_FRAME_MS = 16
_CHART_LOAD_DEBOUNCE_MS = 100
_MAX_ACTIVE_CHART_LOADS = 2
_ASSET_REFERENCE_MAX_AGE = timedelta(days=30)
_ASSET_REFERENCE_RETRY_DELAY = timedelta(hours=1)
_ET = ZoneInfo("America/New_York")
_REGULAR_SESSION_OPEN = time(9, 30)
_REGULAR_SESSION_CLOSE = time(16, 0)
_REGULAR_SESSION_MINUTES = 390
_DAILY_SETTLEMENT_DELAY = timedelta(minutes=5)
_REGULAR_EQUITY_SYMBOL = re.compile(r"[A-Z]{1,5}")


def _history_start(
    timeframe: Timeframe,
    aggregator: Aggregator,
    end: datetime,
) -> datetime:
    desired = (
        end - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        if timeframe.is_intraday
        else _DAILY_HISTORY_START
    )
    provider_limit = aggregator.earliest_history_start(timeframe, end)
    if provider_limit is None:
        return desired
    return max(desired, provider_limit)


def _history_end(
    timeframe: Timeframe,
    aggregator: Aggregator,
    end: datetime,
) -> datetime:
    return aggregator.latest_history_end(timeframe, end)


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
class _ChartLoadRequest:
    generation: int
    symbol: str
    timeframe: Timeframe
    connection: ProviderConnection

    @property
    def cache_namespace(self) -> str:
        return self.connection.cache_namespace


@dataclass
class _ChartLoadTask:
    request: _ChartLoadRequest
    thread: QThread
    worker: "_FetchWorker"


def _is_yahoo_futures_symbol(symbol: str) -> bool:
    return symbol.strip().upper().endswith("=F")


def _uses_regular_equity_session(symbol: str) -> bool:
    normalized = symbol.strip().upper()
    return _REGULAR_EQUITY_SYMBOL.fullmatch(normalized) is not None


def _latest_expected_regular_session(
    endpoint: datetime,
) -> tuple[date, bool]:
    local = endpoint.astimezone(_ET)
    local_time = local.time().replace(tzinfo=None)
    if (
        local.weekday() < 5
        and _REGULAR_SESSION_OPEN <= local_time < _REGULAR_SESSION_CLOSE
    ):
        return (local.date(), True)
    if local.weekday() < 5 and local_time >= _REGULAR_SESSION_CLOSE:
        return (local.date(), False)

    session_date = local.date() - timedelta(days=1)
    while session_date.weekday() >= 5:
        session_date -= timedelta(days=1)
    return (session_date, False)


def _expected_last_session_bar_start(
    timeframe: Timeframe,
    session_date: date,
    tz: tzinfo | None,
) -> datetime:
    session_open = datetime.combine(
        session_date,
        _REGULAR_SESSION_OPEN,
        tzinfo=_ET,
    )
    if timeframe == Timeframe.DAILY:
        return session_open.replace(hour=0, minute=0).astimezone(tz)
    period_minutes = timeframe.minutes
    if period_minutes is None:
        raise ValueError(f"No regular-session bar boundary for {timeframe!r}.")
    final_period = (_REGULAR_SESSION_MINUTES - 1) // period_minutes
    return (
        session_open + timedelta(minutes=final_period * period_minutes)
    ).astimezone(tz)


def _regular_session_settled_at(
    timeframe: Timeframe,
    session_date: date,
    tz: tzinfo | None,
) -> datetime:
    boundary_date = (
        session_date + timedelta(days=1)
        if timeframe == Timeframe.DAILY
        else session_date
    )
    boundary_time = (
        time.min
        if timeframe == Timeframe.DAILY
        else _REGULAR_SESSION_CLOSE
    )
    return datetime.combine(
        boundary_date,
        boundary_time,
        tzinfo=_ET,
    ).astimezone(tz)


def _completed_coverage_end(
    symbol: str,
    timeframe: Timeframe,
    endpoint: datetime,
    completed_end: datetime,
) -> datetime:
    if timeframe != Timeframe.DAILY or not _uses_regular_equity_session(symbol):
        return completed_end
    local = endpoint.astimezone(_ET)
    settled_at = datetime.combine(
        local.date(),
        _REGULAR_SESSION_CLOSE,
        tzinfo=_ET,
    ) + _DAILY_SETTLEMENT_DELAY
    if local.weekday() >= 5 or local < settled_at:
        return completed_end
    return _regular_session_settled_at(
        timeframe,
        local.date(),
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
        return _StartupRoutes(
            yahoo_route,
            yahoo_route,
            f"{selected_connection.display_name} is unavailable: {exc}",
        )
    return _StartupRoutes(
        _DataRoute(Aggregator(selected_provider), selected_connection),
        yahoo_route,
        None,
    )


class _FetchWorker(QObject):
    """
    Runs a data fetch in a background thread.

    Emits finished(series) on success or error(message) on failure.
    The controller creates one worker per fetch, moves it to a QThread,
    and connects its signals before starting.
    """

    finished: pyqtSignal = pyqtSignal(object, object)
    error: pyqtSignal = pyqtSignal(object, str)

    def __init__(
        self,
        db_path: str,
        credential_store: CredentialStore,
        request: _ChartLoadRequest,
    ) -> None:
        super().__init__()
        self._db_path = db_path
        self._credential_store = credential_store
        self._request = request

    def run(self) -> None:
        """
        Fetch bars and return an OHLCVSeries.

        Intraday loads also refresh daily bars so day-based indicators have
        reference data without requiring the user to visit the daily chart.
        """
        try:
            provider = create_provider(
                self._request.connection,
                self._credential_store,
            )
            aggregator = Aggregator(provider)
            now = datetime.now(tz=timezone.utc)
            with Cache(self._db_path) as cache:
                series = _fetch_series_with_references(
                    aggregator,
                    cache,
                    self._request.cache_namespace,
                    self._request.symbol,
                    self._request.timeframe,
                    now,
                )
            self.finished.emit(self._request, series)

        except Exception as exc:
            self.error.emit(self._request, str(exc))


class _SnapshotWorker(QObject):
    finished: pyqtSignal = pyqtSignal(object)   # emits dict[str, MarketSnapshot]
    error:    pyqtSignal = pyqtSignal(str)

    def __init__(self, batches: list[_SnapshotBatch]) -> None:
        super().__init__()
        self._batches = list(batches)

    def run(self) -> None:
        try:
            snapshots: dict[str, MarketSnapshot] = {}
            for batch in self._batches:
                snapshots.update(batch.aggregator.fetch_snapshots(batch.symbols))
            self.finished.emit(snapshots)
        except Exception as exc:
            self.error.emit(str(exc))


class _Level1Worker(QObject):
    finished: pyqtSignal = pyqtSignal(object)   # emits Level1Quote | None

    def __init__(
        self,
        aggregator: Aggregator,
        reference_aggregator: Aggregator,
        cache: Cache,
        symbol: str,
        refresh_reference: bool,
        now: datetime,
    ) -> None:
        super().__init__()
        self._aggregator = aggregator
        self._reference_aggregator = reference_aggregator
        self._cache = cache
        self._symbol = symbol
        self._refresh_reference = refresh_reference
        self._now = now

    def run(self) -> None:
        quote = self._aggregator.fetch_level1(self._symbol)
        if quote is None:
            self.finished.emit(None)
            return
        known_reference_name = (
            quote.company_name
            if self._aggregator is self._reference_aggregator
            else None
        )
        company_name = _resolve_company_name(
            self._cache,
            self._reference_aggregator,
            self._symbol,
            self._now,
            self._refresh_reference,
            known_reference_name,
        )
        if company_name != quote.company_name:
            quote = replace(quote, company_name=company_name)
        self.finished.emit(quote)


class _ProviderValidationWorker(QObject):
    finished: pyqtSignal = pyqtSignal()
    error: pyqtSignal = pyqtSignal(str)

    def __init__(self, provider: DataProvider) -> None:
        super().__init__()
        self._provider = provider

    def run(self) -> None:
        try:
            _validate_provider_connection(self._provider)
            self.finished.emit()
        except Exception as exc:
            self.error.emit(str(exc))


def _validate_provider_connection(provider: DataProvider) -> None:
    if provider.fetch_level1("SPY") is None:
        raise RuntimeError("The provider returned no SPY market-data snapshot.")


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
) -> OHLCVSeries:
    if timeframe.is_intraday:
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
        )
    return _fetch_series(
        aggregator,
        cache,
        cache_namespace,
        symbol,
        timeframe,
        now,
    )


def _fetch_series(
    aggregator: Aggregator,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    now: datetime,
) -> OHLCVSeries:
    history_end = _history_end(timeframe, aggregator, now)
    history_start = _history_start(timeframe, aggregator, history_end)
    if aggregator.native_timeframe(timeframe) != timeframe:
        return _fetch_synthesized_series(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            timeframe,
            history_start,
            history_end,
        )
    _fetch_and_cache_bars(
        aggregator,
        cache,
        cache_namespace,
        symbol,
        timeframe,
        history_start,
        history_end,
    )
    bars = cache.get_bars(
        cache_namespace,
        symbol,
        timeframe,
        int(history_start.timestamp() * 1000),
        int(history_end.timestamp() * 1000),
    )
    return OHLCVSeries(
        symbol=symbol,
        timeframe=timeframe,
        bars=bars,
    )


def _fetch_synthesized_series(
    aggregator: Aggregator,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    history_start: datetime,
    history_end: datetime,
) -> OHLCVSeries:
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
    base_history_start = history_start
    target_covers_head = False
    target_refresh_start: datetime | None = None
    target_coverage_is_legacy = False
    if existing and target_coverage is not None:
        target_covers_head = target_coverage[0] <= requested_start_ms
        target_coverage_is_legacy = _is_legacy_target_coverage(
            aggregator,
            timeframe,
            target_coverage,
        )
        target_coverage_end = datetime.fromtimestamp(
            target_coverage[1] / 1000,
            tz=timezone.utc,
        )
        target_refresh_start = _cached_tail_refresh_start(
            aggregator,
            symbol,
            timeframe,
            history_start,
            target_coverage_end,
            existing[-1].timestamp,
            history_end,
        )
        if (
            target_covers_head
            and target_refresh_start is None
            and (base_coverage is None or not target_coverage_is_legacy)
        ):
            return OHLCVSeries(symbol, timeframe, existing)
        if target_covers_head and target_refresh_start is not None:
            base_history_start = max(history_start, target_refresh_start)
    preserved_target_covers_head = target_covers_head or bool(
        existing and existing[0].timestamp <= history_start
    )
    if preserved_target_covers_head and base_coverage is not None:
        base_coverage_start = datetime.fromtimestamp(
            base_coverage[0] / 1000,
            tz=timezone.utc,
        )
        base_coverage_end = datetime.fromtimestamp(
            base_coverage[1] / 1000,
            tz=timezone.utc,
        )
        if target_refresh_start is None:
            base_history_start = max(history_start, base_coverage_start)
        elif base_history_start > base_coverage_end:
            base_history_start = max(history_start, base_coverage_end)
    try:
        _fetch_and_cache_bars(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            base_timeframe,
            base_history_start,
            history_end,
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
        datetime.fromtimestamp(source_end_ms / 1000, tz=timezone.utc),
    )
    target_end_ms = int(completed_target_end.timestamp() * 1000)
    rebuild_start_ms = _materialization_start_ms(
        aggregator,
        timeframe,
        target_coverage,
        source_start_ms,
        target_end_ms,
    )
    aligned_start_ms = max(
        source_start_ms,
        int(
            _exchange_day_start(
                datetime.fromtimestamp(rebuild_start_ms / 1000, tz=timezone.utc)
            ).timestamp()
            * 1000
        ),
    )
    raw_bars = cache.get_raw_bars(
        cache_namespace,
        symbol,
        base_timeframe,
        aligned_start_ms,
        source_end_ms,
    )
    if _uses_regular_equity_session(symbol):
        raw_bars = [bar for bar in raw_bars if _is_regular_session_bar(bar)]
    materialized = aggregator.synthesize_cached_bars(raw_bars, timeframe)
    if not materialized:
        return OHLCVSeries(symbol, timeframe, existing)

    coverage_start_ms = min(
        aligned_start_ms,
        (
            target_coverage[0]
            if target_coverage is not None and existing
            else aligned_start_ms
        ),
    )
    cache.replace_materialized_bars(
        cache_namespace,
        symbol,
        timeframe,
        aligned_start_ms,
        source_end_ms,
        materialized,
        coverage_start_ms,
        target_end_ms,
    )
    bars = cache.get_bars(
        cache_namespace,
        symbol,
        timeframe,
        requested_start_ms,
        requested_end_ms,
    )
    return OHLCVSeries(symbol, timeframe, bars)


def _materialization_start_ms(
    aggregator: Aggregator,
    timeframe: Timeframe,
    target_coverage: tuple[int, int] | None,
    source_start_ms: int,
    target_end_ms: int,
) -> int:
    if target_coverage is None:
        return source_start_ms
    target_start_ms, previous_end_ms = target_coverage
    if _is_legacy_target_coverage(aggregator, timeframe, target_coverage):
        return source_start_ms
    if source_start_ms < target_start_ms:
        return source_start_ms
    return min(previous_end_ms, target_end_ms)


def _is_legacy_target_coverage(
    aggregator: Aggregator,
    timeframe: Timeframe,
    target_coverage: tuple[int, int],
) -> bool:
    previous_end_ms = target_coverage[1]
    previous_end = datetime.fromtimestamp(previous_end_ms / 1000, tz=timezone.utc)
    aligned_previous_end_ms = int(
        aggregator.completed_history_end(timeframe, previous_end).timestamp() * 1000
    )
    return aligned_previous_end_ms != previous_end_ms


def _exchange_day_start(timestamp: datetime) -> datetime:
    local = timestamp.astimezone(_ET)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        timestamp.tzinfo
    )


def _is_regular_session_bar(bar: Bar) -> bool:
    local = bar.timestamp.astimezone(_ET)
    local_time = local.time().replace(tzinfo=None)
    return (
        local.weekday() < 5
        and _REGULAR_SESSION_OPEN <= local_time < _REGULAR_SESSION_CLOSE
    )


def _fetch_and_cache_bars(
    aggregator: Aggregator,
    cache: Cache,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
    history_start: datetime,
    now: datetime,
) -> None:
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
            )
            return
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
        )
        return

    coverage_start_ms, coverage_end_ms = coverage
    coverage_end = datetime.fromtimestamp(
        coverage_end_ms / 1000,
        tz=timezone.utc,
    )
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
        )
        return
    if requested_start_ms < coverage_start_ms:
        _fetch_bar_range(
            aggregator,
            cache,
            cache_namespace,
            symbol,
            timeframe,
            history_start,
            datetime.fromtimestamp(coverage_start_ms / 1000, tz=timezone.utc),
            coverage_end=datetime.fromtimestamp(
                coverage_start_ms / 1000,
                tz=timezone.utc,
            ),
        )
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
            else datetime.fromtimestamp(newest_ts / 1000, tz=timezone.utc)
        )
        refresh_start = _cached_tail_refresh_start(
            aggregator,
            symbol,
            timeframe,
            history_start,
            datetime.fromtimestamp(coverage_end_ms / 1000, tz=timezone.utc),
            newest_cached,
            now,
        )
        if refresh_start is None:
            return
        try:
            _fetch_bar_range(
                aggregator,
                cache,
                cache_namespace,
                symbol,
                timeframe,
                refresh_start,
                now,
                coverage_end=completed_end,
                require_bars=True,
            )
        except HistoryUnavailableError:
            return


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
) -> None:
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
        self._chart_load_timer = QTimer(self)
        self._chart_load_timer.setSingleShot(True)
        self._chart_load_timer.setInterval(_CHART_LOAD_DEBOUNCE_MS)
        self._chart_load_timer.timeout.connect(self._dispatch_pending_chart_load)

        self._snapshot_thread: QThread | None = None
        self._snapshot_worker: _SnapshotWorker | None = None
        self._level1_thread: QThread | None = None
        self._level1_worker: _Level1Worker | None = None
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
        self._bar_correction_notice: str | None = None

        # The symbol for which self._state.extensions currently holds state.
        # Used to save/restore per-symbol extension state on symbol switch.
        self._loaded_symbol: str | None = None
        self._per_symbol_state: dict[str, list[ChartExtensionState]] = {}

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

    def _load(self) -> None:
        """
        Kick off a background fetch for the current symbol and timeframe.
        Clears the chart immediately so the user sees a clean slate while
        loading, rather than stale data from the previous symbol.
        """
        if self._state.symbol is None:
            return

        self._chart.clear_all()
        self._chart.legend.clear_all()
        self._app_header.set_symbol(self._state.symbol)
        route = self._route_for_symbol(self._state.symbol)
        self._symbol_bar.set_data_source(route.display_name, "pending")

        request = self._make_chart_load_request(
            self._state.symbol,
            self._state.timeframe,
            route.connection,
        )
        self._current_chart_request = request
        self._terminal_chart_generation = None
        self._pending_chart_load = request
        self._chart_load_timer.start()

    def _make_chart_load_request(
        self,
        symbol: str,
        timeframe: Timeframe,
        connection: ProviderConnection,
    ) -> _ChartLoadRequest:
        self._chart_load_generation += 1
        return _ChartLoadRequest(
            generation=self._chart_load_generation,
            symbol=symbol,
            timeframe=timeframe,
            connection=connection,
        )

    def _dispatch_pending_chart_load(self) -> None:
        request = self._pending_chart_load
        if request is None or len(self._chart_load_tasks) >= _MAX_ACTIVE_CHART_LOADS:
            return
        self._pending_chart_load = None
        self._start_chart_load(request)

    def _start_chart_load(self, request: _ChartLoadRequest) -> None:
        route = self._route_for_symbol(request.symbol)
        if route.connection != request.connection:
            if (
                self._current_chart_request == request
                and self._terminal_chart_generation != request.generation
                and self._state.symbol == request.symbol
                and self._state.timeframe == request.timeframe
            ):
                self._terminal_chart_generation = request.generation
                self._on_fetch_error(
                    "The data source changed before the chart load started."
                )
            return

        worker = _FetchWorker(
            db_path=self._db_path,
            credential_store=self._credential_store,
            request=request,
        )
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_chart_fetch_done)
        worker.error.connect(self._on_chart_fetch_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(
            lambda generation=request.generation: self._on_chart_thread_finished(
                generation
            )
        )

        self._chart_load_tasks[request.generation] = _ChartLoadTask(
            request,
            thread,
            worker,
        )
        thread.start()

    def _on_chart_thread_finished(self, generation: int) -> None:
        task = self._chart_load_tasks.pop(generation, None)
        if task is None:
            return
        task.thread.deleteLater()
        if not self._chart_load_timer.isActive():
            self._dispatch_pending_chart_load()

    def _is_current_chart_request(self, request: _ChartLoadRequest) -> bool:
        if (
            self._current_chart_request != request
            or self._terminal_chart_generation == request.generation
            or self._state.symbol != request.symbol
            or self._state.timeframe != request.timeframe
        ):
            return False
        return self._route_for_symbol(request.symbol).connection == request.connection

    def _on_chart_fetch_done(
        self,
        request: _ChartLoadRequest,
        series: OHLCVSeries,
    ) -> None:
        if not self._is_current_chart_request(request):
            return
        self._terminal_chart_generation = request.generation
        self._on_fetch_done(series)

    def _on_chart_fetch_error(
        self,
        request: _ChartLoadRequest,
        message: str,
    ) -> None:
        if not self._is_current_chart_request(request):
            return
        self._terminal_chart_generation = request.generation
        self._on_fetch_error(message)

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
        route = self._route_for_symbol(series.symbol)
        self._symbol_bar.set_data_source(route.display_name, "connected")
        self._extension_runtime.set_cache_namespace(route.cache_namespace)
        self._extension_store.load_for_symbol(series.symbol)
        self._render(series)
        self._update_bar_correction_notice(series)
        self._symbol_bar.set_symbol(series.symbol)
        self._watchlist.set_active_symbol(series.symbol)
        self._refresh_level1()

    def _on_fetch_error(self, message: str) -> None:
        if self._state.symbol is not None:
            route = self._route_for_symbol(self._state.symbol)
            self._symbol_bar.set_data_source(route.display_name, "error")
        show_warning(self, "Load Error", message)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self, series: OHLCVSeries) -> None:
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
                show_warning(self, "Panel Limit Reached", str(exc))
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
            route = self._route_for_symbol(self._state.symbol)
            history_end = _history_end(
                self._state.timeframe,
                route.aggregator,
                now,
            )
            history_start = _history_start(
                self._state.timeframe,
                route.aggregator,
                history_end,
            )
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
        if draw_bars:
            self._update_bar_correction_notice(series)

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
        if self._validation_thread is not None and self._validation_thread.isRunning():
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
        )
        result = dialog.exec()
        if dialog.bars_changed():
            self._reload_extensions()
        if result == QDialog.DialogCode.Accepted:
            self._request_provider_activation(dialog.selected_connection_id())

    def _request_provider_activation(self, connection_id: str) -> None:
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
            show_warning(
                self,
                "Connection Failed",
                f"Could not configure {connection.display_name}.\n\n{exc}",
            )
            return

        self._pending_route = _DataRoute(Aggregator(provider), connection)
        worker = _ProviderValidationWorker(provider)
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_provider_validation_done)
        worker.error.connect(self._on_provider_validation_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)

        self._validation_thread = thread
        self._validation_worker = worker
        status_bar = self.statusBar()
        if status_bar is not None:
            status_bar.showMessage(f"Testing {connection.display_name}...")
        thread.start()

    def _on_provider_validation_done(self) -> None:
        route = self._pending_route
        self._pending_route = None
        if route is None:
            return
        self._commit_provider_route(route)

    def _on_provider_validation_error(self, message: str) -> None:
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

    def _stop_data_workers(self) -> None:
        if self._chart_load_timer.isActive():
            self._chart_load_timer.stop()
        self._pending_chart_load = None
        for task in tuple(self._chart_load_tasks.values()):
            if task.thread.isRunning():
                task.thread.quit()
                task.thread.wait()
        for thread in (
            self._snapshot_thread,
            self._level1_thread,
        ):
            if thread is not None and thread.isRunning():
                thread.quit()
                thread.wait()

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
        worker = _SnapshotWorker(
            [
                _SnapshotBatch(aggregator, batch_symbols)
                for aggregator, batch_symbols in symbols_by_namespace.values()
            ]
        )
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

    def _refresh_level1(self) -> None:
        if self._level1_thread is not None and self._level1_thread.isRunning():
            return
        if self._state.symbol is None:
            return

        symbol = self._state.symbol
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
        worker = _Level1Worker(
            route.aggregator,
            self._yahoo_route.aggregator,
            self._cache,
            symbol,
            refresh_reference,
            now,
        )
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_level1)
        worker.finished.connect(thread.quit)

        self._level1_thread = thread
        self._level1_worker = worker
        thread.start()

    def _on_level1(self, quote: Level1Quote | None) -> None:
        # A quote can arrive after the user has already switched symbols;
        # only the active symbol's quote may reach the header.
        if quote is None or quote.symbol != self._state.symbol:
            return
        self._app_header.set_quote(quote)

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
        self._stop_data_workers()
        if self._validation_thread and self._validation_thread.isRunning():
            self._validation_thread.quit()
            self._validation_thread.wait()
        self._cache.close()
        super().closeEvent(event)  # type: ignore[arg-type]
