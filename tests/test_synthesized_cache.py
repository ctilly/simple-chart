from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest

from app.controller import _fetch_series
from data.aggregator import Aggregator
from data.cache import Cache
from data.models import Bar, BarCorrection, Level1Quote, MarketSnapshot, Timeframe
from data.provider.base import DataProvider, HistoryUnavailableError


_NAMESPACE = "alpaca:iex"
_SYMBOL = "AAPL"
_NOW = datetime(2026, 11, 5, 14, 0, tzinfo=UTC)
_HISTORY_START = _NOW - timedelta(days=600)
_SETTLED_END = datetime(2026, 11, 4, 21, 5, tzinfo=UTC)
_SESSION_OPEN = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)


class _Provider(DataProvider):
    def __init__(
        self,
        error: Exception | None = None,
        bars: list[Bar] | None = None,
    ) -> None:
        self.error = error
        self.bars = [] if bars is None else bars
        self.calls: list[tuple[str, Timeframe, datetime, datetime]] = []

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        self.calls.append((symbol, timeframe, start, end))
        if self.error is not None:
            raise self.error
        return [bar for bar in self.bars if start <= bar.timestamp <= end]

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        return {}

    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        return None

    def native_timeframes(self) -> list[Timeframe]:
        return [Timeframe.MIN5, Timeframe.DAILY, Timeframe.WEEKLY]


def _bar(timestamp: datetime, price: float) -> Bar:
    return Bar(
        timestamp=timestamp,
        open=price,
        high=price + 2.0,
        low=price - 1.0,
        close=price + 1.0,
        volume=100,
    )


def _base_bars() -> list[Bar]:
    bars = [
        _bar(_SESSION_OPEN + timedelta(minutes=offset), 100.0 + offset)
        for offset in range(0, 65, 5)
    ]
    bars.append(_bar(datetime(2026, 11, 4, 20, 55, tzinfo=UTC), 200.0))
    return bars


def _seed_complete_base(cache: Cache, bars: list[Bar] | None = None) -> None:
    cache.put_bars(
        _NAMESPACE,
        _SYMBOL,
        Timeframe.MIN5,
        _base_bars() if bars is None else bars,
    )
    cache.extend_bar_fetch_coverage(
        _NAMESPACE,
        _SYMBOL,
        Timeframe.MIN5,
        int(_HISTORY_START.timestamp() * 1000),
        int(_SETTLED_END.timestamp() * 1000),
    )


def test_missing_target_materializes_from_complete_base_without_provider_call(
    tmp_path: Path,
) -> None:
    provider = _Provider()

    with Cache(str(tmp_path / "test.db")) as cache:
        _seed_complete_base(cache)

        series = _fetch_series(
            Aggregator(provider),
            cache,
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            _NOW,
        )

    assert provider.calls == []
    assert series.timeframe is Timeframe.MIN65
    assert series.bars[0].timestamp == _SESSION_OPEN
    assert series.bars[0].open == 100.0
    assert series.bars[0].close == 161.0


def test_materialization_uses_raw_base_values_not_corrections(tmp_path: Path) -> None:
    provider = _Provider()
    first = _base_bars()[0]

    with Cache(str(tmp_path / "test.db")) as cache:
        _seed_complete_base(cache)
        cache.put_bar_correction(
            BarCorrection(
                cache_namespace=_NAMESPACE,
                symbol=_SYMBOL,
                timeframe=Timeframe.MIN5,
                timestamp=first.timestamp,
                open=101.5,
            )
        )

        series = _fetch_series(
            Aggregator(provider),
            cache,
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            _NOW,
        )

    assert provider.calls == []
    assert series.bars[0].open == first.open


def test_partial_base_rebuild_preserves_older_usable_target(tmp_path: Path) -> None:
    provider = _Provider()
    old_target = _bar(_HISTORY_START, 50.0)
    recent_start = datetime(2026, 11, 4, 14, 30, tzinfo=UTC)

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            [old_target],
        )
        cache.put_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN5,
            _base_bars(),
        )
        cache.extend_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN5,
            int(recent_start.timestamp() * 1000),
            int(_SETTLED_END.timestamp() * 1000),
        )

        series = _fetch_series(
            Aggregator(provider),
            cache,
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            _NOW,
        )

    assert provider.calls == []
    assert old_target in series.bars
    assert any(bar.timestamp == _SESSION_OPEN for bar in series.bars)


