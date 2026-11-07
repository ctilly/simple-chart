import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import app.controller as controller_module
from app.controller import (
    _ChartLoadCancelled,
    _fetch_and_cache_bars,
    _fetch_bar_range,
)
from data.aggregator import Aggregator
from data.cache import Cache
from data.models import Bar, Level1Quote, MarketSnapshot, Timeframe
from data.provider.base import (
    DataProvider,
    RequestCancelledError,
    background_request_cancellation,
    background_requests,
)

_NAMESPACE = "alpaca:test"
_SPAN = timedelta(days=30)
_END = datetime(2026, 6, 10, 14, 37, 12, tzinfo=timezone.utc)


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


class PagedProvider(DataProvider):
    """Date-paged fake: one bar per request, at the request start + 1 hour."""

    def __init__(self, page_span: timedelta | None = _SPAN) -> None:
        self.page_span = page_span
        self.calls: list[tuple[datetime, datetime]] = []
        self.gates: dict[datetime, threading.Event] = {}
        self.failures: set[datetime] = set()
        self.started = threading.Condition()
        self.empty = False

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        with self.started:
            self.calls.append((start, end))
            self.started.notify_all()
        gate = self.gates.get(start)
        if gate is not None:
            assert gate.wait(5)
        if start in self.failures:
            raise RuntimeError("chunk failed")
        if self.empty:
            return []
        return [
            Bar(
                timestamp=start + timedelta(hours=1),
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1,
            )
        ]

    def wait_for_calls(self, count: int) -> None:
        with self.started:
            assert self.started.wait_for(lambda: len(self.calls) >= count, 5)

    def history_page_span(self, timeframe: Timeframe) -> timedelta | None:
        return self.page_span

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        return {}

    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        return None

    def native_timeframes(self) -> list[Timeframe]:
        return [Timeframe.MIN5, Timeframe.DAILY]


def _coverage(cache: Cache) -> tuple[int, int] | None:
    return cache.get_bar_fetch_coverage(_NAMESPACE, "XBI", Timeframe.MIN5)


def _chunk_starts(start: datetime, end: datetime) -> list[datetime]:
    starts: list[datetime] = []
    chunk_end = end
    while chunk_end > start:
        chunk_end = max(start, chunk_end - _SPAN)
        starts.append(chunk_end)
    return starts


class _SubmissionSpy(ThreadPoolExecutor):
    submitted: list[datetime] = []

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        _SubmissionSpy.submitted.append(args[3])  # (context.run, fetch_bars, symbol, timeframe, start, end)
        return super().submit(fn, *args, **kwargs)


def test_cold_paged_range_fetches_chunks_newest_first_and_covers_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SubmissionSpy.submitted = []
    monkeypatch.setattr(controller_module, "ThreadPoolExecutor", _SubmissionSpy)
    provider = PagedProvider()
    aggregator = Aggregator(provider)
    start = _END - timedelta(days=100, hours=3)

    with Cache(str(tmp_path / "test.db")) as cache:
        _fetch_and_cache_bars(
            aggregator, cache, _NAMESPACE, "XBI", Timeframe.MIN5, start, _END,
        )
        stored = cache.get_bars(
            _NAMESPACE, "XBI", Timeframe.MIN5, _ms(start), _ms(_END),
        )
        coverage = _coverage(cache)

    starts = _chunk_starts(start, _END)
    assert len(starts) == 4
    assert sorted(provider.calls, reverse=True) == list(
        zip(starts, [_END, *starts[:-1]])
    )
    assert _SubmissionSpy.submitted == starts
    completed_end = aggregator.completed_history_end(Timeframe.MIN5, _END)
    assert coverage == (_ms(start), _ms(completed_end))
    assert len(stored) == 4


def test_non_paged_provider_keeps_single_request(tmp_path: Path) -> None:
    provider = PagedProvider(page_span=None)
    start = _END - timedelta(days=100)

    with Cache(str(tmp_path / "test.db")) as cache:
        _fetch_and_cache_bars(
            Aggregator(provider), cache, _NAMESPACE, "XBI", Timeframe.MIN5,
            start, _END,
        )

    assert provider.calls == [(start, _END)]


def test_range_within_one_page_keeps_single_request(tmp_path: Path) -> None:
    provider = PagedProvider()
    start = _END - timedelta(days=20)

    with Cache(str(tmp_path / "test.db")) as cache:
        _fetch_and_cache_bars(
            Aggregator(provider), cache, _NAMESPACE, "XBI", Timeframe.MIN5,
            start, _END,
        )

    assert provider.calls == [(start, _END)]


