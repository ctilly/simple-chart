import math
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtCore import Qt

import app.controller as controller_module
from app.controller import (
    _ChartLoadRequest,
    _FetchWorker,
    _history_end,
    _history_start,
    _recent_history_start,
)
from chart.window import INITIAL_VISIBLE_BARS
from data.aggregator import Aggregator
from data.cache import Cache
from data.calendar import bars_for_n_days
from data.models import Bar, Level1Quote, MarketSnapshot, OHLCVSeries, Timeframe
from data.provider.base import DataProvider, background_request_cancellation
from data.provider.config import ProviderConnection, alpaca_paper_connection
from data.provider.credentials import CredentialStore
from data.session import exchange_day_start, is_regular_session_timestamp

UTC = timezone.utc
_SPAN = timedelta(days=30)
_NOW = datetime(2026, 6, 10, 18, 2, 41, tzinfo=UTC)
_BASE_THREADS = threading.active_count()
_INTRADAY = (
    Timeframe.MIN5,
    Timeframe.MIN15,
    Timeframe.MIN30,
    Timeframe.MIN39,
    Timeframe.MIN65,
)

# NYSE full-day closures and early closes, 2025-2026 (test data).
_CLOSED = {
    date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17),
    date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),
    date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
}
_EARLY_CLOSE = {
    date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
    date(2026, 11, 27), date(2026, 12, 24),
}


@pytest.mark.parametrize("timeframe", _INTRADAY)
@pytest.mark.parametrize(
    "end",
    [
        datetime(2026, 1, 2, 14, 0, tzinfo=UTC),   # after Thanksgiving + Christmas + New Year
        datetime(2025, 12, 1, 14, 0, tzinfo=UTC),  # right after Thanksgiving
        datetime(2026, 4, 6, 14, 0, tzinfo=UTC),   # after Good Friday
        datetime(2026, 7, 6, 14, 0, tzinfo=UTC),   # after Juneteenth + July 3
        datetime(2026, 6, 10, 18, 2, tzinfo=UTC),
    ],
)
def test_recent_window_holds_the_initial_view(
    timeframe: Timeframe,
    end: datetime,
) -> None:
    far_start = end - timedelta(days=600)
    start = _recent_history_start(timeframe, far_start, end)

    assert start == exchange_day_start(start)
    sessions = 0
    day = start.astimezone(UTC).date()
    while day < end.date():
        if day.weekday() < 5 and day not in _CLOSED and day not in _EARLY_CLOSE:
            sessions += 1
        day += timedelta(days=1)
    full_session_bars = bars_for_n_days(1, timeframe)
    assert sessions * full_session_bars >= INITIAL_VISIBLE_BARS


_HOLIDAY_ENDS = (
    datetime(2026, 1, 2, 14, 0, tzinfo=UTC),
    datetime(2025, 12, 1, 14, 0, tzinfo=UTC),
    datetime(2026, 4, 6, 14, 0, tzinfo=UTC),
    datetime(2026, 7, 6, 14, 0, tzinfo=UTC),
    datetime(2026, 6, 10, 18, 2, tzinfo=UTC),
)


def _full_sessions(start: datetime, end: datetime) -> int:
    sessions = 0
    day = start.astimezone(UTC).date()
    while day < end.date():
        if day.weekday() < 5 and day not in _CLOSED and day not in _EARLY_CLOSE:
            sessions += 1
        day += timedelta(days=1)
    return sessions


@pytest.mark.parametrize("timeframe", (Timeframe.MIN5, Timeframe.MIN15))
@pytest.mark.parametrize("end", _HOLIDAY_ENDS)
def test_recent_window_covers_a_50_day_lookback_before_the_initial_view(
    timeframe: Timeframe,
    end: datetime,
) -> None:
    start = _recent_history_start(timeframe, end - timedelta(days=600), end, 50)

    assert start == exchange_day_start(start)
    bars = _full_sessions(start, end) * bars_for_n_days(1, timeframe)
    assert bars >= INITIAL_VISIBLE_BARS + bars_for_n_days(50, timeframe)