def test_stale_base_fetches_only_incremental_tail_then_materializes(
    tmp_path: Path,
) -> None:
    prior_last = datetime(2026, 11, 3, 20, 55, tzinfo=UTC)
    prior_settled = datetime(2026, 11, 3, 21, 5, tzinfo=UTC)
    provider = _Provider(bars=_base_bars())

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN5,
            [_bar(prior_last, 50.0)],
        )
        cache.extend_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN5,
            int(_HISTORY_START.timestamp() * 1000),
            int(prior_settled.timestamp() * 1000),
        )
        cache.put_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            [_bar(prior_last, 50.0)],
        )
        cache.extend_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            int(_HISTORY_START.timestamp() * 1000),
            int(prior_settled.timestamp() * 1000),
        )

        series = _fetch_series(
            Aggregator(provider),
            cache,
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            _NOW,
        )

    assert provider.calls == [
        (_SYMBOL, Timeframe.MIN5, prior_last, _NOW),
    ]
    assert series.bars[0].timestamp == prior_last
    assert any(bar.timestamp == _SESSION_OPEN for bar in series.bars)


def test_settled_target_with_partial_base_does_not_fetch_unused_head(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    first_target = _bar(_HISTORY_START + timedelta(days=1), 50.0)
    final_target = _bar(datetime(2026, 11, 4, 20, 55, tzinfo=UTC), 70.0)

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            [first_target, final_target],
        )
        cache.extend_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            int(_HISTORY_START.timestamp() * 1000),
            int(_SETTLED_END.timestamp() * 1000),
        )
        _seed_complete_base(cache)
        cache.replace_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN5,
            int(_SESSION_OPEN.timestamp() * 1000),
            int(_SETTLED_END.timestamp() * 1000),
        )

        series = _fetch_series(
            Aggregator(provider),
            cache,
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            _NOW,
        )

    assert provider.calls == []
    assert series.bars == [first_target, final_target]


def test_target_newer_than_base_preserves_base_coverage_head(tmp_path: Path) -> None:
    now = datetime(2026, 11, 5, 16, 0, tzinfo=UTC)
    history_start = now - timedelta(days=600)
    base_last = datetime(2026, 11, 3, 20, 55, tzinfo=UTC)
    base_end = datetime(2026, 11, 3, 21, 5, tzinfo=UTC)
    target_last = datetime(2026, 11, 5, 14, 30, tzinfo=UTC)
    provider = _Provider(bars=_base_bars())

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN5,
            [_bar(base_last, 50.0)],
        )
        cache.extend_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN5,
            int(history_start.timestamp() * 1000),
            int(base_end.timestamp() * 1000),
        )
        cache.put_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            [_bar(target_last, 70.0)],
        )
        cache.extend_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            int(history_start.timestamp() * 1000),
            int(datetime(2026, 11, 5, 15, 0, tzinfo=UTC).timestamp() * 1000),
        )

        _fetch_series(
            Aggregator(provider),
            cache,
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            now,
        )
        coverage = cache.get_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN5,
        )

    assert provider.calls[0][2] <= base_end
    assert coverage is not None
    assert coverage[0] == int(history_start.timestamp() * 1000)


def test_history_unavailable_head_still_materializes_cached_base(
    tmp_path: Path,
) -> None:
    provider = _Provider(HistoryUnavailableError(_SYMBOL, Timeframe.MIN5))

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN5,
            _base_bars(),
        )
        cache.extend_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN5,
            int(_SESSION_OPEN.timestamp() * 1000),
            int(_SETTLED_END.timestamp() * 1000),
        )

        series = _fetch_series(
            Aggregator(provider),
            cache,
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            _NOW,
        )

    assert [call[1] for call in provider.calls] == [Timeframe.MIN5]
    assert series.bars[0].timestamp == _SESSION_OPEN
    assert series.bars[0].open == 100.0


def test_history_unavailable_returns_existing_target_without_mutation(
    tmp_path: Path,
) -> None:
    existing = _bar(_SESSION_OPEN, 50.0)
    provider = _Provider(HistoryUnavailableError(_SYMBOL, Timeframe.MIN5))

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            [existing],
        )

        series = _fetch_series(
            Aggregator(provider),
            cache,
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            _NOW,
        )

    assert series.bars == [existing]
    assert [call[1] for call in provider.calls] == [Timeframe.MIN5]


