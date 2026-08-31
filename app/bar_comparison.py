from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from statistics import median
from typing import Literal

from data.calendar import bar_session_key
from data.models import Bar, Timeframe
from data.provider import (
    ProviderAvailability,
    ProviderConfigurationError,
    create_provider,
)
from data.provider.base import (
    DataProvider,
    MarketDataEntitlementError,
    UnsupportedTimeframeError,
)
from data.provider.config import (
    ConnectionEnvironment,
    MarketDataFeed,
    ProviderConnection,
    YFINANCE_CONNECTION_ID,
)
from data.provider.credentials import CredentialStore


BarField = Literal["open", "high", "low", "close", "volume"]
ProviderFactory = Callable[[ProviderConnection, CredentialStore], DataProvider]

_PRICE_FIELDS: tuple[BarField, ...] = ("open", "high", "low", "close")
_BAR_FIELDS: tuple[BarField, ...] = (*_PRICE_FIELDS, "volume")
_SYNTHESIZED_TIMEFRAMES = (Timeframe.MIN39, Timeframe.MIN65)
_CELL_RELATIVE_TOLERANCE = 0.0001
_CELL_ABSOLUTE_TOLERANCE = 0.01
_CORROBORATION_RELATIVE_TOLERANCE = 0.01
_CORROBORATION_DIRECTION_RATIO = 3.0
_SOURCE_LABELS = {
    "yfinance": "Yahoo Finance",
    "alpaca:iex": "Alpaca / IEX",
    "alpaca:delayed_sip": "Alpaca / SIP (15-minute delayed)",
    "alpaca:sip": "Alpaca / SIP (real-time)",
}


def _never_cancel() -> bool:
    return False


class BarComparisonRowKind(StrEnum):
    CACHED_ORIGIN = "cached_origin"
    REFRESHED_ORIGIN = "refreshed_origin"
    CORROBORATION = "corroboration"


@dataclass(frozen=True)
class BarComparisonRow:
    source_namespace: str
    label: str
    kind: BarComparisonRowKind
    bar: Bar | None
    error: str | None = None
    adjustment_basis_difference: bool = False


@dataclass(frozen=True)
class BarFieldSuggestion:
    field: BarField
    cached_value: float | int
    refreshed_value: float | int
    corroborating_sources: tuple[str, ...]


@dataclass(frozen=True)
class BarComparisonResult:
    rows: tuple[BarComparisonRow, ...]
    suggestions: tuple[BarFieldSuggestion, ...]
    origin_unchanged: bool


@dataclass(frozen=True)
class _SourceRequest:
    namespace: str
    label: str
    connection: ProviderConnection | None
    kind: BarComparisonRowKind


