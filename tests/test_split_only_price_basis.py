import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from pytestqt.qtbot import QtBot

from app.controller import MainWindow, _basis_change_message
from data.adjustment import BasisChangeRecord
from data.cache import Cache
from data.models import Timeframe
from data.provider.registry import ProviderRegistry
from providers.yahoo.implementation import YFinanceProvider


_SYMBOL = "COST"
_COST_PRIOR_CLOSE = datetime(2026, 11, 23, tzinfo=UTC)
_TIMESTAMP_MS = int(_COST_PRIOR_CLOSE.timestamp() * 1000)
_BAR_INSERTS = {
    "bars": "INSERT INTO bars VALUES (?, ?, ?, ?, 1090, 1100, 1085, ?, 1000, NULL)",
    "bar_fetch_coverage": "INSERT INTO bar_fetch_coverage VALUES (?, ?, ?, ?, ?)",
    "basis_changes": "INSERT INTO basis_changes VALUES (?, ?, ?, ?, ?, 0.97, 1.0)",
    "bar_corrections": "INSERT INTO bar_corrections VALUES (?, ?, ?, ?, NULL, NULL, 1080, NULL, NULL)",
}
_PRESERVED_TABLES = ("extension_records", "watchlist", "provider_connections", "asset_reference")
_SCHEMA = Path(__file__).resolve().parents[1] / "data" / "schema.sql"


@pytest.mark.parametrize(
    ("timeframe", "interval", "timestamp", "close", "adj_close"),
    [
        (Timeframe.DAILY, "1d", _COST_PRIOR_CLOSE, 1097.91, 1067.91),
        (Timeframe.WEEKLY, "1wk", datetime(2026, 11, 16, tzinfo=UTC), 1080.0, 1050.0),
    ],
)
def test_yahoo_fetch_bars_uses_close_on_split_only_basis(
    monkeypatch: pytest.MonkeyPatch,
    timeframe: Timeframe,
    interval: str,
    timestamp: datetime,
    close: float,
    adj_close: float,
) -> None:
    frame = pd.DataFrame(
        {"Open": [1075.0], "High": [1100.0], "Low": [1070.0],
         "Close": [close], "Adj Close": [adj_close], "Volume": [1000]},
        index=[timestamp],
    )
    requests: list[tuple[str, bool]] = []

    class Ticker:
        def history(self, *, interval: str, auto_adjust: bool, **kwargs: object) -> pd.DataFrame:
            requests.append((interval, auto_adjust))
            if auto_adjust:
                adjusted = frame.drop(columns=["Adj Close"]).copy()
                prices = ["Open", "High", "Low", "Close"]
                adjusted[prices] = adjusted[prices] * (adj_close / close)
                return adjusted
            return frame

    monkeypatch.setattr("providers.yahoo.implementation.yf.Ticker", lambda symbol: Ticker())

    bars = YFinanceProvider().fetch_bars(_SYMBOL, timeframe, timestamp, timestamp + timedelta(days=7))

    assert [bar.close for bar in bars] == pytest.approx([close])
    assert (bars[0].open, bars[0].high, bars[0].low, bars[0].volume) == (1075, 1100, 1070, 1000)
    assert requests == [(interval, False)]


def _seed_bar_groups(
    db: sqlite3.Connection, groups: Iterable[tuple[str, str]], *, close: float,
) -> None:
    for namespace, timeframe in groups:
        key = (namespace, _SYMBOL, timeframe)
        db.execute(_BAR_INSERTS["bars"], (*key, _TIMESTAMP_MS, close))
        db.execute(_BAR_INSERTS["bar_fetch_coverage"], (*key, _TIMESTAMP_MS, _TIMESTAMP_MS + 1))
        db.execute(_BAR_INSERTS["basis_changes"], (*key, _TIMESTAMP_MS + 1, _TIMESTAMP_MS))
        db.execute(_BAR_INSERTS["bar_corrections"], (*key, _TIMESTAMP_MS))


def _bar_groups(db: sqlite3.Connection) -> dict[str, set[tuple[str, str]]]:
    return {
        table: set(db.execute(f"SELECT cache_namespace, timeframe FROM {table}"))
        for table in _BAR_INSERTS
    }


