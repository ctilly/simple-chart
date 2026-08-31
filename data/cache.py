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
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from data.calendar import MARKET_TIMEZONE, bar_session_key, session_date_anchor
from data.models import (
    AssetReference,
    Bar,
    BarCorrection,
    BarInspection,
    ChartExtensionStoreRecord,
    SuspiciousBarCandidate,
    Timeframe,
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

    def refresh_provider_bar(
        self,
        cache_namespace: str,
        symbol: str,
        timeframe: Timeframe,
        original_timestamp: datetime,
        refreshed_bar: Bar,
    ) -> None:
        original_timestamp_ms = _datetime_to_ms(original_timestamp)
        refreshed_timestamp_ms = _datetime_to_ms(refreshed_bar.timestamp)
        _validate_bar(refreshed_bar)
        if bar_session_key(original_timestamp, timeframe) != bar_session_key(
            refreshed_bar.timestamp,
            timeframe,
        ):
            raise ValueError(
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
                raise ValueError("The provider bar to refresh does not exist.")

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
                    raise ValueError(
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
            raise ValueError("The provider bar to correct does not exist.")
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
            raise ValueError("A bar correction must override at least one field.")
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
            raise ValueError("A bar correction must differ from the provider bar.")
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
    ) -> int | None:
        row = self._conn.execute(
            """
            SELECT MIN(timestamp) FROM bars
            WHERE cache_namespace = ? AND symbol = ? AND timeframe = ?
            """,
            (cache_namespace, symbol, timeframe.value),
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
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


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
        except ValueError as exc:
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
        raise ValueError("Corrected prices must be positive and finite.")
    if bar.high < max(bar.open, bar.close):
        raise ValueError("Corrected high cannot be below open or close.")
    if bar.low > min(bar.open, bar.close):
        raise ValueError("Corrected low cannot be above open or close.")
    if (
        not isinstance(bar.volume, int)
        or isinstance(bar.volume, bool)
        or bar.volume < 0
    ):
        raise ValueError("Corrected volume must be a nonnegative integer.")


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
