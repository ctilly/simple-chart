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
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data.models import AssetReference, Bar, ChartExtensionStoreRecord, Timeframe
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
            SELECT timestamp, open, high, low, close, volume, vwap
            FROM bars
            WHERE cache_namespace = ?
              AND symbol    = ?
              AND timeframe  = ?
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