class BarComparisonService:
    def __init__(
        self,
        credential_store: CredentialStore,
        provider_connections: Sequence[ProviderConnection],
        provider_availability: Mapping[str, ProviderAvailability],
        provider_factory: ProviderFactory = create_provider,
        preferred_connection_id: str | None = None,
    ) -> None:
        self._credential_store = credential_store
        self._connections = tuple(provider_connections)
        self._availability = dict(provider_availability)
        self._provider_factory = provider_factory
        self._preferred_connection_id = preferred_connection_id

    def compare(
        self,
        origin_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        cached_bar: Bar,
        should_cancel: Callable[[], bool] = _never_cancel,
    ) -> BarComparisonResult:
        if timeframe in _SYNTHESIZED_TIMEFRAMES:
            raise ValueError(
                f"{timeframe.value} bars are synthesized and cannot be compared "
                "directly with provider bars."
            )

        requests = self._source_requests(origin_namespace)
        rows: list[BarComparisonRow] = [
            BarComparisonRow(
                origin_namespace,
                f"Cached {_source_label(origin_namespace)}",
                BarComparisonRowKind.CACHED_ORIGIN,
                cached_bar,
            )
        ]
        for request in requests:
            if should_cancel():
                raise InterruptedError
            rows.append(
                self._fetch_source(request, symbol, timeframe, cached_bar)
            )

        refreshed_bar = next(
            row.bar
            for row in rows
            if row.kind == BarComparisonRowKind.REFRESHED_ORIGIN
        )
        reference = cached_bar if refreshed_bar is None else refreshed_bar
        for index, row in enumerate(rows):
            if row.kind != BarComparisonRowKind.CORROBORATION or row.bar is None:
                continue
            rows[index] = replace(
                row,
                adjustment_basis_difference=_has_adjustment_basis_difference(
                    reference,
                    row.bar,
                ),
            )

        suggestions = (
            ()
            if refreshed_bar is None
            else _build_suggestions(cached_bar, refreshed_bar, rows)
        )
        return BarComparisonResult(
            rows=tuple(rows),
            suggestions=suggestions,
            origin_unchanged=refreshed_bar is not None and not suggestions,
        )

    def _source_requests(self, origin_namespace: str) -> list[_SourceRequest]:
        alpaca_connection = self._alpaca_connection()
        origin_connection = self._connection_for_namespace(
            origin_namespace,
            alpaca_connection,
        )
        requests = [
            _SourceRequest(
                origin_namespace,
                f"Refreshed {_source_label(origin_namespace)}",
                origin_connection,
                BarComparisonRowKind.REFRESHED_ORIGIN,
            )
        ]

        if alpaca_connection is not None:
            alternate_feeds: tuple[MarketDataFeed, ...]
            if origin_namespace == "alpaca:iex":
                alternate_feeds = (MarketDataFeed.DELAYED_SIP,)
            elif origin_namespace in ("alpaca:delayed_sip", "alpaca:sip"):
                alternate_feeds = (MarketDataFeed.IEX,)
            elif origin_namespace == "yfinance":
                alternate_feeds = (
                    MarketDataFeed.IEX,
                    MarketDataFeed.DELAYED_SIP,
                )
            else:
                alternate_feeds = ()
            for feed in alternate_feeds:
                connection = replace(alpaca_connection, feed=feed)
                requests.append(
                    _SourceRequest(
                        connection.cache_namespace,
                        _source_label(connection.cache_namespace),
                        connection,
                        BarComparisonRowKind.CORROBORATION,
                    )
                )

        if origin_namespace != "yfinance":
            yahoo = next(
                (
                    connection
                    for connection in self._connections
                    if connection.connection_id == YFINANCE_CONNECTION_ID
                ),
                None,
            )
            requests.append(
                _SourceRequest(
                    "yfinance",
                    _source_label("yfinance"),
                    yahoo,
                    BarComparisonRowKind.CORROBORATION,
                )
            )
        return requests

    def _alpaca_connection(self) -> ProviderConnection | None:
        availability = self._availability.get("alpaca")
        if availability is None or not availability.available:
            return None
        candidates = [
            connection
            for connection in self._connections
            if connection.provider_name == "alpaca"
        ]
        candidates.sort(
            key=lambda connection: (
                connection.connection_id != self._preferred_connection_id,
                connection.environment != ConnectionEnvironment.PAPER,
                connection.connection_id,
            )
        )
        for connection in candidates:
            try:
                credentials = self._credential_store.get(connection.connection_id)
            except RuntimeError:
                return None
            if credentials is not None:
                return connection
        return None

    def _connection_for_namespace(
        self,
        namespace: str,
        alpaca_connection: ProviderConnection | None,
    ) -> ProviderConnection | None:
        if namespace == "yfinance":
            return next(
                (
                    connection
                    for connection in self._connections
                    if connection.connection_id == YFINANCE_CONNECTION_ID
                ),
                None,
            )
        if alpaca_connection is None:
            return None
        feed_value = namespace.removeprefix("alpaca:")
        try:
            feed = MarketDataFeed(feed_value)
        except ValueError:
            return None
        return replace(alpaca_connection, feed=feed)

    def _fetch_source(
        self,
        request: _SourceRequest,
        symbol: str,
        timeframe: Timeframe,
        cached_bar: Bar,
    ) -> BarComparisonRow:
        if request.connection is None:
            return _failed_row(request, "This data source is unavailable.")
        availability = self._availability.get(request.connection.provider_name)
        if availability is None or not availability.available:
            return _failed_row(request, "This data source is unavailable.")
        try:
            provider = self._provider_factory(
                request.connection,
                self._credential_store,
            )
            if timeframe not in provider.native_timeframes():
                return _failed_row(
                    request,
                    "This source does not provide the selected timeframe.",
                )
            start, end = _request_window(cached_bar.timestamp, timeframe)
            bars = provider.fetch_bars(symbol, timeframe, start, end)
        except MarketDataEntitlementError:
            return _failed_row(
                request,
                "Market-data entitlement does not permit this request.",
            )
        except UnsupportedTimeframeError:
            return _failed_row(
                request,
                "This source does not provide the selected timeframe.",
            )
        except ProviderConfigurationError:
            return _failed_row(request, "This data source is unavailable.")
        except Exception:
            return _failed_row(request, "The provider request failed.")

        target_key = bar_session_key(cached_bar.timestamp, timeframe)
        try:
            matching = [
                bar
                for bar in bars
                if bar_session_key(bar.timestamp, timeframe) == target_key
            ]
        except ValueError:
            return _failed_row(request, "The provider returned an invalid timestamp.")
        if not matching:
            return _failed_row(request, "No matching bar was returned.")
        if len(matching) > 1:
            return _failed_row(request, "Multiple matching bars were returned.")
        return BarComparisonRow(
            request.namespace,
            request.label,
            request.kind,
            matching[0],
        )