def test_dispatch_window_bounds_completed_work_behind_a_stalled_chunk(
    tmp_path: Path,
) -> None:
    provider = PagedProvider()
    start = _END - timedelta(days=300)
    starts = _chunk_starts(start, _END)
    newest_gate = threading.Event()
    provider.gates[starts[0]] = newest_gate
    cancellation = threading.Event()
    errors: list[BaseException] = []

    with Cache(str(tmp_path / "test.db")) as cache:

        def run() -> None:
            with Cache(str(tmp_path / "test.db")) as worker_cache:
                try:
                    _fetch_bar_range(
                        Aggregator(provider), worker_cache, _NAMESPACE, "XBI",
                        Timeframe.MIN5, start, _END,
                        cancellation_event=cancellation,
                    )
                except BaseException as exc:
                    errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        provider.wait_for_calls(4)
        # Give the dispatcher a chance to (incorrectly) start a fifth chunk.
        worker.join(0.2)
        assert len(provider.calls) == 4
        cancellation.set()
        newest_gate.set()
        worker.join(5)
        assert not worker.is_alive()
        coverage = _coverage(cache)

    assert len(provider.calls) == 4
    assert len(errors) == 1 and isinstance(errors[0], _ChartLoadCancelled)
    # All four dispatched chunks finished and are contiguous from the end.
    assert coverage == (_ms(starts[3]), _ms(_END))


def test_failed_middle_chunk_commits_contiguous_run_and_resumes_remainder(
    tmp_path: Path,
) -> None:
    provider = PagedProvider()
    start = _END - timedelta(days=150)
    starts = _chunk_starts(start, _END)
    provider.failures.add(starts[2])
    aggregator = Aggregator(provider)

    with Cache(str(tmp_path / "test.db")) as cache:
        with pytest.raises(RuntimeError, match="chunk failed"):
            _fetch_and_cache_bars(
                aggregator, cache, _NAMESPACE, "XBI", Timeframe.MIN5,
                start, _END,
            )
        completed_end = aggregator.completed_history_end(Timeframe.MIN5, _END)
        assert _coverage(cache) == (_ms(starts[1]), _ms(completed_end))

        provider.failures.clear()
        provider.calls.clear()
        _fetch_and_cache_bars(
            aggregator, cache, _NAMESPACE, "XBI", Timeframe.MIN5, start, _END,
        )
        coverage = _coverage(cache)

    head_calls = [call for call in provider.calls if call[1] <= starts[1]]
    remainder = _chunk_starts(start, starts[1])
    assert sorted(head_calls) == sorted(
        zip(remainder, [starts[1], *remainder[:-1]])
    )
    assert coverage == (_ms(start), _ms(completed_end))


def test_cancel_after_first_commit_keeps_contiguous_run_and_stops(
    tmp_path: Path,
) -> None:
    provider = PagedProvider()
    start = _END - timedelta(days=300)
    starts = _chunk_starts(start, _END)
    cancellation = threading.Event()
    second_gate = threading.Event()
    provider.gates[starts[1]] = second_gate
    for later in starts[2:]:
        provider.gates[later] = second_gate

    with Cache(str(tmp_path / "test.db")) as cache:
        original_put = cache.put_bars

        def put_then_cancel(*args: object, **kwargs: object) -> None:
            original_put(*args, **kwargs)  # type: ignore[arg-type]
            cancellation.set()
            second_gate.set()

        cache.put_bars = put_then_cancel  # type: ignore[method-assign]
        with pytest.raises(_ChartLoadCancelled):
            _fetch_bar_range(
                Aggregator(provider), cache, _NAMESPACE, "XBI", Timeframe.MIN5,
                start, _END, cancellation_event=cancellation,
            )
        coverage = _coverage(cache)

    assert len(provider.calls) == 4
    assert coverage is not None
    assert coverage[1] == _ms(_END)
    assert coverage[0] in {_ms(chunk_start) for chunk_start in starts[:4]}


def test_disjoint_old_coverage_is_replaced_by_first_commit(tmp_path: Path) -> None:
    provider = PagedProvider()
    aggregator = Aggregator(provider)
    start = _END - timedelta(days=100)
    old_start = _END - timedelta(days=400)
    old_end = _END - timedelta(days=300)

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.replace_bar_fetch_coverage(
            _NAMESPACE, "XBI", Timeframe.MIN5, _ms(old_start), _ms(old_end),
        )
        _fetch_and_cache_bars(
            aggregator, cache, _NAMESPACE, "XBI", Timeframe.MIN5, start, _END,
        )
        coverage = _coverage(cache)

    completed_end = aggregator.completed_history_end(Timeframe.MIN5, _END)
    assert coverage == (_ms(start), _ms(completed_end))


