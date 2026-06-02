import sqlite3
from pathlib import Path

from data.cache import Cache


def test_watchlist_adds_symbols_in_append_order(tmp_path: Path) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.add_to_watchlist("SPY")
        cache.add_to_watchlist("QQQ")
        cache.add_to_watchlist("IWM")

        symbols = cache.get_watchlist()

    assert symbols == ["SPY", "QQQ", "IWM"]


def test_watchlist_reorder_persists_user_order(tmp_path: Path) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.add_to_watchlist("SPY")
        cache.add_to_watchlist("QQQ")
        cache.add_to_watchlist("IWM")

        cache.reorder_watchlist(["IWM", "SPY", "QQQ"])

    with Cache(str(tmp_path / "test.db")) as cache:
        symbols = cache.get_watchlist()

    assert symbols == ["IWM", "SPY", "QQQ"]


def test_watchlist_duplicate_add_keeps_original_order(tmp_path: Path) -> None:
    with Cache(str(tmp_path / "test.db")) as cache:
        cache.add_to_watchlist("SPY")
        cache.add_to_watchlist("QQQ")
        cache.add_to_watchlist("SPY")

        symbols = cache.get_watchlist()

    assert symbols == ["SPY", "QQQ"]


def test_watchlist_sort_order_migration_preserves_legacy_rowid_order(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE watchlist (symbol TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO watchlist (symbol) VALUES (?)", ("SPY",))
        conn.execute("INSERT INTO watchlist (symbol) VALUES (?)", ("QQQ",))
        conn.execute("INSERT INTO watchlist (symbol) VALUES (?)", ("IWM",))
        conn.commit()
    finally:
        conn.close()

    with Cache(str(db_path)) as cache:
        symbols = cache.get_watchlist()

    assert symbols == ["SPY", "QQQ", "IWM"]
