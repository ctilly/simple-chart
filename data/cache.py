"""
data/cache.py

SQLite-backed cache for bar data and extension records.

The Cache class is the only part of the app that reads from or writes to
the database. Everything above this layer (extensions, chart, controller)
works with Bar and OHLCVSeries objects and never touches SQLite directly.

Responsibilities:
  - Initialize the database schema on first launch
  - Store and retrieve OHLCV bars (keyed by symbol + timeframe + timestamp)
  - Store, retrieve, update, and delete plugin-owned extension records

The controller checks the cache before calling a data provider. On a cache
miss (not enough bars), it fetches from the provider and calls put_bars()
to populate the cache for next time.
"""

import json
import math
import sqlite3
from collections.abc import Collection, Mapping
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from data.adjustment import BasisChange, BasisChangeChoice, BasisChangeRecord
from data.aggregator import resample
from data.calendar import MARKET_TIMEZONE, bar_session_key, session_date_anchor
from data.errors import UserFacingError
from data.models import (
    AssetReference,
    Bar,
    BarCorrection,
    BarInspection,
    ChartExtensionStoreRecord,
    SuspiciousBarCandidate,
    Timeframe,
)
from data.timestamps import ms_to_datetime
from data.session import (
    is_regular_session_timestamp,
    uses_regular_equity_session,
)
from data.provider.config import (
    FIXED_CONNECTION_IDS,
    ConnectionEnvironment,
    MarketDataFeed,
    ProviderConnection,
    YFINANCE_CONNECTION_ID,
    fixed_connections,
)


# Path to the DDL file, relative to this module.
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_MAX_TIMESTAMP_MS = 2**63 - 1
_BASIS_CHANGE_CHOICE_SETTING = "basis_change_choice"


class BarCorrectionError(UserFacingError, ValueError):
    """A bar correction or provider-bar refresh the user must change."""


class IdenticalCorrectionError(BarCorrectionError):
    """A bar correction whose values all match the provider bar."""