@pytest.mark.parametrize("timeframe", _INTRADAY)
def test_recent_window_lookback_widening_is_capped(timeframe: Timeframe) -> None:
    far_start = _NOW - timedelta(days=600)
    view_only = _recent_history_start(timeframe, far_start, _NOW)
    capped = _recent_history_start(timeframe, far_start, _NOW, 500)

    assert capped == min(
        view_only,
        exchange_day_start(
            _NOW - timedelta(days=controller_module._MAX_RECENT_WINDOW_DAYS)
        ),
    )
    assert _recent_history_start(timeframe, far_start, _NOW, 0) == view_only


def test_recent_window_never_precedes_history_start() -> None:
    history_start = _NOW - timedelta(days=5)

    assert _recent_history_start(Timeframe.MIN65, history_start, _NOW) == history_start


class SessionProvider(DataProvider):
    """Deterministic regular-session 5m/15m bars for any requested range."""

    def __init__(
        self,
        page_span: timedelta | None,
        empty_after: datetime | None = None,
    ) -> None:
        self.page_span = page_span
        self.empty_after = empty_after
        self.calls: list[tuple[Timeframe, datetime, datetime]] = []
        self.events: list[str] = []
        self.backfill_before: datetime | None = None
        self.priorities: list[tuple[Timeframe, datetime, datetime, bool]] = []

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        self.calls.append((timeframe, start, end))
        self.priorities.append(
            (timeframe, start, end, background_request_cancellation() is not None)
        )
        if self.backfill_before is not None and end <= self.backfill_before:
            self.events.append("backfill_request")
        if timeframe == Timeframe.DAILY:
            return []
        minutes = timeframe.minutes
        assert minutes is not None
        step = timedelta(minutes=minutes)
        epoch = datetime(2020, 1, 1, tzinfo=UTC)
        slot = epoch + step * math.ceil((start - epoch) / step)
        bars: list[Bar] = []
        while slot < end:
            if is_regular_session_timestamp(slot) and (
                self.empty_after is None or slot < self.empty_after
            ):
                price = 100.0 + (slot.timestamp() // 60) % 997 / 10.0
                bars.append(
                    Bar(
                        timestamp=slot,
                        open=price,
                        high=price + 1.0,
                        low=price - 1.0,
                        close=price + 0.5,
                        volume=int(price * 10),
                    )
                )
            slot += step
        return bars

    def history_page_span(self, timeframe: Timeframe) -> timedelta | None:
        return self.page_span if timeframe.is_intraday else None

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, MarketSnapshot]:
        return {}

    def fetch_level1(self, symbol: str) -> Level1Quote | None:
        return None

    def native_timeframes(self) -> list[Timeframe]:
        return [Timeframe.MIN5, Timeframe.MIN15, Timeframe.DAILY]


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> "_FixedDateTime":
        return cls.fromtimestamp(_NOW.timestamp(), tz=UTC)


def _load(
    db_path: Path,
    provider: SessionProvider,
    timeframe: Timeframe,
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_delivery: Any = None,
    cancellation_event: threading.Event | None = None,
) -> tuple[list[OHLCVSeries], list[OHLCVSeries], list[str]]:
    def create_test_provider(
        _connection: ProviderConnection,
        _credential_store: CredentialStore,
    ) -> DataProvider:
        return provider

    monkeypatch.setattr(controller_module, "datetime", _FixedDateTime)
    monkeypatch.setattr(controller_module, "create_provider", create_test_provider)
    request = _ChartLoadRequest(1, "XBI", timeframe, alpaca_paper_connection())
    worker = _FetchWorker(str(db_path), None, request, cancellation_event)  # type: ignore[arg-type]
    recent: list[OHLCVSeries] = []
    finished: list[OHLCVSeries] = []
    errors: list[str] = []
    def on_recent(_request: object, series: OHLCVSeries) -> None:
        provider.events.append("recent_ready")
        if on_delivery is not None:
            on_delivery(_request, series)
        recent.append(series)

    def on_finished(_request: object, series: OHLCVSeries) -> None:
        if on_delivery is not None:
            on_delivery(_request, series)
        finished.append(series)

    worker.recent_ready.connect(on_recent)
    worker.finished.connect(on_finished)
    worker.error.connect(lambda _request, message: errors.append(message))
    worker.run()
    return recent, finished, errors