def test_settled_target_does_not_hide_missing_history_head(tmp_path: Path) -> None:
    existing = _bar(_SESSION_OPEN, 50.0)
    provider = _Provider()

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            [existing],
        )
        cache.extend_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            int(_SESSION_OPEN.timestamp() * 1000),
            int(_SETTLED_END.timestamp() * 1000),
        )

        series = _fetch_series(
            Aggregator(provider),
            cache,
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            _NOW,
        )

    assert provider.calls == [
        (_SYMBOL, Timeframe.MIN5, _HISTORY_START, _NOW),
    ]
    assert series.bars == [existing]


def test_other_provider_errors_propagate_and_preserve_existing_target(
    tmp_path: Path,
) -> None:
    existing = _bar(_SESSION_OPEN, 50.0)
    provider = _Provider(RuntimeError("provider failed"))

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.put_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            [existing],
        )

        with pytest.raises(RuntimeError, match="provider failed"):
            _fetch_series(
                Aggregator(provider),
                cache,
                _NAMESPACE,
                _SYMBOL,
                Timeframe.MIN65,
                _NOW,
            )
        preserved = cache.get_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            int(_HISTORY_START.timestamp() * 1000),
            int(_NOW.timestamp() * 1000),
        )

    assert preserved == [existing]


def test_legacy_nonaligned_target_rebuilds_from_cached_base(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    orphan = _bar(_SESSION_OPEN + timedelta(minutes=5), 70.0)
    legacy_end = _SETTLED_END + timedelta(seconds=3)

    with Cache(str(tmp_path / "test.db")) as cache:
        _seed_complete_base(cache)
        cache.put_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            [_bar(_SESSION_OPEN, 50.0), orphan],
        )
        cache.extend_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            int(_HISTORY_START.timestamp() * 1000),
            int(legacy_end.timestamp() * 1000),
        )

        series = _fetch_series(
            Aggregator(provider),
            cache,
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            _NOW,
        )
        coverage = cache.get_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
        )

    assert provider.calls == []
    assert [bar.timestamp for bar in series.bars] == [
        _SESSION_OPEN,
        datetime(2026, 11, 4, 20, 55, tzinfo=UTC),
    ]
    assert series.bars[0].open == 100.0
    assert coverage == (
        int(_HISTORY_START.timestamp() * 1000),
        int(_SETTLED_END.timestamp() * 1000),
    )


def test_warm_materialization_preserves_unchanged_historical_target(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    unchanged = _bar(datetime(2026, 11, 3, 14, 30, tzinfo=UTC), 50.0)

    with Cache(str(tmp_path / "test.db")) as cache:
        _seed_complete_base(cache)
        cache.put_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            [unchanged, _bar(_SESSION_OPEN, 70.0)],
        )
        cache.extend_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            int(_HISTORY_START.timestamp() * 1000),
            int(_SETTLED_END.timestamp() * 1000),
        )

        series = _fetch_series(
            Aggregator(provider),
            cache,
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            _NOW,
        )

    assert provider.calls == []
    assert series.bars[0] == unchanged
    assert series.bars[1].timestamp == _SESSION_OPEN
    assert series.bars[1].open == 100.0


def test_materialized_bars_and_coverage_roll_back_together(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    original = _bar(_SESSION_OPEN, 50.0)
    original_coverage = (
        int(_HISTORY_START.timestamp() * 1000),
        int(_SESSION_OPEN.timestamp() * 1000),
    )

    with Cache(str(db_path)) as cache:
        cache.put_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            [original],
        )
        cache.extend_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            *original_coverage,
        )
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_materialized_insert
                BEFORE INSERT ON bars
                WHEN NEW.timeframe = '65m' AND NEW.open = 999.0
                BEGIN
                    SELECT RAISE(ABORT, 'forced materialization failure');
                END
                """
            )

        with pytest.raises(sqlite3.IntegrityError, match="forced materialization"):
            cache.replace_materialized_bars(
                _NAMESPACE,
                _SYMBOL,
                Timeframe.MIN65,
                int(_SESSION_OPEN.timestamp() * 1000),
                int(_SETTLED_END.timestamp() * 1000),
                [_bar(_SESSION_OPEN, 999.0)],
                int(_HISTORY_START.timestamp() * 1000),
                int(_SETTLED_END.timestamp() * 1000),
            )
        bars = cache.get_bars(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
            int(_HISTORY_START.timestamp() * 1000),
            int(_NOW.timestamp() * 1000),
        )
        coverage = cache.get_bar_fetch_coverage(
            _NAMESPACE,
            _SYMBOL,
            Timeframe.MIN65,
        )

    assert bars == [original]
    assert coverage == original_coverage