class Cache:
    """
    Wraps a SQLite connection and exposes read/write operations for bars
    and extension records.

    Usage:
        cache = Cache("/path/to/simplechart.db")
        bars = cache.get_bars("yfinance", "QQQ", Timeframe.MIN5, start_ts_ms, end_ts_ms)
        cache.put_bars("yfinance", "QQQ", Timeframe.MIN5, bars)
        cache.close()

    Or as a context manager:
        with Cache("/path/to/simplechart.db") as cache:
            ...
    """

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # Return rows as sqlite3.Row so columns are accessible by name.
        self._conn.row_factory = sqlite3.Row
        # WAL mode allows reads and writes to proceed concurrently without
        # blocking each other. Important when the chart is reading bars while
        # a background fetch is writing new ones.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        """Run schema.sql to create tables if they don't exist yet."""
        ddl = _SCHEMA_PATH.read_text()
        self._conn.executescript(ddl)
        self._migrate_bars_cache_namespace()
        self._migrate_watchlist_sort_order()
        self._ensure_fixed_provider_connections()

    def _migrate_bars_cache_namespace(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(bars)")
        }
        if "cache_namespace" in columns:
            return

        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE bars_with_namespace (
                    cache_namespace TEXT NOT NULL,
                    symbol          TEXT NOT NULL,
                    timeframe       TEXT NOT NULL,
                    timestamp       INTEGER NOT NULL,
                    open            REAL NOT NULL,
                    high            REAL NOT NULL,
                    low             REAL NOT NULL,
                    close           REAL NOT NULL,
                    volume          INTEGER NOT NULL,
                    vwap            REAL,
                    PRIMARY KEY (
                        cache_namespace, symbol, timeframe, timestamp
                    )
                )
                """
            )
            self._conn.execute(
                """
                INSERT INTO bars_with_namespace (
                    cache_namespace, symbol, timeframe, timestamp,
                    open, high, low, close, volume, vwap
                )
                SELECT 'yfinance', symbol, timeframe, timestamp,
                       open, high, low, close, volume, vwap
                FROM bars
                """
            )
            self._conn.execute("DROP TABLE bars")
            self._conn.execute("ALTER TABLE bars_with_namespace RENAME TO bars")

    def _migrate_watchlist_sort_order(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(watchlist)")
        }
        if "sort_order" in columns:
            return

        with self._conn:
            self._conn.execute(
                "ALTER TABLE watchlist ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
            )
            rows = self._conn.execute(
                "SELECT rowid, symbol FROM watchlist ORDER BY rowid ASC"
            ).fetchall()
            self._conn.executemany(
                "UPDATE watchlist SET sort_order = ? WHERE symbol = ?",
                [
                    (index, row["symbol"])
                    for index, row in enumerate(rows)
                ],
            )

    def _ensure_fixed_provider_connections(self) -> None:
        with self._conn:
            placeholders = ", ".join("?" for _ in FIXED_CONNECTION_IDS)
            self._conn.execute(
                f"DELETE FROM provider_connections "
                f"WHERE connection_id NOT IN ({placeholders})",
                FIXED_CONNECTION_IDS,
            )
            for sort_order, connection in enumerate(fixed_connections()):
                self._conn.execute(
                    """
                    INSERT INTO provider_connections (
                        connection_id, display_name, provider_name,
                        environment, feed, sort_order
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(connection_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        provider_name = excluded.provider_name,
                        environment = excluded.environment,
                        sort_order = excluded.sort_order
                    """,
                    (
                        connection.connection_id,
                        connection.display_name,
                        connection.provider_name,
                        connection.environment,
                        connection.feed,
                        sort_order,
                    ),
                )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO application_settings (
                    setting_key, setting_value
                )
                VALUES ('active_provider_connection', ?)
                """,
                (YFINANCE_CONNECTION_ID,),
            )
            self._conn.execute(
                f"""
                UPDATE application_settings
                SET setting_value = ?
                WHERE setting_key = 'active_provider_connection'
                  AND setting_value NOT IN ({placeholders})
                """,
                (YFINANCE_CONNECTION_ID, *FIXED_CONNECTION_IDS),
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Bars
    # ------------------------------------------------------------------

    def get_bar_cache_namespaces(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT cache_namespace FROM bars ORDER BY cache_namespace"
        )
        return [row["cache_namespace"] for row in rows]

    def get_bars(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        start_ts_ms: int,
        end_ts_ms: int,
    ) -> list[Bar]:
        """
        Return cached bars for symbol+timeframe in [start_ts_ms, end_ts_ms].

        Returns an empty list if no bars are cached for this range — the
        controller treats an empty result as a cache miss and fetches from
        the provider.

        Bars are returned oldest-first (ORDER BY timestamp ASC).
        """
        cursor = self._conn.execute(
            """
            SELECT
                bars.timestamp,
                COALESCE(bar_corrections.open, bars.open) AS open,
                COALESCE(bar_corrections.high, bars.high) AS high,
                COALESCE(bar_corrections.low, bars.low) AS low,
                COALESCE(bar_corrections.close, bars.close) AS close,
                COALESCE(bar_corrections.volume, bars.volume) AS volume,
                bars.open AS raw_open,
                bars.high AS raw_high,
                bars.low AS raw_low,
                bars.close AS raw_close,
                bars.volume AS raw_volume,
                bars.vwap,
                bar_corrections.timestamp AS correction_timestamp
            FROM bars
            LEFT JOIN bar_corrections USING (
                cache_namespace, symbol, timeframe, timestamp
            )
            WHERE bars.cache_namespace = ?
              AND bars.symbol = ?
              AND bars.timeframe = ?
              AND bars.timestamp >= ?
              AND bars.timestamp <= ?
            ORDER BY bars.timestamp ASC
            """,
            (cache_namespace, symbol, timeframe.value, start_ts_ms, end_ts_ms),
        )
        return [
            _row_to_effective_bar(row)
            for row in cursor
        ]

    def get_raw_bars(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        start_ts_ms: int,
        end_ts_ms: int,
    ) -> list[Bar]:
        cursor = self._conn.execute(
            """
            SELECT timestamp, open, high, low, close, volume, vwap
            FROM bars
            WHERE cache_namespace = ?
              AND symbol = ?
              AND timeframe = ?
              AND timestamp >= ?
              AND timestamp <= ?
            ORDER BY timestamp ASC
            """,
            (cache_namespace, symbol, timeframe.value, start_ts_ms, end_ts_ms),
        )
        return [_row_to_bar(row) for row in cursor]

    def put_bars(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        bars: list[Bar],
    ) -> None:
        """
        Insert or replace bars in the cache.

        INSERT OR REPLACE handles duplicates gracefully — if the provider
        returns a bar we already have (e.g. during a partial refresh), the
        existing row is updated rather than raising an error.

        All inserts for a single call are wrapped in one transaction for
        performance. Inserting bars one-by-one with auto-commit would be
        significantly slower for large fetches.
        """
        with self._conn:
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO bars
                    (cache_namespace, symbol, timeframe, timestamp,
                     open, high, low, close, volume, vwap)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        cache_namespace,
                        symbol,
                        timeframe.value,
                        _datetime_to_ms(bar.timestamp),
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                        bar.vwap,
                    )
                    for bar in bars
                ],
            )

    def replace_materialized_bars(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        start_ts_ms: int,
        end_ts_ms: int,
        bars: list[Bar],
        coverage_start_ts_ms: int,
        coverage_end_ts_ms: int,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                DELETE FROM bars
                WHERE cache_namespace = ?
                  AND symbol = ?
                  AND timeframe = ?
                  AND timestamp >= ?
                  AND timestamp <= ?
                """,
                (
                    cache_namespace,
                    symbol,
                    timeframe.value,
                    start_ts_ms,
                    end_ts_ms,
                ),
            )
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO bars
                    (cache_namespace, symbol, timeframe, timestamp,
                     open, high, low, close, volume, vwap)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        cache_namespace,
                        symbol,
                        timeframe.value,
                        _datetime_to_ms(bar.timestamp),
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.volume,
                        bar.vwap,
                    )
                    for bar in bars
                ],
            )
            self._conn.execute(
                """
                INSERT INTO bar_fetch_coverage (
                    cache_namespace, symbol, timeframe,
                    start_timestamp, end_timestamp
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_namespace, symbol, timeframe) DO UPDATE SET
                    start_timestamp = excluded.start_timestamp,
                    end_timestamp = excluded.end_timestamp
                """,
                (
                    cache_namespace,
                    symbol,
                    timeframe.value,
                    coverage_start_ts_ms,
                    coverage_end_ts_ms,
                ),
            )

    def rewind_symbol_history(
        self,
        cache_namespace: str,
        symbol: str,
        boundaries: Mapping[Timeframe, int],
        synthesized_timeframes: Collection[Timeframe],
    ) -> None:
        """Delete one symbol's bars at or after each timeframe's boundary.

        ``boundaries`` maps each timeframe to rewind to its boundary in UTC
        ms; timeframes not in it are untouched. Coverage of a native
        timeframe is removed so the next load refetches from scratch.
        Coverage of a synthesized timeframe is cut back to the part before
        its boundary, or removed when nothing before the boundary was
        covered. Corrections and extension records are kept. All changes
        happen in one transaction.
        """
        with self._conn:
            for timeframe, boundary_ms in boundaries.items():
                key = (cache_namespace, symbol, timeframe.value)
                self._conn.execute(
                    """
                    DELETE FROM bars
                    WHERE cache_namespace = ?
                      AND symbol = ?
                      AND timeframe = ?
                      AND timestamp >= ?
                    """,
                    (*key, boundary_ms),
                )
                if timeframe not in synthesized_timeframes:
                    self._conn.execute(
                        """
                        DELETE FROM bar_fetch_coverage
                        WHERE cache_namespace = ?
                          AND symbol = ?
                          AND timeframe = ?
                        """,
                        key,
                    )
                    continue
                self._conn.execute(
                    """
                    DELETE FROM bar_fetch_coverage
                    WHERE cache_namespace = ?
                      AND symbol = ?
                      AND timeframe = ?
                      AND start_timestamp >= ?
                    """,
                    (*key, boundary_ms),
                )
                self._conn.execute(
                    """
                    UPDATE bar_fetch_coverage
                    SET end_timestamp = MIN(end_timestamp, ?)
                    WHERE cache_namespace = ?
                      AND symbol = ?
                      AND timeframe = ?
                    """,
                    (boundary_ms, *key),
                )

    def refresh_provider_bar(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        original_timestamp: datetime,
        refreshed_bar: Bar,
        synthesized_timeframes: Collection[Timeframe] = (),
    ) -> None:
        original_timestamp_ms = _datetime_to_ms(original_timestamp)
        refreshed_timestamp_ms = _datetime_to_ms(refreshed_bar.timestamp)
        _validate_bar(refreshed_bar)
        if bar_session_key(original_timestamp, timeframe) != bar_session_key(
            refreshed_bar.timestamp,
            timeframe,
        ):
            raise BarCorrectionError(
                "The refreshed provider bar does not match the same bar session."
            )

        with self._conn:
            # Lock before reading so a background fetch cannot replace the
            # selected row between correction normalization and the writes.
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                """
                SELECT
                    bars.timestamp,
                    bar_corrections.timestamp AS correction_timestamp,
                    bar_corrections.open AS corrected_open,
                    bar_corrections.high AS corrected_high,
                    bar_corrections.low AS corrected_low,
                    bar_corrections.close AS corrected_close,
                    bar_corrections.volume AS corrected_volume
                FROM bars
                LEFT JOIN bar_corrections USING (
                    cache_namespace, symbol, timeframe, timestamp
                )
                WHERE bars.cache_namespace = ?
                  AND bars.symbol = ?
                  AND bars.timeframe = ?
                  AND bars.timestamp = ?
                """,
                (
                    cache_namespace,
                    symbol,
                    timeframe.value,
                    original_timestamp_ms,
                ),
            ).fetchone()
            if row is None:
                raise BarCorrectionError("The provider bar to refresh does not exist.")

            if refreshed_timestamp_ms != original_timestamp_ms:
                collision = self._conn.execute(
                    """
                    SELECT 1
                    FROM bars
                    WHERE cache_namespace = ?
                      AND symbol = ?
                      AND timeframe = ?
                      AND timestamp = ?
                    """,
                    (
                        cache_namespace,
                        symbol,
                        timeframe.value,
                        refreshed_timestamp_ms,
                    ),
                ).fetchone()
                if collision is not None:
                    raise BarCorrectionError(
                        "The refreshed provider timestamp already has a cached bar."
                    )

            correction_values: tuple[
                float | None,
                float | None,
                float | None,
                float | None,
                int | None,
            ] | None = None
            if row["correction_timestamp"] is not None:
                correction_values = (
                    None
                    if row["corrected_open"] == refreshed_bar.open
                    else row["corrected_open"],
                    None
                    if row["corrected_high"] == refreshed_bar.high
                    else row["corrected_high"],
                    None
                    if row["corrected_low"] == refreshed_bar.low
                    else row["corrected_low"],
                    None
                    if row["corrected_close"] == refreshed_bar.close
                    else row["corrected_close"],
                    None
                    if row["corrected_volume"] == refreshed_bar.volume
                    else row["corrected_volume"],
                )

            key = (
                cache_namespace,
                symbol,
                timeframe.value,
                original_timestamp_ms,
            )
            self._conn.execute(
                """
                DELETE FROM bar_corrections
                WHERE cache_namespace = ?
                  AND symbol = ?
                  AND timeframe = ?
                  AND timestamp = ?
                """,
                key,
            )
            self._conn.execute(
                """
                DELETE FROM bars
                WHERE cache_namespace = ?
                  AND symbol = ?
                  AND timeframe = ?
                  AND timestamp = ?
                """,
                key,
            )
            self._conn.execute(
                """
                INSERT INTO bars (
                    cache_namespace, symbol, timeframe, timestamp,
                    open, high, low, close, volume, vwap
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_namespace,
                    symbol,
                    timeframe.value,
                    refreshed_timestamp_ms,
                    refreshed_bar.open,
                    refreshed_bar.high,
                    refreshed_bar.low,
                    refreshed_bar.close,
                    refreshed_bar.volume,
                    refreshed_bar.vwap,
                ),
            )
            if correction_values is not None and any(
                value is not None for value in correction_values
            ):
                self._conn.execute(
                    """
                    INSERT INTO bar_corrections (
                        cache_namespace, symbol, timeframe, timestamp,
                        open, high, low, close, volume
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cache_namespace,
                        symbol,
                        timeframe.value,
                        refreshed_timestamp_ms,
                        *correction_values,
                    ),
                )
            for synthesized in synthesized_timeframes:
                self._resynthesize_session(
                    cache_namespace,
                    symbol,
                    timeframe,
                    synthesized,
                    refreshed_bar.timestamp,
                )

    def _resynthesize_session(
        self,
        cache_namespace: str,
        symbol: str,
        base_timeframe: Timeframe,
        timeframe: Timeframe,
        timestamp: datetime,
    ) -> None:
        """
        Rebuild a synthesized timeframe's bars for the exchange day holding
        timestamp from the base timeframe's raw bars, as chart loads build
        them. A day with no synthesized bars is left alone. Runs inside the
        caller's transaction.
        """
        day = timestamp.astimezone(MARKET_TIMEZONE).date()
        start_ms = _datetime_to_ms(
            datetime.combine(day, time.min, tzinfo=MARKET_TIMEZONE)
        )
        end_ms = _datetime_to_ms(
            datetime.combine(day + timedelta(days=1), time.min, tzinfo=MARKET_TIMEZONE)
        ) - 1
        key = (cache_namespace, symbol, timeframe.value)
        materialized = self._conn.execute(
            """
            SELECT 1 FROM bars
            WHERE cache_namespace = ?
              AND symbol = ?
              AND timeframe = ?
              AND timestamp >= ?
              AND timestamp <= ?
            LIMIT 1
            """,
            (*key, start_ms, end_ms),
        ).fetchone()
        if materialized is None:
            return
        base_bars = self.get_raw_bars(
            cache_namespace,
            symbol,
            base_timeframe,
            start_ms,
            end_ms,
        )
        if uses_regular_equity_session(symbol):
            base_bars = [
                bar for bar in base_bars if is_regular_session_timestamp(bar.timestamp)
            ]
        self._conn.execute(
            """
            DELETE FROM bars
            WHERE cache_namespace = ?
              AND symbol = ?
              AND timeframe = ?
              AND timestamp >= ?
              AND timestamp <= ?
            """,
            (*key, start_ms, end_ms),
        )
        self._conn.executemany(
            """
            INSERT INTO bars
                (cache_namespace, symbol, timeframe, timestamp,
                 open, high, low, close, volume, vwap)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    *key,
                    _datetime_to_ms(bar.timestamp),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    bar.vwap,
                )
                for bar in resample(base_bars, timeframe)
            ],
        )

    def get_bar_inspection(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        timestamp: datetime,
    ) -> BarInspection | None:
        timestamp_ms = _datetime_to_ms(timestamp)
        row = self._conn.execute(
            """
            SELECT
                bars.timestamp,
                bars.open AS raw_open,
                bars.high AS raw_high,
                bars.low AS raw_low,
                bars.close AS raw_close,
                bars.volume AS raw_volume,
                bars.vwap,
                bar_corrections.open AS corrected_open,
                bar_corrections.high AS corrected_high,
                bar_corrections.low AS corrected_low,
                bar_corrections.close AS corrected_close,
                bar_corrections.volume AS corrected_volume,
                bar_corrections.timestamp AS correction_timestamp
            FROM bars
            LEFT JOIN bar_corrections USING (
                cache_namespace, symbol, timeframe, timestamp
            )
            WHERE bars.cache_namespace = ?
              AND bars.symbol = ?
              AND bars.timeframe = ?
              AND bars.timestamp = ?
            """,
            (cache_namespace, symbol, timeframe.value, timestamp_ms),
        ).fetchone()
        if row is None:
            return None
        return _row_to_bar_inspection(
            row,
            cache_namespace,
            symbol,
            timeframe,
        )

    def get_bar_inspections_for_date(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        session_date: date,
    ) -> list[BarInspection]:
        start_date = session_date_anchor(session_date, timeframe)
        period_days = 7 if timeframe == Timeframe.WEEKLY else 1
        boundary_timezone = (
            MARKET_TIMEZONE if timeframe.is_intraday else timezone.utc
        )
        start = datetime.combine(
            start_date,
            time.min,
            tzinfo=boundary_timezone,
        )
        end = start + timedelta(days=period_days)
        rows = self._conn.execute(
            """
            SELECT
                bars.timestamp,
                bars.open AS raw_open,
                bars.high AS raw_high,
                bars.low AS raw_low,
                bars.close AS raw_close,
                bars.volume AS raw_volume,
                bars.vwap,
                bar_corrections.open AS corrected_open,
                bar_corrections.high AS corrected_high,
                bar_corrections.low AS corrected_low,
                bar_corrections.close AS corrected_close,
                bar_corrections.volume AS corrected_volume,
                bar_corrections.timestamp AS correction_timestamp
            FROM bars
            LEFT JOIN bar_corrections USING (
                cache_namespace, symbol, timeframe, timestamp
            )
            WHERE bars.cache_namespace = ?
              AND bars.symbol = ?
              AND bars.timeframe = ?
              AND bars.timestamp >= ?
              AND bars.timestamp < ?
            ORDER BY bars.timestamp ASC
            """,
            (
                cache_namespace,
                symbol,
                timeframe.value,
                _datetime_to_ms(start),
                _datetime_to_ms(end),
            ),
        )
        return [
            _row_to_bar_inspection(
                row,
                cache_namespace,
                symbol,
                timeframe,
            )
            for row in rows
        ]

    def find_suspicious_bars(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        minimum_deviation_percent: float,
    ) -> list[SuspiciousBarCandidate]:
        if not math.isfinite(minimum_deviation_percent):
            raise ValueError("Minimum deviation must be finite.")
        if minimum_deviation_percent < 0:
            raise ValueError("Minimum deviation cannot be negative.")
        rows = self._conn.execute(
            """
            SELECT
                bars.timestamp,
                bars.open AS raw_open,
                bars.high AS raw_high,
                bars.low AS raw_low,
                bars.close AS raw_close,
                bars.volume AS raw_volume,
                bars.vwap,
                bar_corrections.open AS corrected_open,
                bar_corrections.high AS corrected_high,
                bar_corrections.low AS corrected_low,
                bar_corrections.close AS corrected_close,
                bar_corrections.volume AS corrected_volume,
                bar_corrections.timestamp AS correction_timestamp
            FROM bars
            LEFT JOIN bar_corrections USING (
                cache_namespace, symbol, timeframe, timestamp
            )
            WHERE bars.cache_namespace = ?
              AND bars.symbol = ?
              AND bars.timeframe = ?
            ORDER BY bars.timestamp ASC
            """,
            (cache_namespace, symbol, timeframe.value),
        )
        candidates: list[SuspiciousBarCandidate] = []
        for row in rows:
            deviation_percent = _bar_deviation_values(
                row["raw_high"],
                row["raw_low"],
                row["raw_close"],
            )
            if deviation_percent < minimum_deviation_percent:
                continue
            inspection = _row_to_bar_inspection(
                row,
                cache_namespace,
                symbol,
                timeframe,
            )
            candidates.append(
                SuspiciousBarCandidate(inspection, deviation_percent)
            )
        return sorted(
            candidates,
            key=lambda candidate: candidate.deviation_percent,
            reverse=True,
        )

    def count_bar_correction_conflicts(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        start_ts_ms: int,
        end_ts_ms: int,
    ) -> int:
        rows = self._conn.execute(
            """
            SELECT
                bars.timestamp,
                bars.open AS raw_open,
                bars.high AS raw_high,
                bars.low AS raw_low,
                bars.close AS raw_close,
                bars.volume AS raw_volume,
                bars.vwap,
                bar_corrections.open AS corrected_open,
                bar_corrections.high AS corrected_high,
                bar_corrections.low AS corrected_low,
                bar_corrections.close AS corrected_close,
                bar_corrections.volume AS corrected_volume,
                bar_corrections.timestamp AS correction_timestamp
            FROM bars
            JOIN bar_corrections USING (
                cache_namespace, symbol, timeframe, timestamp
            )
            WHERE bars.cache_namespace = ?
              AND bars.symbol = ?
              AND bars.timeframe = ?
              AND bars.timestamp >= ?
              AND bars.timestamp <= ?
            """,
            (
                cache_namespace,
                symbol,
                timeframe.value,
                start_ts_ms,
                end_ts_ms,
            ),
        )
        return sum(
            _row_to_bar_inspection(
                row,
                cache_namespace,
                symbol,
                timeframe,
            ).correction_error
            is not None
            for row in rows
        )

    def put_bar_correction(self, correction: BarCorrection) -> None:
        """Replace the complete override set for one provider bar.

        ``None`` means that field should use the provider value. Values equal
        to the provider bar are normalized back to ``None`` before storage, so
        only actual differences persist. Callers editing an existing
        correction must therefore submit every override they intend to keep.
        """
        timestamp_ms = _datetime_to_ms(correction.timestamp)
        inspection = self.get_bar_inspection(
            correction.cache_namespace,
            correction.symbol,
            correction.timeframe,
            correction.timestamp,
        )
        if inspection is None:
            raise BarCorrectionError("The provider bar to correct does not exist.")
        if all(
            value is None
            for value in (
                correction.open,
                correction.high,
                correction.low,
                correction.close,
                correction.volume,
            )
        ):
            raise BarCorrectionError("A bar correction must override at least one field.")
        effective = _apply_bar_correction(inspection.raw_bar, correction)
        _validate_bar(effective)
        raw = inspection.raw_bar
        normalized = BarCorrection(
            cache_namespace=correction.cache_namespace,
            symbol=correction.symbol,
            timeframe=correction.timeframe,
            timestamp=correction.timestamp,
            open=None if effective.open == raw.open else effective.open,
            high=None if effective.high == raw.high else effective.high,
            low=None if effective.low == raw.low else effective.low,
            close=None if effective.close == raw.close else effective.close,
            volume=None if effective.volume == raw.volume else effective.volume,
        )
        if all(
            value is None
            for value in (
                normalized.open,
                normalized.high,
                normalized.low,
                normalized.close,
                normalized.volume,
            )
        ):
            raise IdenticalCorrectionError("A bar correction must differ from the provider bar.")
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO bar_corrections (
                    cache_namespace, symbol, timeframe, timestamp,
                    open, high, low, close, volume
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_namespace, symbol, timeframe, timestamp)
                DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume
                """,
                (
                    correction.cache_namespace,
                    correction.symbol,
                    correction.timeframe.value,
                    timestamp_ms,
                    normalized.open,
                    normalized.high,
                    normalized.low,
                    normalized.close,
                    normalized.volume,
                ),
            )

    def delete_bar_correction(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        timestamp: datetime,
    ) -> None:
        timestamp_ms = _datetime_to_ms(timestamp)
        with self._conn:
            self._conn.execute(
                """
                DELETE FROM bar_corrections
                WHERE cache_namespace = ?
                  AND symbol = ?
                  AND timeframe = ?
                  AND timestamp = ?
                """,
                (cache_namespace, symbol, timeframe.value, timestamp_ms),
            )

    def newest_cached_timestamp(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
    ) -> int | None:
        """
        Return the timestamp (UTC ms) of the most recent cached bar for
        symbol+timeframe, or None if no bars are cached.

        The controller uses this to determine the start point for an
        incremental fetch — rather than re-fetching the full history, it
        only requests bars newer than what's already cached.
        """
        row = self._conn.execute(
            """
            SELECT MAX(timestamp) FROM bars
            WHERE cache_namespace = ? AND symbol = ? AND timeframe = ?
            """,
            (cache_namespace, symbol, timeframe.value),
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def oldest_cached_timestamp(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        start_ts_ms: int = -(2**63),
    ) -> int | None:
        """The oldest cached bar's timestamp at or after start_ts_ms."""
        row = self._conn.execute(
            """
            SELECT MIN(timestamp) FROM bars
            WHERE cache_namespace = ? AND symbol = ? AND timeframe = ?
              AND timestamp >= ?
            """,
            (cache_namespace, symbol, timeframe.value, start_ts_ms),
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def get_bar_fetch_coverage(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
    ) -> tuple[int, int] | None:
        row = self._conn.execute(
            """
            SELECT start_timestamp, end_timestamp
            FROM bar_fetch_coverage
            WHERE cache_namespace = ? AND symbol = ? AND timeframe = ?
            """,
            (cache_namespace, symbol, timeframe.value),
        ).fetchone()
        if row is None:
            return None
        return (row["start_timestamp"], row["end_timestamp"])

    def extend_bar_fetch_coverage(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        start_ts_ms: int,
        end_ts_ms: int,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO bar_fetch_coverage (
                    cache_namespace, symbol, timeframe,
                    start_timestamp, end_timestamp
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_namespace, symbol, timeframe) DO UPDATE SET
                    start_timestamp = MIN(
                        start_timestamp, excluded.start_timestamp
                    ),
                    end_timestamp = MAX(end_timestamp, excluded.end_timestamp)
                """,
                (
                    cache_namespace,
                    symbol,
                    timeframe.value,
                    start_ts_ms,
                    end_ts_ms,
                ),
            )

    def replace_bar_fetch_coverage(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        start_ts_ms: int,
        end_ts_ms: int,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO bar_fetch_coverage (
                    cache_namespace, symbol, timeframe,
                    start_timestamp, end_timestamp
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_namespace, symbol, timeframe) DO UPDATE SET
                    start_timestamp = excluded.start_timestamp,
                    end_timestamp = excluded.end_timestamp
                """,
                (
                    cache_namespace,
                    symbol,
                    timeframe.value,
                    start_ts_ms,
                    end_ts_ms,
                ),
            )

    def begin_basis_change(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        dependent_timeframes: Collection[Timeframe],
        change: BasisChange,
        boundary_ms: int,
        shown_from_ms: int,
    ) -> None:
        """Prepare a native timeframe for re-download on a new basis.

        In one transaction, for the timeframe and the timeframes built from
        it: corrections at or after boundary_ms move to the new basis (up to
        the first bar the change says is already on it), and bars at or
        after boundary_ms and all coverage are deleted, so the next fetch
        starts cold. Pending basis changes of the timeframe move to the new
        basis too. When cached bars of the timeframe at or after
        shown_from_ms remain before the boundary, a basis_changes row
        records them as old-basis history from the old coverage start.
        """
        timeframes = (timeframe, *dependent_timeframes)
        corrections_end_ms = (
            change.new_basis_from_ms
            if change.new_basis_from_ms is not None
            else _MAX_TIMESTAMP_MS
        )
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            coverage = self.get_bar_fetch_coverage(cache_namespace, symbol, timeframe)
            for scaled in timeframes:
                key = (cache_namespace, symbol, scaled.value)
                self._scale_corrections(
                    key,
                    boundary_ms,
                    corrections_end_ms,
                    change.ratio,
                    change.volume_factor,
                )
                self._conn.execute(
                    """
                    DELETE FROM bars
                    WHERE cache_namespace = ?
                      AND symbol = ?
                      AND timeframe = ?
                      AND timestamp >= ?
                    """,
                    (*key, boundary_ms),
                )
                self._conn.execute(
                    """
                    DELETE FROM bar_fetch_coverage
                    WHERE cache_namespace = ? AND symbol = ? AND timeframe = ?
                    """,
                    key,
                )
            key = (cache_namespace, symbol, timeframe.value)
            self._conn.execute(
                """
                UPDATE basis_changes
                SET ratio = ratio * ?, volume_factor = volume_factor * ?
                WHERE cache_namespace = ? AND symbol = ? AND timeframe = ?
                """,
                (change.ratio, change.volume_factor, *key),
            )
            if coverage is None or coverage[0] >= boundary_ms:
                return
            shown_old_bar = self._conn.execute(
                """
                SELECT 1 FROM bars
                WHERE cache_namespace = ?
                  AND symbol = ?
                  AND timeframe = ?
                  AND timestamp >= ?
                  AND timestamp < ?
                LIMIT 1
                """,
                (*key, max(coverage[0], shown_from_ms), boundary_ms),
            ).fetchone()
            if shown_old_bar is None:
                return
            self._conn.execute(
                """
                INSERT OR REPLACE INTO basis_changes (
                    cache_namespace, symbol, timeframe, boundary_timestamp,
                    old_coverage_start, ratio, volume_factor
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *key,
                    boundary_ms,
                    coverage[0],
                    change.ratio,
                    change.volume_factor,
                ),
            )

    def _scale_corrections(
        self,
        key: tuple[str, str, str],
        start_ms: int,
        end_ms: int,
        ratio: float,
        volume_factor: float,
    ) -> None:
        """Move corrections in [start_ms, end_ms) to a new basis."""
        self._conn.execute(
            """
            UPDATE bar_corrections
            SET open = open * :ratio,
                high = high * :ratio,
                low = low * :ratio,
                close = close * :ratio,
                volume = CAST(ROUND(volume * :volume_factor) AS INTEGER)
            WHERE cache_namespace = :namespace
              AND symbol = :symbol
              AND timeframe = :timeframe
              AND timestamp >= :start
              AND timestamp < :end
            """,
            {
                "ratio": ratio,
                "volume_factor": volume_factor,
                "namespace": key[0],
                "symbol": key[1],
                "timeframe": key[2],
                "start": start_ms,
                "end": end_ms,
            },
        )

    def rescale_basis_changes(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        dependent_timeframes: Collection[Timeframe],
    ) -> None:
        """Move a timeframe's recorded old-basis history to the new basis.

        Records are applied newest first while each one ends where the
        timeframe's coverage starts: its bars and corrections, and those of
        the dependent timeframes, are rescaled and the coverage start moves
        back to the record's old coverage start. A record that no longer
        meets the coverage (something reset it since) is dropped with the
        bars it describes left as they are. All records of the timeframe
        are removed. One transaction.
        """
        key = (cache_namespace, symbol, timeframe.value)
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            coverage = self.get_bar_fetch_coverage(cache_namespace, symbol, timeframe)
            if coverage is not None:
                coverage_start_ms = coverage[0]
                records = self._conn.execute(
                    """
                    SELECT old_coverage_start, boundary_timestamp, ratio,
                           volume_factor
                    FROM basis_changes
                    WHERE cache_namespace = ? AND symbol = ? AND timeframe = ?
                    ORDER BY boundary_timestamp DESC
                    """,
                    key,
                ).fetchall()
                for record in records:
                    if record["boundary_timestamp"] != coverage_start_ms:
                        break
                    for scaled in (timeframe, *dependent_timeframes):
                        scaled_key = (cache_namespace, symbol, scaled.value)
                        self._scale_bars(
                            scaled_key,
                            record["old_coverage_start"],
                            record["boundary_timestamp"],
                            record["ratio"],
                            record["volume_factor"],
                        )
                        self._scale_corrections(
                            scaled_key,
                            record["old_coverage_start"],
                            record["boundary_timestamp"],
                            record["ratio"],
                            record["volume_factor"],
                        )
                    coverage_start_ms = record["old_coverage_start"]
                self._conn.execute(
                    """
                    UPDATE bar_fetch_coverage
                    SET start_timestamp = ?
                    WHERE cache_namespace = ? AND symbol = ? AND timeframe = ?
                    """,
                    (coverage_start_ms, *key),
                )
            self._delete_basis_changes(key)

    def discard_basis_changes(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        dependent_timeframes: Collection[Timeframe],
    ) -> None:
        """Delete a timeframe's recorded old-basis history.

        Bars before the newest record boundary, and their corrections, are
        deleted for the timeframe and its dependent timeframes; the provider
        no longer returns them, so the corrections could never apply again.
        One transaction.
        """
        key = (cache_namespace, symbol, timeframe.value)
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                """
                SELECT MAX(boundary_timestamp)
                FROM basis_changes
                WHERE cache_namespace = ? AND symbol = ? AND timeframe = ?
                """,
                key,
            ).fetchone()
            if row[0] is not None:
                for discarded in (timeframe, *dependent_timeframes):
                    for table in ("bars", "bar_corrections"):
                        self._conn.execute(
                            f"""
                            DELETE FROM {table}
                            WHERE cache_namespace = ?
                              AND symbol = ?
                              AND timeframe = ?
                              AND timestamp < ?
                            """,
                            (cache_namespace, symbol, discarded.value, row[0]),
                        )
            self._delete_basis_changes(key)

    def _delete_basis_changes(self, key: tuple[str, str, str]) -> None:
        self._conn.execute(
            """
            DELETE FROM basis_changes
            WHERE cache_namespace = ? AND symbol = ? AND timeframe = ?
            """,
            key,
        )

    def _scale_bars(
        self,
        key: tuple[str, str, str],
        start_ms: int,
        end_ms: int,
        ratio: float,
        volume_factor: float,
    ) -> None:
        """Move bars in [start_ms, end_ms) to a new basis."""
        self._conn.execute(
            """
            UPDATE bars
            SET open = open * :ratio,
                high = high * :ratio,
                low = low * :ratio,
                close = close * :ratio,
                vwap = vwap * :ratio,
                volume = CAST(ROUND(volume * :volume_factor) AS INTEGER)
            WHERE cache_namespace = :namespace
              AND symbol = :symbol
              AND timeframe = :timeframe
              AND timestamp >= :start
              AND timestamp < :end
            """,
            {
                "ratio": ratio,
                "volume_factor": volume_factor,
                "namespace": key[0],
                "symbol": key[1],
                "timeframe": key[2],
                "start": start_ms,
                "end": end_ms,
            },
        )

    def get_basis_change_choice(self) -> BasisChangeChoice | None:
        """The remembered answer to the basis-change question; None asks."""
        row = self._conn.execute(
            """
            SELECT setting_value
            FROM application_settings
            WHERE setting_key = ?
            """,
            (_BASIS_CHANGE_CHOICE_SETTING,),
        ).fetchone()
        return None if row is None else BasisChangeChoice(row[0])

    def set_basis_change_choice(self, choice: BasisChangeChoice | None) -> None:
        with self._conn:
            if choice is None:
                self._conn.execute(
                    "DELETE FROM application_settings WHERE setting_key = ?",
                    (_BASIS_CHANGE_CHOICE_SETTING,),
                )
                return
            self._conn.execute(
                """
                INSERT INTO application_settings (setting_key, setting_value)
                VALUES (?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value
                """,
                (_BASIS_CHANGE_CHOICE_SETTING, choice.value),
            )

    def get_basis_changes(
        self,
        cache_namespace: str,
        symbol: str,
    ) -> list[BasisChangeRecord]:
        rows = self._conn.execute(
            """
            SELECT timeframe, old_coverage_start, boundary_timestamp,
                   ratio, volume_factor
            FROM basis_changes
            WHERE cache_namespace = ? AND symbol = ?
            ORDER BY timeframe, boundary_timestamp
            """,
            (cache_namespace, symbol),
        )
        return [
            BasisChangeRecord(
                cache_namespace,
                symbol,
                Timeframe(row["timeframe"]),
                row["old_coverage_start"],
                row["boundary_timestamp"],
                row["ratio"],
                row["volume_factor"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Watchlist
    # ------------------------------------------------------------------

    def get_watchlist(self) -> list[str]:
        """Return watchlist symbols in user-defined order."""
        cursor = self._conn.execute(
            "SELECT symbol FROM watchlist ORDER BY sort_order ASC, symbol ASC"
        )
        return [row[0] for row in cursor]

    def add_to_watchlist(self, symbol: str) -> None:
        """Add a symbol to the watchlist. No-op if already present."""
        row = self._conn.execute(
            "SELECT MAX(sort_order) FROM watchlist"
        ).fetchone()
        next_order = 0 if row[0] is None else int(row[0]) + 1
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO watchlist (symbol, sort_order) VALUES (?, ?)",
                (symbol, next_order),
            )

    def remove_from_watchlist(self, symbol: str) -> None:
        """Remove a symbol from the watchlist."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM watchlist WHERE symbol = ?",
                (symbol,),
            )

    def reorder_watchlist(self, symbols: list[str]) -> None:
        """Persist the user-defined order for the current watchlist symbols."""
        with self._conn:
            self._conn.executemany(
                "UPDATE watchlist SET sort_order = ? WHERE symbol = ?",
                [
                    (index, symbol)
                    for index, symbol in enumerate(symbols)
                ],
            )

    # ------------------------------------------------------------------
    # Asset reference
    # ------------------------------------------------------------------

    def get_asset_reference(self, symbol: str) -> AssetReference | None:
        row = self._conn.execute(
            """
            SELECT symbol, company_name, refreshed_at
            FROM asset_reference
            WHERE symbol = ?
            """,
            (symbol.strip().upper(),),
        ).fetchone()
        if row is None:
            return None
        return AssetReference(
            symbol=row["symbol"],
            company_name=row["company_name"],
            refreshed_at=_ms_to_datetime(row["refreshed_at"]),
        )

    def put_asset_reference(
        self,
        symbol: str,
        company_name: str,
        refreshed_at: datetime,
    ) -> None:
        normalized = symbol.strip().upper()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO asset_reference (symbol, company_name, refreshed_at)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    company_name = excluded.company_name,
                    refreshed_at = excluded.refreshed_at
                """,
                (normalized, company_name, _datetime_to_ms(refreshed_at)),
            )

    # ------------------------------------------------------------------
    # Provider connections
    # ------------------------------------------------------------------

    def get_provider_connections(self) -> list[ProviderConnection]:
        cursor = self._conn.execute(
            """
            SELECT connection_id, display_name, provider_name, environment, feed
            FROM provider_connections
            ORDER BY sort_order ASC, display_name ASC
            """
        )
        return [_row_to_provider_connection(row) for row in cursor]

    def get_provider_connection(
        self,
        connection_id: str,
    ) -> ProviderConnection | None:
        row = self._conn.execute(
            """
            SELECT connection_id, display_name, provider_name, environment, feed
            FROM provider_connections
            WHERE connection_id = ?
            """,
            (connection_id,),
        ).fetchone()
        return None if row is None else _row_to_provider_connection(row)

    def set_provider_connection_feed(
        self,
        connection_id: str,
        feed: MarketDataFeed,
    ) -> None:
        connection = self.get_provider_connection(connection_id)
        if connection is None or connection.provider_name != "alpaca":
            raise ValueError(f"Connection {connection_id!r} is not an Alpaca connection.")
        with self._conn:
            self._conn.execute(
                "UPDATE provider_connections SET feed = ? WHERE connection_id = ?",
                (feed, connection_id),
            )

    def get_active_provider_connection_id(self) -> str:
        row = self._conn.execute(
            """
            SELECT setting_value
            FROM application_settings
            WHERE setting_key = 'active_provider_connection'
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("The active provider connection is not configured.")
        return str(row[0])

    def set_active_provider_connection_id(self, connection_id: str) -> None:
        if self.get_provider_connection(connection_id) is None:
            raise ValueError(f"Unknown provider connection: {connection_id}")
        with self._conn:
            self._conn.execute(
                """
                UPDATE application_settings
                SET setting_value = ?
                WHERE setting_key = 'active_provider_connection'
                """,
                (connection_id,),
            )

    # ------------------------------------------------------------------
    # ChartExtension records
    # ------------------------------------------------------------------

    def get_extension_records(
        self,
        store_key: str,
        symbol: str,
    ) -> list[ChartExtensionStoreRecord]:
        cursor = self._conn.execute(
            """
            SELECT record_id, store_key, symbol, sort_key, payload
            FROM extension_records
            WHERE store_key = ?
              AND symbol = ?
            ORDER BY sort_key ASC, record_id ASC
            """,
            (store_key, symbol),
        )
        return [_row_to_extension_record(row) for row in cursor]

    def put_extension_record(
        self,
        store_key: str,
        symbol: str,
        sort_key: int,
        payload: dict[str, Any],
    ) -> ChartExtensionStoreRecord:
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO extension_records (
                    store_key, symbol, sort_key, payload
                )
                VALUES (?, ?, ?, ?)
                """,
                (store_key, symbol, sort_key, json.dumps(payload)),
            )
        record_id = cursor.lastrowid
        if record_id is None:
            raise RuntimeError("SQLite did not return an extension record id.")
        return ChartExtensionStoreRecord(
            record_id=record_id,
            store_key=store_key,
            symbol=symbol,
            sort_key=sort_key,
            payload=dict(payload),
        )

    def update_extension_record(
        self,
        record_id: int,
        sort_key: int,
        payload: dict[str, Any],
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                UPDATE extension_records
                SET sort_key = ?,
                    payload = ?
                WHERE record_id = ?
                """,
                (sort_key, json.dumps(payload), record_id),
            )

    def delete_extension_record(self, record_id: int) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM extension_records WHERE record_id = ?",
                (record_id,),
            )


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------