def _windows(timeframe: Timeframe) -> tuple[datetime, datetime, datetime]:
    aggregator = Aggregator(SessionProvider(None))
    end = _history_end(timeframe, aggregator, _NOW)
    full_start = _history_start(timeframe, aggregator, end)
    return full_start, _recent_history_start(timeframe, full_start, end), end


def _bar_tuples(series: OHLCVSeries) -> list[tuple[object, ...]]:
    return [
        (bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume)
        for bar in series.bars
    ]


@pytest.mark.parametrize(
    "timeframe",
    [Timeframe.MIN5, Timeframe.MIN15, Timeframe.MIN39, Timeframe.MIN65],
)
def test_cold_paged_load_renders_recent_first_then_matches_single_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeframe: Timeframe,
) -> None:
    full_start, recent_start, _end = _windows(timeframe)
    paged = SessionProvider(_SPAN)
    paged.backfill_before = recent_start

    recent, finished, errors = _load(tmp_path / "paged.db", paged, timeframe, monkeypatch)
    _single_recent, single, single_errors = _load(
        tmp_path / "single.db", SessionProvider(None), timeframe, monkeypatch,
    )

    assert errors == [] and single_errors == []
    assert len(recent) == 1 and len(finished) == 1 and len(single) == 1
    # The recent window is delivered before any older history is requested.
    assert paged.events[0] == "recent_ready"
    assert "backfill_request" in paged.events
    assert _single_recent == []
    assert recent[0].bars[0].timestamp >= recent_start
    assert len(recent[0].bars) >= INITIAL_VISIBLE_BARS
    assert _bar_tuples(recent[0]) == _bar_tuples(finished[0])[-len(recent[0].bars):]
    assert _bar_tuples(finished[0]) == _bar_tuples(single[0])
    assert finished[0].bars[0].timestamp < recent_start
    fetched = Timeframe.MIN15 if timeframe == Timeframe.MIN15 else Timeframe.MIN5
    history_calls = [call for call in paged.calls if call[0] == fetched]
    # During an open session every load re-fetches the forming bar's tail.
    tail_calls = [call for call in history_calls if call[1] >= _end - timedelta(hours=1)]
    recent_calls = [
        call for call in history_calls
        if call[1] >= recent_start and call not in tail_calls
    ]
    backfill_calls = [call for call in history_calls if call[2] <= recent_start]
    assert len(tail_calls) + len(recent_calls) + len(backfill_calls) == len(history_calls)
    assert history_calls[: len(recent_calls)] == recent_calls
    assert min(call[1] for call in recent_calls) == recent_start
    assert min(call[1] for call in backfill_calls) == full_start
    assert len(tail_calls) == 1


def test_warm_paged_load_is_single_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SessionProvider(_SPAN)
    _load(tmp_path / "test.db", provider, Timeframe.MIN5, monkeypatch)
    provider.calls.clear()

    recent, finished, errors = _load(
        tmp_path / "test.db", provider, Timeframe.MIN5, monkeypatch,
    )

    assert errors == []
    assert recent == []
    assert len(finished) == 1
    # Only the open session's forming-bar tail refresh, as before this change.
    history_calls = [call for call in provider.calls if call[0] == Timeframe.MIN5]
    assert len(history_calls) == 1
    assert history_calls[0][1] >= _NOW - timedelta(hours=1)


