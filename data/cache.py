"""
data/cache.py

SQLite-backed cache for bar data and AVWAP anchors.

The Cache class is the only part of the app that reads from or writes to
the database. Everything above this layer (indicators, chart, controller)
works with Bar and OHLCVSeries objects and never touches SQLite directly.

Responsibilities:
  - Initialize the database schema on first launch
  - Store and retrieve OHLCV bars (keyed by symbol + timeframe + timestamp)
  - Store, retrieve, and delete AVWAP anchor records

The controller checks the cache before calling a data provider. On a cache
miss (not enough bars), it fetches from the provider and calls put_bars()
to populate the cache for next time.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from data.models import AnchorRecord, Bar, OHLCVSeries, Timeframe


# Path to the DDL file, relative to this module.
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class Cache:
    """
    Wraps a SQLite connection and exposes read/write operations for bars
    and AVWAP anchors.

    Usage:
        cache = Cache("/path/to/simplechart.db")
        bars = cache.get_bars("QQQ", Timeframe.MIN5, start_ts_ms, end_ts_ms)
        cache.put_bars("QQQ", Timeframe.MIN5, bars)
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
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the initial schema release."""
        for stmt in (
            "ALTER TABLE avwap_anchors ADD COLUMN line_width REAL NOT NULL DEFAULT 2.0",
            "ALTER TABLE avwap_anchors ADD COLUMN line_style TEXT NOT NULL DEFAULT 'solid'",
            "ALTER TABLE avwap_anchors ADD COLUMN show_anchor INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists
        self._conn.commit()

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
            WHERE symbol    = ?
              AND timeframe  = ?
              AND timestamp >= ?
              AND timestamp <= ?
            ORDER BY timestamp ASC
            """,
            (symbol, timeframe.value, start_ts_ms, end_ts_ms),
        )
        return [_row_to_bar(row) for row in cursor]

    def put_bars(
        self,
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
                    (symbol, timeframe, timestamp, open, high, low, close, volume, vwap)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
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
            WHERE symbol = ? AND timeframe = ?
            """,
            (symbol, timeframe.value),
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    # ------------------------------------------------------------------
    # Watchlist
    # ------------------------------------------------------------------

    def get_watchlist(self) -> list[str]:
        """Return watchlist symbols in insertion order."""
        cursor = self._conn.execute(
            "SELECT symbol FROM watchlist ORDER BY rowid ASC"
        )
        return [row[0] for row in cursor]

    def add_to_watchlist(self, symbol: str) -> None:
        """Add a symbol to the watchlist. No-op if already present."""
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO watchlist (symbol) VALUES (?)",
                (symbol,),
            )

    def remove_from_watchlist(self, symbol: str) -> None:
        """Remove a symbol from the watchlist."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM watchlist WHERE symbol = ?",
                (symbol,),
            )

    # ------------------------------------------------------------------
    # AVWAP anchors
    # ------------------------------------------------------------------

    def get_anchors(self, symbol: str) -> list[AnchorRecord]:
        """
        Return all AVWAP anchors for a symbol, ordered by anchor time.

        Called on symbol load so the chart can draw all persisted anchors
        immediately without requiring user interaction.
        """
        cursor = self._conn.execute(
            """
            SELECT anchor_id, symbol, anchor_ts, label, color, line_width, line_style, show_anchor
            FROM avwap_anchors
            WHERE symbol = ?
            ORDER BY anchor_ts ASC
            """,
            (symbol,),
        )
        return [_row_to_anchor(row) for row in cursor]

    def put_anchor(self, anchor: AnchorRecord) -> AnchorRecord:
        """
        Persist an AVWAP anchor and return it with anchor_id set.

        The returned AnchorRecord is a new object — the input is not mutated
        (AnchorRecord is a dataclass but not frozen, however mutating the
        caller's object would be surprising behavior).
        """
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO avwap_anchors (
                    symbol, anchor_ts, label, color,
                    line_width, line_style, show_anchor
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (anchor.symbol, anchor.anchor_ts, anchor.label, anchor.color,
                 anchor.line_width, anchor.line_style, int(anchor.show_anchor)),
            )
        return AnchorRecord(
            symbol=anchor.symbol,
            anchor_ts=anchor.anchor_ts,
            label=anchor.label,
            color=anchor.color,
            line_width=anchor.line_width,
            line_style=anchor.line_style,
            show_anchor=anchor.show_anchor,
            anchor_id=cursor.lastrowid,
        )

    def delete_anchor(self, anchor_id: int) -> None:
        """Remove an AVWAP anchor by its database ID."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM avwap_anchors WHERE anchor_id = ?",
                (anchor_id,),
            )

    def update_anchor(self, anchor: AnchorRecord) -> None:
        """
        Update an existing anchor.

        Called when the user changes display properties via the indicator
        config dialog or drags the anchor to a different candle.
        anchor.anchor_id must be set (i.e. the anchor was previously
        returned by put_anchor or get_anchors).
        """
        if anchor.anchor_id is None:
            raise ValueError("Cannot update an anchor that has not been persisted.")
        with self._conn:
            self._conn.execute(
                """
                UPDATE avwap_anchors
                SET anchor_ts = ?,
                    label = ?,
                    color = ?,
                    line_width = ?,
                    line_style = ?,
                    show_anchor = ?
                WHERE anchor_id = ?
                """,
                (anchor.anchor_ts, anchor.label, anchor.color, anchor.line_width,
                 anchor.line_style, int(anchor.show_anchor),
                 anchor.anchor_id),
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


def _row_to_anchor(row: sqlite3.Row) -> AnchorRecord:
    return AnchorRecord(
        anchor_id=row["anchor_id"],
        symbol=row["symbol"],
        anchor_ts=row["anchor_ts"],
        label=row["label"],
        color=row["color"],
        line_width=row["line_width"],
        line_style=row["line_style"],
        show_anchor=bool(row["show_anchor"]),
    )