def _datetime_to_ms(dt: datetime) -> int:
    """Convert a UTC-aware datetime to a Unix millisecond timestamp."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("Bar timestamps must be timezone-aware.")
    return int(dt.timestamp() * 1000)


def _ms_to_datetime(ms: int) -> datetime:
    """Convert a Unix millisecond timestamp to a UTC-aware datetime."""
    return ms_to_datetime(ms)


def _row_to_bar(row: sqlite3.Row) -> Bar:
    return Bar(
        timestamp=_ms_to_datetime(row["timestamp"]),
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
        vwap=row["vwap"],
    )


def _row_to_effective_bar(
    row: sqlite3.Row,
) -> Bar:
    bar = _row_to_bar(row)
    if row["correction_timestamp"] is None:
        return bar
    try:
        _validate_bar(bar)
    except ValueError:
        return _row_to_raw_bar(row)
    return bar


def _row_to_bar_inspection(
    row: sqlite3.Row,
    cache_namespace: str,
    symbol: str,
    timeframe: Timeframe,
) -> BarInspection:
    timestamp = _ms_to_datetime(row["timestamp"])
    raw_bar = _row_to_raw_bar(row)

    correction = None
    if row["correction_timestamp"] is not None:
        correction = BarCorrection(
            cache_namespace=cache_namespace,
            symbol=symbol,
            timeframe=timeframe,
            timestamp=timestamp,
            open=row["corrected_open"],
            high=row["corrected_high"],
            low=row["corrected_low"],
            close=row["corrected_close"],
            volume=row["corrected_volume"],
        )
    effective_bar = (
        raw_bar
        if correction is None
        else _apply_bar_correction(raw_bar, correction)
    )
    correction_error = None
    if correction is not None:
        try:
            _validate_bar(effective_bar)
        except BarCorrectionError as exc:
            correction_error = str(exc)
    return BarInspection(
        cache_namespace=cache_namespace,
        symbol=symbol,
        timeframe=timeframe,
        raw_bar=raw_bar,
        effective_bar=effective_bar,
        correction=correction,
        correction_error=correction_error,
    )


def _row_to_raw_bar(row: sqlite3.Row) -> Bar:
    return Bar(
        timestamp=_ms_to_datetime(row["timestamp"]),
        open=row["raw_open"],
        high=row["raw_high"],
        low=row["raw_low"],
        close=row["raw_close"],
        volume=row["raw_volume"],
        vwap=row["vwap"],
    )


def _apply_bar_correction(raw: Bar, correction: BarCorrection) -> Bar:
    return Bar(
        timestamp=raw.timestamp,
        open=raw.open if correction.open is None else correction.open,
        high=raw.high if correction.high is None else correction.high,
        low=raw.low if correction.low is None else correction.low,
        close=raw.close if correction.close is None else correction.close,
        volume=raw.volume if correction.volume is None else correction.volume,
        vwap=raw.vwap,
    )


def _validate_bar(bar: Bar) -> None:
    prices = (bar.open, bar.high, bar.low, bar.close)
    if not all(math.isfinite(value) and value > 0 for value in prices):
        raise BarCorrectionError("Corrected prices must be positive and finite.")
    if bar.high < max(bar.open, bar.close):
        raise BarCorrectionError("Corrected high cannot be below open or close.")
    if bar.low > min(bar.open, bar.close):
        raise BarCorrectionError("Corrected low cannot be above open or close.")
    if (
        not isinstance(bar.volume, int)
        or isinstance(bar.volume, bool)
        or bar.volume < 0
    ):
        raise BarCorrectionError("Corrected volume must be a nonnegative integer.")


def _bar_deviation_values(high: float, low: float, close: float) -> float:
    if high <= 0 or low <= 0 or close <= 0:
        return math.inf
    return max(
        high / close - 1.0,
        close / low - 1.0,
    ) * 100.0


def _row_to_extension_record(row: sqlite3.Row) -> ChartExtensionStoreRecord:
    payload = json.loads(row["payload"])
    if not isinstance(payload, dict):
        raise ValueError("ChartExtension record payload must be a JSON object.")
    return ChartExtensionStoreRecord(
        record_id=row["record_id"],
        store_key=row["store_key"],
        symbol=row["symbol"],
        sort_key=row["sort_key"],
        payload=payload,
    )


def _row_to_provider_connection(row: sqlite3.Row) -> ProviderConnection:
    environment_value = row["environment"]
    feed_value = row["feed"]
    return ProviderConnection(
        connection_id=row["connection_id"],
        display_name=row["display_name"],
        provider_name=row["provider_name"],
        environment=(
            None
            if environment_value is None
            else ConnectionEnvironment(environment_value)
        ),
        feed=None if feed_value is None else MarketDataFeed(feed_value),
    )