def test_long_stale_tail_refresh_stays_single_request_and_empty_keeps_coverage(
    tmp_path: Path,
) -> None:
    provider = PagedProvider()
    provider.empty = True
    aggregator = Aggregator(provider)
    start = _END - timedelta(days=300)
    covered_end = aggregator.completed_history_end(
        Timeframe.MIN5, _END - timedelta(days=90),
    )

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.replace_bar_fetch_coverage(
            _NAMESPACE, "XBI", Timeframe.MIN5, _ms(start), _ms(covered_end),
        )
        _fetch_and_cache_bars(
            aggregator, cache, _NAMESPACE, "XBI", Timeframe.MIN5, start, _END,
        )
        coverage = _coverage(cache)

    assert len(provider.calls) == 1
    assert provider.calls[0][1] == _END
    assert coverage == (_ms(start), _ms(covered_end))


def test_cancel_after_first_replacing_commit_keeps_only_new_suffix(
    tmp_path: Path,
) -> None:
    provider = PagedProvider()
    start = _END - timedelta(days=300)
    starts = _chunk_starts(start, _END)
    old_start = _END - timedelta(days=900)
    old_end = _END - timedelta(days=800)
    cancellation = threading.Event()
    older_gate = threading.Event()
    for older in starts[1:]:
        provider.gates[older] = older_gate

    with Cache(str(tmp_path / "test.db")) as cache:
        cache.replace_bar_fetch_coverage(
            _NAMESPACE, "XBI", Timeframe.MIN5, _ms(old_start), _ms(old_end),
        )
        original_replace = cache.replace_bar_fetch_coverage

        def replace_then_cancel(*args: Any, **kwargs: Any) -> None:
            original_replace(*args, **kwargs)
            cancellation.set()
            older_gate.set()

        cache.replace_bar_fetch_coverage = replace_then_cancel  # type: ignore[method-assign]
        with pytest.raises(_ChartLoadCancelled):
            _fetch_bar_range(
                Aggregator(provider), cache, _NAMESPACE, "XBI", Timeframe.MIN5,
                start, _END, replace_coverage=True,
                cancellation_event=cancellation,
            )
        coverage = _coverage(cache)

    assert coverage is not None
    # The disjoint old interval is gone; only the new contiguous suffix remains.
    assert coverage[1] == _ms(_END)
    assert coverage[0] in {_ms(chunk_start) for chunk_start in starts[:4]}
    assert coverage[0] > _ms(old_end)


def test_cancelled_background_chunks_count_as_cancellation_not_failure(
    tmp_path: Path,
) -> None:
    provider = PagedProvider()
    start = _END - timedelta(days=150)
    starts = _chunk_starts(start, _END)
    cancellation = threading.Event()
    original_fetch = provider.fetch_bars

    def fetch(symbol: str, timeframe: Timeframe, chunk_start: datetime, chunk_end: datetime) -> list[Bar]:
        if chunk_start == starts[0]:
            return original_fetch(symbol, timeframe, chunk_start, chunk_end)
        cancellation.set()
        raise RequestCancelledError

    provider.fetch_bars = fetch  # type: ignore[method-assign]

    with Cache(str(tmp_path / "test.db")) as cache:
        with background_requests(cancellation):
            with pytest.raises(_ChartLoadCancelled):
                _fetch_bar_range(
                    Aggregator(provider), cache, _NAMESPACE, "XBI", Timeframe.MIN5,
                    start, _END, cancellation_event=cancellation,
                )
        coverage = _coverage(cache)

    assert coverage == (_ms(starts[0]), _ms(_END))


def test_chunk_tasks_inherit_the_callers_request_priority(tmp_path: Path) -> None:
    provider = PagedProvider()
    seen: list[bool] = []
    original_fetch = provider.fetch_bars

    def fetch(symbol: str, timeframe: Timeframe, chunk_start: datetime, chunk_end: datetime) -> list[Bar]:
        seen.append(background_request_cancellation() is not None)
        return original_fetch(symbol, timeframe, chunk_start, chunk_end)

    provider.fetch_bars = fetch  # type: ignore[method-assign]
    start = _END - timedelta(days=100)

    with Cache(str(tmp_path / "test.db")) as cache:
        _fetch_bar_range(
            Aggregator(provider), cache, _NAMESPACE, "XBI", Timeframe.MIN5, start, _END,
        )
        foreground = list(seen)
        seen.clear()
        with background_requests(threading.Event()):
            _fetch_bar_range(
                Aggregator(provider), cache, _NAMESPACE, "XBI", Timeframe.MIN5,
                start - timedelta(days=100), start,
            )

    assert foreground and not any(foreground)
    assert seen and all(seen)