def _request_window(
    timestamp: datetime,
    timeframe: Timeframe,
) -> tuple[datetime, datetime]:
    key = bar_session_key(timestamp, timeframe)
    if isinstance(key, date):
        start = datetime.combine(key, time.min, tzinfo=UTC)
        days = 7 if timeframe == Timeframe.WEEKLY else 1
        return (start, start + timedelta(days=days))
    minutes = 1 if timeframe == Timeframe.MIN1 else timeframe.minutes
    if minutes is None:
        raise ValueError(f"No request duration exists for {timeframe.value}.")
    duration = timedelta(minutes=minutes)
    target = timestamp.astimezone(UTC)
    return (target - duration, target + duration * 2)


def _build_suggestions(
    cached_bar: Bar,
    refreshed_bar: Bar,
    rows: Sequence[BarComparisonRow],
) -> tuple[BarFieldSuggestion, ...]:
    suggestions: list[BarFieldSuggestion] = []
    for field in _BAR_FIELDS:
        cached_value = _field_value(cached_bar, field)
        refreshed_value = _field_value(refreshed_bar, field)
        changed = (
            cached_value != refreshed_value
            if field == "volume"
            else _prices_differ(float(cached_value), float(refreshed_value))
        )
        if not changed:
            continue
        corroborating_sources: tuple[str, ...] = ()
        if field != "volume":
            corroborating_sources = tuple(
                row.source_namespace
                for row in rows
                if row.kind == BarComparisonRowKind.CORROBORATION
                and row.bar is not None
                and not row.adjustment_basis_difference
                and _directionally_corroborates(
                    float(cached_value),
                    float(refreshed_value),
                    float(_field_value(row.bar, field)),
                )
            )
        suggestions.append(
            BarFieldSuggestion(
                field,
                cached_value,
                refreshed_value,
                corroborating_sources,
            )
        )
    return tuple(suggestions)


def _prices_differ(origin: float, candidate: float) -> bool:
    tolerance = max(
        _CELL_ABSOLUTE_TOLERANCE,
        abs(origin) * _CELL_RELATIVE_TOLERANCE,
    )
    return abs(candidate - origin) > tolerance


def _directionally_corroborates(
    cached: float,
    refreshed: float,
    candidate: float,
) -> bool:
    refreshed_distance = abs(candidate - refreshed)
    cached_distance = abs(candidate - cached)
    near_tolerance = max(
        _CELL_ABSOLUTE_TOLERANCE,
        abs(refreshed) * _CORROBORATION_RELATIVE_TOLERANCE,
    )
    return (
        refreshed_distance <= near_tolerance
        and cached_distance
        >= _CORROBORATION_DIRECTION_RATIO * refreshed_distance
    )


def _has_adjustment_basis_difference(reference: Bar, candidate: Bar) -> bool:
    reference_prices = tuple(
        float(_field_value(reference, field)) for field in _PRICE_FIELDS
    )
    candidate_prices = tuple(
        float(_field_value(candidate, field)) for field in _PRICE_FIELDS
    )
    if any(value <= 0 for value in (*reference_prices, *candidate_prices)):
        return False
    ratios = tuple(
        candidate_value / reference_value
        for reference_value, candidate_value in zip(
            reference_prices,
            candidate_prices,
        )
    )
    ratio_midpoint = median(ratios)
    if abs(ratio_midpoint - 1.0) <= _CELL_RELATIVE_TOLERANCE:
        return False
    return all(
        abs(ratio / ratio_midpoint - 1.0) <= _CELL_RELATIVE_TOLERANCE
        for ratio in ratios
    )


def _field_value(bar: Bar, field: BarField) -> float | int:
    if field == "open":
        return bar.open
    if field == "high":
        return bar.high
    if field == "low":
        return bar.low
    if field == "close":
        return bar.close
    return bar.volume


def _failed_row(request: _SourceRequest, error: str) -> BarComparisonRow:
    return BarComparisonRow(
        request.namespace,
        request.label,
        request.kind,
        None,
        error,
    )


def _source_label(namespace: str) -> str:
    return _SOURCE_LABELS.get(namespace, namespace)
