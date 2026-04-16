-- data/schema.sql
--
-- SQLite schema for SimpleChart.
--
-- Two tables:
--   bars          — cached OHLCV data from the data provider
--   avwap_anchors — user-defined AVWAP anchor points, persisted across sessions
--
-- Run once on first launch via cache.py:init_db().


-- bars
-- ----------------------------------------------------------------------------
-- One row per bar. The composite primary key (symbol, timeframe, timestamp)
-- enforces uniqueness and doubles as the covering index for the most common
-- query: "give me all bars for QQQ at 5m between time A and time B."
--
-- timeframe stores the Timeframe enum's string value: "5m", "15m", "daily",
-- etc. Using the string value (rather than an integer code) keeps the data
-- human-readable when inspecting the database directly.
--
-- timestamp is UTC milliseconds since epoch, stored as an INTEGER. SQLite has
-- no native datetime type; integers are compact, fast to index, and avoid any
-- timezone serialization ambiguity.
--
-- vwap is nullable — not all providers supply it.

CREATE TABLE IF NOT EXISTS bars (
    symbol      TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL,
    timestamp   INTEGER NOT NULL,   -- UTC milliseconds
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      INTEGER NOT NULL,
    vwap        REAL,               -- nullable

    PRIMARY KEY (symbol, timeframe, timestamp)
);


-- avwap_anchors
-- ----------------------------------------------------------------------------
-- Stores user-defined AVWAP anchor points. Anchors persist across sessions
-- and work correctly when switching timeframes because they are stored as
-- UTC timestamps, never as bar indexes.
--
-- anchor_ts is UTC milliseconds, matching the format used in bars.timestamp
-- so the two can be compared directly (e.g. in timestamp_ms_to_bar_index).
--
-- label and color are display properties. label is shown in the chart legend;
-- color is a hex string (e.g. "#00FF88").
--
-- The index on (symbol, anchor_ts) supports the most common query: load all
-- anchors for a given symbol ordered by time.

CREATE TABLE IF NOT EXISTS avwap_anchors (
    anchor_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    anchor_ts   INTEGER NOT NULL,   -- UTC milliseconds
    label       TEXT    NOT NULL,
    color       TEXT    NOT NULL,   -- hex, e.g. "#00FF88"
    line_width  REAL    NOT NULL DEFAULT 1.0,
    line_style  TEXT    NOT NULL DEFAULT 'solid'
);

CREATE INDEX IF NOT EXISTS idx_avwap_anchors_symbol
    ON avwap_anchors (symbol, anchor_ts);


-- watchlist
-- ----------------------------------------------------------------------------
-- User-curated list of symbols for quick chart navigation.
-- Symbols are displayed in insertion order (ORDER BY rowid ASC).
-- The PRIMARY KEY on symbol prevents duplicates.

CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY
);
