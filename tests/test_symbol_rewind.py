from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from data.cache import Cache
from data.models import Bar, BarCorrection, Timeframe


_NAMESPACE = "yfinance"
_OTHER_NAMESPACE = "alpaca:iex"
_SYMBOL = "AAPL"
_OTHER_SYMBOL = "MSFT"

# Yahoo serves about 55 days of intraday history. With "now" at
# 2026-11-06 the limit falls on 2026-09-12; the first session start at or
# after it is the Monday 2026-09-14 ET midnight.
_INTRADAY_BOUNDARY = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
_DAILY_BOUNDARY = datetime(2016, 1, 1, 5, 0, tzinfo=UTC)
_WEEKLY_BOUNDARY = datetime(2016, 1, 4, 5, 0, tzinfo=UTC)

_SEEDED_BARS: dict[Timeframe, list[datetime]] = {
    Timeframe.MIN5: [
        datetime(2026, 8, 3, 14, 30, tzinfo=UTC),
        datetime(2026, 9, 11, 19, 55, tzinfo=UTC),
        datetime(2026, 9, 14, 13, 30, tzinfo=UTC),
        datetime(2026, 11, 5, 20, 55, tzinfo=UTC),
    ],
    Timeframe.MIN15: [
        datetime(2026, 8, 3, 14, 30, tzinfo=UTC),
        datetime(2026, 11, 5, 20, 45, tzinfo=UTC),
    ],
    Timeframe.MIN39: [
        datetime(2026, 8, 3, 14, 30, tzinfo=UTC),
        datetime(2026, 9, 11, 19, 33, tzinfo=UTC),
        datetime(2026, 9, 14, 13, 30, tzinfo=UTC),
        datetime(2026, 11, 5, 14, 30, tzinfo=UTC),
    ],
    Timeframe.MIN65: [
        datetime(2026, 8, 3, 14, 30, tzinfo=UTC),
        datetime(2026, 9, 14, 13, 30, tzinfo=UTC),
        datetime(2026, 11, 5, 14, 30, tzinfo=UTC),
    ],
    Timeframe.DAILY: [
        datetime(2015, 12, 31, 5, 0, tzinfo=UTC),
        _DAILY_BOUNDARY,
        datetime(2026, 11, 5, 5, 0, tzinfo=UTC),
    ],
    Timeframe.WEEKLY: [
        datetime(2015, 12, 28, 5, 0, tzinfo=UTC),
        _WEEKLY_BOUNDARY,
        datetime(2026, 11, 2, 5, 0, tzinfo=UTC),
    ],
}

_KEPT_BARS: dict[Timeframe, list[datetime]] = {
    Timeframe.MIN5: _SEEDED_BARS[Timeframe.MIN5][:2],
    Timeframe.MIN15: _SEEDED_BARS[Timeframe.MIN15],
    Timeframe.MIN39: _SEEDED_BARS[Timeframe.MIN39][:2],
    Timeframe.MIN65: _SEEDED_BARS[Timeframe.MIN65][:1],
    Timeframe.DAILY: _SEEDED_BARS[Timeframe.DAILY][:1],
    Timeframe.WEEKLY: _SEEDED_BARS[Timeframe.WEEKLY][:1],
}


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


# MIN15 is deliberately absent: timeframes without a boundary are untouched.
# WEEKLY is last so a failure on its rows happens after earlier timeframes
# have already been rewound inside the transaction.
_BOUNDARIES: dict[Timeframe, int] = {
    Timeframe.MIN5: _ms(_INTRADAY_BOUNDARY),
    Timeframe.MIN39: _ms(_INTRADAY_BOUNDARY),
    Timeframe.MIN65: _ms(_INTRADAY_BOUNDARY),
    Timeframe.DAILY: _ms(_DAILY_BOUNDARY),
    Timeframe.WEEKLY: _ms(_WEEKLY_BOUNDARY),
}
_SYNTHESIZED = frozenset({Timeframe.MIN39, Timeframe.MIN65})

_COVERAGE_END = _ms(datetime(2026, 11, 5, 21, 0, tzinfo=UTC))
_SEEDED_COVERAGE: dict[Timeframe, tuple[int, int]] = {
    Timeframe.MIN5: (_ms(datetime(2026, 8, 1, 4, 0, tzinfo=UTC)), _COVERAGE_END),
    Timeframe.MIN15: (_ms(datetime(2026, 8, 1, 4, 0, tzinfo=UTC)), _COVERAGE_END),
    Timeframe.MIN39: (_ms(datetime(2026, 8, 1, 4, 0, tzinfo=UTC)), _COVERAGE_END),
    Timeframe.MIN65: (_ms(datetime(2026, 9, 14, 4, 0, tzinfo=UTC)), _COVERAGE_END),
    Timeframe.DAILY: (_ms(datetime(2015, 12, 1, 5, 0, tzinfo=UTC)), _COVERAGE_END),
    Timeframe.WEEKLY: (_ms(_WEEKLY_BOUNDARY), _COVERAGE_END),
}