def test_non_paged_provider_is_single_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_start, _recent_start, end = _windows(Timeframe.MIN5)
    provider = SessionProvider(None)

    recent, finished, errors = _load(
        tmp_path / "test.db", provider, Timeframe.MIN5, monkeypatch,
    )

    assert errors == [] and recent == [] and len(finished) == 1
    assert [call for call in provider.calls if call[0] == Timeframe.MIN5] == [
        (Timeframe.MIN5, full_start, end),
    ]


def test_empty_recent_slice_skips_interim_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _full_start, recent_start, _end = _windows(Timeframe.MIN5)
    provider = SessionProvider(_SPAN, empty_after=recent_start - timedelta(days=10))

    recent, finished, errors = _load(
        tmp_path / "test.db", provider, Timeframe.MIN5, monkeypatch,
    )

    assert errors == [] and recent == []
    assert len(finished) == 1 and finished[0].bars


def _seed_raw_and_target(
    db_path: Path,
    timeframe: Timeframe,
    target_start: datetime,
    target_end_ms: int,
    raw_start: datetime,
    end: datetime,
) -> None:
    namespace = alpaca_paper_connection().cache_namespace
    raw = SessionProvider(None).fetch_bars("XBI", Timeframe.MIN5, raw_start, end)
    aggregator = Aggregator(SessionProvider(None))
    with Cache(str(db_path)) as cache:
        cache.put_bars(namespace, "XBI", Timeframe.MIN5, raw)
        cache.replace_bar_fetch_coverage(
            namespace, "XBI", Timeframe.MIN5,
            int(raw_start.timestamp() * 1000),
            int(aggregator.completed_history_end(Timeframe.MIN5, end).timestamp() * 1000),
        )
        # Wrong (repairable) target rows inside the target coverage.
        wrong = [
            Bar(raw_start + timedelta(days=day, hours=14), 50.0, 51.0, 49.0, 50.5, 1)
            for day in range(1, 4)
        ]
        cache.put_bars(namespace, "XBI", timeframe, wrong)
        cache.replace_bar_fetch_coverage(
            namespace, "XBI", timeframe,
            int(target_start.timestamp() * 1000), target_end_ms,
        )


@pytest.mark.parametrize("timeframe", [Timeframe.MIN39, Timeframe.MIN65])
def test_legacy_synthesized_target_stays_single_phase_and_is_repaired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeframe: Timeframe,
) -> None:
    full_start, recent_start, end = _windows(timeframe)
    raw_start = exchange_day_start(recent_start - timedelta(days=60))
    db_path = tmp_path / "legacy.db"
    # Legacy watermark: not on a completed-interval boundary, starting after
    # the full window start so only the legacy rule keeps it single-phase.
    _seed_raw_and_target(
        db_path, timeframe, raw_start,
        int((end - timedelta(minutes=7)).timestamp() * 1000),
        raw_start, end,
    )

    recent, finished, errors = _load(db_path, SessionProvider(_SPAN), timeframe, monkeypatch)
    _r, single, single_errors = _load(
        tmp_path / "single.db", SessionProvider(None), timeframe, monkeypatch,
    )

    assert errors == [] and single_errors == [] and recent == []
    assert len(finished) == 1 and full_start < raw_start
    assert _bar_tuples(finished[0]) == _bar_tuples(single[0])


@pytest.mark.parametrize("timeframe", [Timeframe.MIN39, Timeframe.MIN65])
def test_complete_synthesized_target_stays_single_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeframe: Timeframe,
) -> None:
    full_start, recent_start, end = _windows(timeframe)
    db_path = tmp_path / "test.db"
    aggregator = Aggregator(SessionProvider(None))
    _seed_raw_and_target(
        db_path, timeframe, full_start,
        int(aggregator.completed_history_end(timeframe, end).timestamp() * 1000),
        exchange_day_start(recent_start - timedelta(days=60)), end,
    )

    recent, finished, errors = _load(db_path, SessionProvider(_SPAN), timeframe, monkeypatch)

    assert errors == [] and recent == [] and len(finished) == 1


