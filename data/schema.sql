-- data/schema.sql
--
-- SQLite schema for SimpleChart.
--
-- Tables:
--   bars              — cached OHLCV data from the data provider
--   extension_records — plugin-owned persisted records
--   watchlist         — user-curated symbols
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


-- extension_records
-- ----------------------------------------------------------------------------
-- Generic persistence for extension plugins. The app stores and orders opaque
-- JSON payloads; each extension owns the payload schema behind its store_key.

CREATE TABLE IF NOT EXISTS extension_records (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_key TEXT    NOT NULL,
    symbol    TEXT    NOT NULL,
    sort_key  INTEGER NOT NULL,
    payload   TEXT    NOT NULL,

    UNIQUE (store_key, record_id)
);

CREATE INDEX IF NOT EXISTS idx_extension_records_store_symbol
    ON extension_records (store_key, symbol, sort_key);


-- watchlist
-- ----------------------------------------------------------------------------
-- User-curated list of symbols for quick chart navigation.
-- Symbols are displayed by sort_order ASC.
-- The PRIMARY KEY on symbol prevents duplicates.

CREATE TABLE IF NOT EXISTS watchlist (
    symbol     TEXT    PRIMARY KEY,
    sort_order INTEGER NOT NULL
);