def test_rewind_deletes_bars_at_or_after_each_boundary_and_keeps_older_bars(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    with Cache(str(db_path)) as cache:
        _seed_symbol(cache, _NAMESPACE, _SYMBOL)

        cache.rewind_symbol_history(_NAMESPACE, _SYMBOL, _BOUNDARIES, _SYNTHESIZED)

    assert _bar_timestamps(db_path, _NAMESPACE, _SYMBOL) == {
        timeframe: [_ms(timestamp) for timestamp in timestamps]
        for timeframe, timestamps in _KEPT_BARS.items()
    }


def test_rewind_keeps_yahoo_intraday_bars_older_than_the_source_limit(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    older_than_limit = datetime(2026, 8, 3, 14, 30, tzinfo=UTC)
    with Cache(str(db_path)) as cache:
        _seed_symbol(cache, _NAMESPACE, _SYMBOL)

        cache.rewind_symbol_history(_NAMESPACE, _SYMBOL, _BOUNDARIES, _SYNTHESIZED)

        kept = cache.get_raw_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN5,
            _ms(older_than_limit),
            _ms(older_than_limit),
        )

    assert kept == [_bar(older_than_limit)]


def test_rewind_leaves_other_symbols_namespaces_and_tables_unchanged(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    with Cache(str(db_path)) as cache:
        _seed_symbol(cache, _NAMESPACE, _SYMBOL)
        _seed_symbol(cache, _NAMESPACE, _OTHER_SYMBOL)
        _seed_symbol(cache, _OTHER_NAMESPACE, _SYMBOL)
        for timeframe, timestamps in (
            (Timeframe.MIN5, _SEEDED_BARS[Timeframe.MIN5]),
            (Timeframe.MIN39, _SEEDED_BARS[Timeframe.MIN39]),
            (Timeframe.DAILY, _SEEDED_BARS[Timeframe.DAILY]),
        ):
            for timestamp in (timestamps[0], timestamps[-1]):
                _correct_volume(cache, _NAMESPACE, _SYMBOL, timeframe, timestamp)
                _correct_volume(
                    cache, _NAMESPACE, _OTHER_SYMBOL, timeframe, timestamp
                )
                _correct_volume(
                    cache, _OTHER_NAMESPACE, _SYMBOL, timeframe, timestamp
                )
        cache.put_extension_record(
            "avwap",
            _SYMBOL,
            _ms(_SEEDED_BARS[Timeframe.MIN5][-1]),
            {"anchor_ms": _ms(_SEEDED_BARS[Timeframe.MIN5][-1])},
        )
        cache.put_extension_record(
            "vertical_line",
            _SYMBOL,
            _ms(_DAILY_BOUNDARY),
            {"timestamp_ms": _ms(_DAILY_BOUNDARY)},
        )
        unchanged_tables = {
            table: _table_rows(db_path, table)
            for table in _all_tables(db_path)
            if table not in {"bars", "bar_fetch_coverage"}
        }
        other_bars = _other_rows(db_path, "bars")
        other_coverage = _other_rows(db_path, "bar_fetch_coverage")

        cache.rewind_symbol_history(_NAMESPACE, _SYMBOL, _BOUNDARIES, _SYNTHESIZED)

    assert len(unchanged_tables["bar_corrections"]) == 18
    assert len(unchanged_tables["extension_records"]) == 2
    assert {
        table: _table_rows(db_path, table)
        for table in unchanged_tables
    } == unchanged_tables
    assert _other_rows(db_path, "bars") == other_bars
    assert _other_rows(db_path, "bar_fetch_coverage") == other_coverage
    assert len(other_bars) == 2 * sum(
        len(timestamps) for timestamps in _SEEDED_BARS.values()
    )


def test_rewind_removes_native_coverage_and_keeps_synthesized_prefix(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    with Cache(str(db_path)) as cache:
        _seed_symbol(cache, _NAMESPACE, _SYMBOL)

        cache.rewind_symbol_history(_NAMESPACE, _SYMBOL, _BOUNDARIES, _SYNTHESIZED)

        coverage = {
            timeframe: cache.get_bar_fetch_coverage(_NAMESPACE, _SYMBOL, timeframe)
            for timeframe in _SEEDED_COVERAGE
        }

    assert coverage == {
        # Native: removed whether coverage started before or at B.
        Timeframe.MIN5: None,
        Timeframe.DAILY: None,
        Timeframe.WEEKLY: None,
        # Synthesized, start < B: truthful prefix [start, B).
        Timeframe.MIN39: (
            _SEEDED_COVERAGE[Timeframe.MIN39][0],
            _ms(_INTRADAY_BOUNDARY),
        ),
        # Synthesized, start >= B: removed.
        Timeframe.MIN65: None,
        # No boundary given: untouched.
        Timeframe.MIN15: _SEEDED_COVERAGE[Timeframe.MIN15],
    }


def test_rewind_keeps_synthesized_coverage_that_already_ends_before_boundary(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    prefix = (
        _ms(datetime(2026, 8, 1, 4, 0, tzinfo=UTC)),
        _ms(datetime(2026, 9, 4, 4, 0, tzinfo=UTC)),
    )
    with Cache(str(db_path)) as cache:
        cache.put_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN39,
            [_bar(datetime(2026, 8, 3, 14, 30, tzinfo=UTC))],
        )
        cache.replace_bar_fetch_coverage(
            _NAMESPACE, _SYMBOL, Timeframe.MIN39, *prefix
        )

        cache.rewind_symbol_history(_NAMESPACE, _SYMBOL, _BOUNDARIES, _SYNTHESIZED)

        coverage_39m = cache.get_bar_fetch_coverage(
            _NAMESPACE, _SYMBOL, Timeframe.MIN39
        )
        coverage_65m = cache.get_bar_fetch_coverage(
            _NAMESPACE, _SYMBOL, Timeframe.MIN65
        )

    assert coverage_39m == prefix
    assert coverage_65m is None


def test_rewind_rolls_back_every_change_when_it_fails_midway(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    with Cache(str(db_path)) as cache:
        _seed_symbol(cache, _NAMESPACE, _SYMBOL)
        _correct_volume(
            cache,
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN5,
            _SEEDED_BARS[Timeframe.MIN5][-1],
        )
        before = {
            table: _table_rows(db_path, table)
            for table in _all_tables(db_path)
        }
        _execute(
            db_path,
            f"""
            CREATE TRIGGER fail_weekly_rewind
            BEFORE DELETE ON bars
            WHEN OLD.timeframe = 'weekly'
              AND OLD.timestamp = {_ms(_SEEDED_BARS[Timeframe.WEEKLY][-1])}
            BEGIN
                SELECT RAISE(ABORT, 'forced rewind failure');
            END
            """,
        )

        with pytest.raises(sqlite3.IntegrityError, match="forced rewind failure"):
            cache.rewind_symbol_history(
                _NAMESPACE, _SYMBOL, _BOUNDARIES, _SYNTHESIZED
            )

        after = {
            table: _table_rows(db_path, table)
            for table in _all_tables(db_path)
        }

    assert after == before


def test_rewind_keeps_every_correction(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "test.db"
    with Cache(str(db_path)) as cache:
        _seed_symbol(cache, _NAMESPACE, _SYMBOL)
        for timeframe in (Timeframe.MIN5, Timeframe.MIN39, Timeframe.WEEKLY):
            _correct_volume(
                cache, _NAMESPACE, _SYMBOL, timeframe, _SEEDED_BARS[timeframe][-1]
            )

        cache.rewind_symbol_history(_NAMESPACE, _SYMBOL, _BOUNDARIES, _SYNTHESIZED)

    assert len(_table_rows(db_path, "bar_corrections")) == 3


def _seed_symbol(cache: Cache, namespace: str, symbol: str) -> None:
    for timeframe, timestamps in _SEEDED_BARS.items():
        cache.put_bars(
            namespace,
            symbol,
            timeframe,
            [_bar(timestamp) for timestamp in timestamps],
        )
    for timeframe, (start_ms, end_ms) in _SEEDED_COVERAGE.items():
        cache.replace_bar_fetch_coverage(
            namespace, symbol, timeframe, start_ms, end_ms
        )


def _correct_volume(
    cache: Cache,
    namespace: str,
    symbol: str,
    timeframe: Timeframe,
    timestamp: datetime,
) -> None:
    cache.put_bar_correction(
        BarCorrection(
            cache_namespace=namespace,
            symbol=symbol,
            timeframe=timeframe,
            timestamp=timestamp,
            volume=2_000,
        )
    )


def _bar(timestamp: datetime) -> Bar:
    return Bar(
        timestamp=timestamp,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1_000,
        vwap=100.25,
    )


def _bar_timestamps(
    db_path: Path,
    namespace: str,
    symbol: str,
) -> dict[Timeframe, list[int]]:
    rows = _query(
        db_path,
        """
        SELECT timeframe, timestamp
        FROM bars
        WHERE cache_namespace = ? AND symbol = ?
        ORDER BY timeframe, timestamp
        """,
        (namespace, symbol),
    )
    result: dict[Timeframe, list[int]] = {}
    for timeframe, timestamp in rows:
        result.setdefault(Timeframe(timeframe), []).append(timestamp)
    return result


def _all_tables(db_path: Path) -> list[str]:
    rows = _query(
        db_path,
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
    )
    return [name for (name,) in rows]


def _table_rows(db_path: Path, table: str) -> list[tuple[object, ...]]:
    return sorted(_query(db_path, f"SELECT * FROM {table}"), key=repr)


def _other_rows(db_path: Path, table: str) -> list[tuple[object, ...]]:
    return sorted(
        _query(
            db_path,
            f"""
            SELECT * FROM {table}
            WHERE NOT (cache_namespace = ? AND symbol = ?)
            """,
            (_NAMESPACE, _SYMBOL),
        ),
        key=repr,
    )


def _query(
    db_path: Path,
    sql: str,
    parameters: tuple[object, ...] = (),
) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(db_path)) as connection:
        return connection.execute(sql, parameters).fetchall()


def _execute(db_path: Path, sql: str, parameters: tuple[object, ...] = ()) -> None:
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(sql, parameters)