@pytest.mark.parametrize("timeframe", [Timeframe.MIN39, Timeframe.MIN65])
def test_recent_window_requests_are_foreground_and_backfill_is_background(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeframe: Timeframe,
) -> None:
    _full_start, recent_start, end = _windows(timeframe)
    provider = SessionProvider(_SPAN)

    recent, finished, errors = _load(tmp_path / "test.db", provider, timeframe, monkeypatch)

    assert errors == [] and len(recent) == 1 and len(finished) == 1
    history = [entry for entry in provider.priorities if entry[0] == Timeframe.MIN5]
    phase_one = [entry for entry in history if entry[1] >= recent_start and entry[1] < end - timedelta(hours=1)]
    backfill = [entry for entry in history if entry[2] <= recent_start]
    assert len(phase_one) >= 2  # the 55/35-day recent window spans several chunks
    assert not any(entry[3] for entry in phase_one)
    assert backfill and all(entry[3] for entry in backfill)


class GatedBranchProvider(SessionProvider):
    """
    The daily-reference call and the first intraday call each block on their own
    gate after entering, so tests decide which branch completes first.
    """

    def __init__(self, page_span: timedelta | None) -> None:
        super().__init__(page_span)
        self.daily_entered = threading.Event()
        self.intraday_entered = threading.Event()
        self.release_daily = threading.Event()
        self.release_intraday = threading.Event()
        self.completed: list[str] = []
        self.raised: list[str] = []
        self.fail_daily = False
        self.fail_intraday = False
        self._intraday_gated = False
        self._lock = threading.Lock()

    def fetch_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime,
    ) -> list[Bar]:
        if timeframe == Timeframe.DAILY:
            self.daily_entered.set()
            assert self.release_daily.wait(5)
            if self.fail_daily:
                self.raised.append("daily")
                raise RuntimeError("daily failed")
            with self._lock:
                self.completed.append("daily")
            return [
                Bar(end - timedelta(days=day), 10.0, 11.0, 9.0, 10.5, 100)
                for day in range(1, 4)
            ]
        with self._lock:
            gated = not self._intraday_gated
            self._intraday_gated = True
        if gated:
            self.intraday_entered.set()
            assert self.release_intraday.wait(5)
            if self.fail_intraday:
                self.raised.append("intraday")
                raise RuntimeError("intraday failed")
        bars = super().fetch_bars(symbol, timeframe, start, end)
        if gated:
            with self._lock:
                self.completed.append("intraday")
        return bars


def _daily_rows(db_path: Path) -> int:
    with Cache(str(db_path)) as cache:
        return len(cache.get_bars(
            alpaca_paper_connection().cache_namespace, "XBI", Timeframe.DAILY,
            0, int(_NOW.timestamp() * 1000) + 1,
        ))


def _start_worker(
    db_path: Path,
    provider: SessionProvider,
    monkeypatch: pytest.MonkeyPatch,
    cancellation_event: threading.Event | None = None,
    on_delivery_check: Any = None,
) -> tuple[threading.Thread, list[str], list[int], list[str]]:
    """Run the production worker on a thread; returns delivery kinds, daily rows at delivery, errors."""
    monkeypatch.setattr(controller_module, "datetime", _FixedDateTime)
    monkeypatch.setattr(
        controller_module, "create_provider", lambda _connection, _store: provider,
    )
    request = _ChartLoadRequest(1, "XBI", Timeframe.MIN5, alpaca_paper_connection())
    worker = _FetchWorker(str(db_path), None, request, cancellation_event)  # type: ignore[arg-type]
    delivered: list[str] = []
    daily_rows: list[int] = []
    errors: list[str] = []

    def on_delivery(kind: str) -> None:
        if on_delivery_check is not None:
            on_delivery_check()
        daily_rows.append(_daily_rows(db_path))
        delivered.append(kind)

    # Direct connections: the worker runs on a plain thread and these tests run
    # no Qt event loop, so queued delivery would never happen.
    direct = Qt.ConnectionType.DirectConnection
    worker.recent_ready.connect(lambda _request, _series: on_delivery("recent"), direct)
    worker.finished.connect(lambda _request, _series: on_delivery("finished"), direct)
    worker.error.connect(lambda _request, message: errors.append(message), direct)
    thread = threading.Thread(target=worker.run)
    thread.start()
    return thread, delivered, daily_rows, errors