def _table_rows(db: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    return db.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()


@pytest.mark.usefixtures("isolated_credentials")
def test_startup_migrates_split_only_cache_once(
    tmp_path: Path, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, provider_registry: ProviderRegistry,
) -> None:
    path = str(tmp_path / "pre-migration.db")
    affected = {("yfinance", "daily"), ("yfinance", "weekly")} | {
        (namespace, timeframe.value)
        for namespace in ("alpaca:iex", "alpaca:delayed_sip", "alpaca:sip")
        for timeframe in Timeframe
    }
    retained = {
        ("yfinance", timeframe.value) for timeframe in Timeframe if timeframe.is_intraday
    } | {("other-source", "daily")}
    settings = {"active_provider_connection": "alpaca-paper", "show_dividends": "0"}

    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(_SCHEMA.read_text())
        _seed_bar_groups(db, affected | retained, close=1067.91)
        db.execute(
            "INSERT INTO extension_records VALUES (1, ?, ?, 0, ?)",
            ("horizontal_line", _SYMBOL, '{"price": 1067.91}'),
        )
        db.execute(
            "INSERT INTO extension_records VALUES (2, ?, ?, 0, ?)",
            ("avwap", _SYMBOL, '{"anchor_ts_ms": 1795392000000}'),
        )
        db.execute("INSERT INTO watchlist VALUES (?, 0)", (_SYMBOL,))
        db.execute("INSERT INTO asset_reference VALUES (?, 'Costco', ?, 'yfinance')", (_SYMBOL, _TIMESTAMP_MS))
        db.executemany("INSERT INTO application_settings VALUES (?, ?)", settings.items())
        for sort_order, connection_id in enumerate(provider_registry.connection_ids):
            options = {"feed": "delayed_sip"} if connection_id == "alpaca-paper" else None
            connection = provider_registry.build_connection(connection_id, options)
            db.execute(
                """
                INSERT INTO provider_connections
                    (connection_id, display_name, provider_name, sort_order, options)
                VALUES (?, ?, ?, ?, ?)
                """,
                (connection_id, connection.display_name, connection.provider_name,
                 sort_order, json.dumps(dict(connection.options))),
            )
        preserved = {table: _table_rows(db, table) for table in _PRESERVED_TABLES}

    groups_at_startup: list[dict[str, set[tuple[str, str]]]] = []

    def observe_first_load(window: MainWindow) -> None:
        with closing(sqlite3.connect(path)) as db:
            groups_at_startup.append(_bar_groups(db))

    def skip_snapshots(window: MainWindow) -> None:
        pass

    monkeypatch.setattr(MainWindow, "_load", observe_first_load)
    monkeypatch.setattr(MainWindow, "_refresh_watchlist_snapshots", skip_snapshots)
    window = MainWindow(path, provider_registry=provider_registry)
    qtbot.addWidget(window)
    window.close()

    assert groups_at_startup == [dict.fromkeys(_BAR_INSERTS, retained)]
    with closing(sqlite3.connect(path)) as db:
        assert {table: _table_rows(db, table) for table in _PRESERVED_TABLES} == preserved
        migrated_settings = dict(db.execute("SELECT * FROM application_settings"))
        assert {key: migrated_settings[key] for key in settings} == settings
        assert len(migrated_settings.keys() - settings.keys()) == 1
        retained_rows = {table: _table_rows(db, table) for table in _BAR_INSERTS}

    with Cache(path, registry=provider_registry), closing(sqlite3.connect(path)) as db:
        assert {table: _table_rows(db, table) for table in _BAR_INSERTS} == retained_rows
        with db:
            _seed_bar_groups(db, affected, close=1097.91)
        fresh_rows = {table: _table_rows(db, table) for table in _BAR_INSERTS}

    with Cache(path, registry=provider_registry), closing(sqlite3.connect(path)) as db:
        assert {table: _table_rows(db, table) for table in _BAR_INSERTS} == fresh_rows
        assert dict(db.execute("SELECT * FROM application_settings")) == migrated_settings


def test_split_only_cache_migration_runs_after_legacy_namespace_conversion(
    tmp_path: Path, provider_registry: ProviderRegistry,
) -> None:
    path = str(tmp_path / "legacy.db")
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(
            """
            CREATE TABLE bars (
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER NOT NULL,
                vwap REAL,
                PRIMARY KEY (symbol, timeframe, timestamp)
            );
            """
        )
        db.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, 1090, 1100, 1085, 1097.91, 1000, NULL)",
            [(_SYMBOL, timeframe, _TIMESTAMP_MS) for timeframe in ("daily", "5m")],
        )

    with Cache(path, registry=provider_registry), closing(sqlite3.connect(path)) as db:
        assert _bar_groups(db)["bars"] == {("yfinance", "5m")}


def test_split_only_cache_migration_rolls_back_without_setting_marker(
    tmp_path: Path, provider_registry: ProviderRegistry,
) -> None:
    path = str(tmp_path / "interrupted.db")
    group = {("yfinance", "daily")}
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(_SCHEMA.read_text())
        _seed_bar_groups(db, group, close=1067.91)
        db.executescript(
            """
            CREATE TRIGGER block_migration_marker
            BEFORE INSERT ON application_settings
            WHEN NEW.setting_key <> 'active_provider_connection'
            BEGIN
                SELECT RAISE(ABORT, 'test interrupted migration');
            END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="test interrupted migration"):
        with Cache(path, registry=provider_registry):
            pass

    with closing(sqlite3.connect(path)) as db, db:
        assert _bar_groups(db) == dict.fromkeys(_BAR_INSERTS, group)
        assert db.execute(
            "SELECT 1 FROM application_settings WHERE setting_key = 'split_only_bar_cache_migrated'"
        ).fetchone() is None
        db.execute("DROP TRIGGER block_migration_marker")

    with Cache(path, registry=provider_registry), closing(sqlite3.connect(path)) as db:
        assert _bar_groups(db) == dict.fromkeys(_BAR_INSERTS, set())


def test_basis_change_prompt_is_neutral_when_the_cause_is_uncertain() -> None:
    record = BasisChangeRecord(
        "yfinance", _SYMBOL, Timeframe.MIN5, _TIMESTAMP_MS, _TIMESTAMP_MS + 1, 0.97, 1.0,
    )

    message = _basis_change_message(_SYMBOL, record)

    assert "dividend" not in message.lower()
    assert "split" not in message.lower()
    assert "COST" in message and "0.97" in message
    assert "Rescale" in message and "discard" in message