@pytest.mark.parametrize("paged", [True, False])
@pytest.mark.parametrize("last_branch", ["daily", "intraday"])
def test_daily_references_overlap_intraday_and_commit_before_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paged: bool,
    last_branch: str,
) -> None:
    provider = GatedBranchProvider(_SPAN if paged else None)
    db_path = tmp_path / "test.db"
    branch_done = {"daily": threading.Event(), "intraday": threading.Event()}
    completion_order: list[str] = []
    premature: list[str] = []
    real_refresh = controller_module._refresh_daily_references
    real_fetch_series = controller_module._fetch_series

    def refresh(*args: Any, **kwargs: Any) -> None:
        real_refresh(*args, **kwargs)  # returns after the daily commit
        completion_order.append("daily")
        branch_done["daily"].set()

    def fetch_series(*args: Any, **kwargs: Any) -> OHLCVSeries:
        series = real_fetch_series(*args, **kwargs)
        if not branch_done["intraday"].is_set():  # the phase-1 / single-phase branch
            completion_order.append("intraday")
            branch_done["intraday"].set()
        return series

    monkeypatch.setattr(controller_module, "_refresh_daily_references", refresh)
    monkeypatch.setattr(controller_module, "_fetch_series", fetch_series)
    thread, delivered, daily_rows, errors = _start_worker(
        db_path, provider, monkeypatch,
        on_delivery_check=lambda: premature.extend(
            name for name, done in branch_done.items() if not done.is_set()
        ),
    )

    # Both branches are inside the provider at once: they overlap.
    assert provider.daily_entered.wait(5) and provider.intraday_entered.wait(5)
    first_branch = "intraday" if last_branch == "daily" else "daily"
    releases = {"daily": provider.release_daily, "intraday": provider.release_intraday}
    releases[first_branch].set()
    assert branch_done[first_branch].wait(5)  # fully finished, not just returned
    releases[last_branch].set()
    thread.join(10)

    assert not thread.is_alive() and errors == []
    assert completion_order == [first_branch, last_branch]
    assert premature == []  # every delivery happened after both branches finished
    assert delivered == (["recent", "finished"] if paged else ["finished"])
    assert all(rows == 3 for rows in daily_rows)
    assert threading.active_count() == _BASE_THREADS


@pytest.mark.parametrize(
    ("failing", "message"),
    [("daily", "daily failed"), ("intraday", "intraday failed"), ("both", "intraday failed")],
)
def test_concurrent_reference_failure_reports_exactly_one_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
    message: str,
) -> None:
    provider = GatedBranchProvider(None)
    provider.fail_daily = failing in ("daily", "both")
    provider.fail_intraday = failing in ("intraday", "both")
    provider.release_daily.set()
    provider.release_intraday.set()

    thread, delivered, _rows, errors = _start_worker(tmp_path / "test.db", provider, monkeypatch)
    thread.join(10)

    assert delivered == []
    assert errors == [message]
    assert threading.active_count() == _BASE_THREADS


def test_failure_after_user_cancellation_reports_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = GatedBranchProvider(None)
    provider.fail_daily = True
    cancellation = threading.Event()

    thread, delivered, _rows, errors = _start_worker(
        tmp_path / "test.db", provider, monkeypatch, cancellation,
    )
    assert provider.daily_entered.wait(5) and provider.intraday_entered.wait(5)
    cancellation.set()
    provider.release_daily.set()
    provider.release_intraday.set()
    thread.join(10)

    assert provider.raised == ["daily"]  # the intended failure really happened
    assert delivered == [] and errors == []
