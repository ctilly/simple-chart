"""
History wave H-S3: older history loads when the view nears the oldest bar
(docs/agentic-projects/codebase-review-fixes-2026-11-10.md, "History wave").

Reuses the frozen Saturday 2026-10-24 clock and fake sources of
test_basis_change.py. The Alpaca-like source pages intraday history in 30-day
requests and has no history limit; FLOOR is the oldest bar it serves.
"""

import threading
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import app.controller as controller_module
from app.controller import MainWindow, _first_session_open_at_or_after
from chart.window import ChartWidget
from data.aggregator import Aggregator
from data.cache import Cache
from app.state import ChartExtensionState
from data.models import Bar, OHLCVSeries, Timeframe
from simplechart.api import (
    ChartExtension,
    ChartExtensionRender,
    HorizontalLineRender,
    register_extension,
)
from simplechart.extensions import _registry
from data.provider.base import HistoryUnavailableError
from data.provider.config import ALPACA_PAPER_CONNECTION_ID, YFINANCE_CONNECTION_ID
from test_basis_change import (  # noqa: F401  (fixture)
    ET,
    _ALPACA,
    _ALPACA_FETCH_START,
    _FETCH_START,
    _NOW,
    _SYMBOL,
    _WEDNESDAY,
    _WEDNESDAY_CLOSE,
    _YAHOO,
    _YAHOO_LIMIT,
    _AdjustedSource,
    _adjusted,
    _at,
    _bars,
    _isolated_finplot_windows,
    _ms,
    _seed,
    _settled,
    _show,
    _window,
)

_FLOOR = _at(date(2024, 6, 3))
_production_bars_wanted = ChartWidget.older_history_bars_wanted
_FIRST_CHUNK_START = _first_session_open_at_or_after(_ALPACA_FETCH_START - timedelta(days=120))


class _FlooredSource(_AdjustedSource):
    """Alpaca-like: paged intraday, no history limit, nothing before floor."""

    def __init__(
        self,
        floor: datetime,
        ratio: float = 1.0,
        volume_factor: float = 1.0,
        ex_before: datetime | None = None,
    ) -> None:
        super().__init__(None, ratio, volume_factor, page_span=timedelta(days=30))
        self.floor = floor
        self.ex_before = ex_before
        self.gate: threading.Event | None = None
        self.gated = threading.Event()

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        if self.gate is not None and timeframe.is_intraday and end <= _ALPACA_FETCH_START:
            self.gated.set()
            assert self.gate.wait(20)
        if self.ex_before is not None:
            with self._lock:
                self.calls.append((timeframe, start, end))
            bars = [
                bar if bar.timestamp >= self.ex_before else _scaled_basis(bar)
                for bar in _source(timeframe, start, end)
            ]
        else:
            bars = super().fetch_bars(symbol, timeframe, start, end)
        return [bar for bar in bars if bar.timestamp >= self.floor]


def _source(timeframe: Timeframe, start: datetime, end: datetime) -> list[Bar]:
    from test_basis_change import _source_bars

    return _source_bars(timeframe, start, end)


def _scaled_basis(bar: Bar) -> Bar:
    from test_basis_change import _scaled

    return _scaled(bar, 0.5, 2.0)


def _seed_alpaca(db_path: Path, timeframe: Timeframe) -> None:
    """The initial 600-day window, cached through Wednesday's close."""
    _seed(
        db_path,
        timeframe,
        _bars(timeframe, _ALPACA_FETCH_START.date(), _WEDNESDAY),
        _ALPACA_FETCH_START,
        _WEDNESDAY_CLOSE,
        _ALPACA,
    )


def _older_window(
    qtbot: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: _AdjustedSource,
    connection_id: str = YFINANCE_CONNECTION_ID,
) -> MainWindow:
    window = _window(qtbot, db_path, monkeypatch, provider, connection_id)
    # test_basis_change's harness disables the trigger for unrelated tests.
    monkeypatch.setattr(
        ChartWidget, "older_history_bars_wanted", _production_bars_wanted,
    )
    return window


def _alpaca_window(
    qtbot: Any,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: _FlooredSource,
) -> MainWindow:
    return _older_window(qtbot, db_path, monkeypatch, provider, ALPACA_PAPER_CONNECTION_ID)


def _older(qtbot: Any, window: MainWindow, bars_wanted: int = 100) -> list[Bar]:
    window._on_older_history_wanted(bars_wanted)
    return _settled(qtbot, window)


def _older_requests(provider: _AdjustedSource, timeframe: Timeframe) -> list[tuple[datetime, datetime]]:
    return [
        (start, end)
        for tf, start, end in provider.calls
        if tf == timeframe and end <= _ALPACA_FETCH_START
    ]


def test_the_chart_asks_for_older_history_near_its_left_edge(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    window = _alpaca_window(qtbot, db_path, monkeypatch, _FlooredSource(_FLOOR))
    monkeypatch.setattr(window, "_on_older_history_wanted", lambda _bars: None)
    _show(qtbot, window, Timeframe.MIN30)
    viewbox: Any = window._chart.plot_manager.price_viewbox()

    with qtbot.waitSignal(window._chart.older_history_wanted, timeout=2000) as blocker:
        viewbox.setXRange(-40, 160, padding=0)

    assert blocker.args is not None and blocker.args[0] >= 200
    viewbox.setXRange(1000, 1200, padding=0)
    assert window._chart.older_history_bars_wanted() == 0


def test_older_intraday_history_loads_and_keeps_the_view(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    provider = _FlooredSource(_FLOOR)
    window = _alpaca_window(qtbot, db_path, monkeypatch, provider)
    shown = _show(qtbot, window, Timeframe.MIN30)
    viewbox: Any = window._chart.plot_manager.price_viewbox()
    viewbox.setXRange(5.0, 205.0, padding=0)
    edge_times = (shown[5].timestamp, shown[205].timestamp)

    bars = _older(qtbot, window)

    assert bars[0].timestamp == _FIRST_CHUNK_START
    assert bars == _bars(Timeframe.MIN30, _FIRST_CHUNK_START.date(), date(2026, 10, 23))
    requests = _older_requests(provider, Timeframe.MIN30)
    assert requests and max(end for _start, end in requests) == _ALPACA_FETCH_START
    assert min(start for start, _end in requests) == _FIRST_CHUNK_START
    rect = viewbox.viewRect()
    assert bars[round(rect.left())].timestamp == edge_times[0]
    assert bars[round(rect.right())].timestamp == edge_times[1]
    with Cache(str(db_path)) as cache:
        coverage = cache.get_bar_fetch_coverage(_ALPACA, _SYMBOL, Timeframe.MIN30)
    assert coverage is not None and coverage[0] == _ms(_FIRST_CHUNK_START)


def test_older_history_stops_at_the_provider_floor_across_restarts(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    provider = _FlooredSource(_FLOOR)
    window = _alpaca_window(qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN30)

    for _ in range(6):
        bars = _older(qtbot, window, bars_wanted=2000)
    requests = len(provider.calls)
    _older(qtbot, window, bars_wanted=2000)

    assert bars[0].timestamp == _FLOOR
    assert len(provider.calls) == requests
    window.close()

    restarted = _alpaca_window(qtbot, db_path, monkeypatch, provider)
    _show(qtbot, restarted, Timeframe.MIN30)
    provider.calls.clear()
    _older(qtbot, restarted, bars_wanted=2000)
    _older(qtbot, restarted, bars_wanted=2000)

    assert _older_requests(provider, Timeframe.MIN30) == []
    series = restarted._current_series
    assert series is not None and series.bars[0].timestamp == _FLOOR


def test_yahoo_intraday_extends_from_the_cache_without_requests(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Yahoo returns ~55 days; contiguous cached history beyond the initial
    # 600-day window is shown on pan, and nothing older is requested.
    db_path = tmp_path / "test.db"
    deep_start = date(2024, 11, 4)
    _seed(
        db_path,
        Timeframe.MIN30,
        _bars(Timeframe.MIN30, deep_start, _WEDNESDAY),
        _at(deep_start),
        _WEDNESDAY_CLOSE,
    )
    provider = _AdjustedSource(_YAHOO_LIMIT)
    window = _older_window(qtbot, db_path, monkeypatch, provider)
    initial = _show(qtbot, window, Timeframe.MIN30)
    assert initial[0].timestamp == _ALPACA_FETCH_START

    for _ in range(3):
        bars = _older(qtbot, window, bars_wanted=2000)

    assert bars[0].timestamp == _at(deep_start)
    assert all(
        start >= _FETCH_START - timedelta(days=7)
        for tf, start, _end in provider.calls
        if tf.is_intraday
    )


def test_older_39m_history_is_built_from_older_5m(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN5)
    provider = _FlooredSource(_FLOOR)
    window = _alpaca_window(qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN39)

    bars = _older(qtbot, window)

    assert bars[0].timestamp == _FIRST_CHUNK_START
    assert bars == Aggregator(provider).synthesize_cached_bars(
        _bars(Timeframe.MIN5, _FIRST_CHUNK_START.date(), date(2026, 10, 23)),
        Timeframe.MIN39,
    )


def test_a_timeframe_change_drops_an_older_result_and_waits_for_it(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    _seed_alpaca(db_path, Timeframe.MIN15)
    provider = _FlooredSource(_FLOOR)
    window = _alpaca_window(qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN30)
    provider.gate = threading.Event()
    window._on_older_history_wanted(100)
    qtbot.waitUntil(provider.gated.is_set, timeout=10_000)

    window._state.timeframe = Timeframe.MIN15
    window._load()
    qtbot.wait(controller_module._CHART_LOAD_DEBOUNCE_MS * 3)

    # The same symbol's load waits for the older writer to finish.
    assert window._pending_chart_load is not None
    assert [task.request.timeframe for task in window._chart_load_tasks.values()] == [
        Timeframe.MIN30,
    ]
    provider.gate.set()
    bars = _settled(qtbot, window)

    assert window._current_series is not None
    assert window._current_series.timeframe == Timeframe.MIN15
    assert bars[0].timestamp == _ALPACA_FETCH_START


def test_an_older_merge_waits_for_a_drag(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    window = _alpaca_window(qtbot, db_path, monkeypatch, _FlooredSource(_FLOOR))
    shown = _show(qtbot, window, Timeframe.MIN30)
    runtime_type = type(window._extension_runtime)
    monkeypatch.setattr(runtime_type, "drag_active", property(lambda _self: True))

    held = _older(qtbot, window)

    assert held == shown
    monkeypatch.setattr(runtime_type, "drag_active", property(lambda _self: False))
    window._flush_held_history_merge()
    series = window._current_series
    assert series is not None and series.bars[0].timestamp == _FIRST_CHUNK_START


def test_a_redraw_keeps_the_older_history(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    window = _alpaca_window(qtbot, db_path, monkeypatch, _FlooredSource(_FLOOR))
    _show(qtbot, window, Timeframe.MIN30)
    extended = _older(qtbot, window)

    window._reload_extensions()

    series = window._current_series
    assert series is not None and series.bars == extended


def test_a_basis_change_at_the_oldest_cached_session_reloads_instead(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The provider now returns the cache's oldest session on another basis:
    # older bars must not be joined to it; a normal load resets the cache.
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    provider = _FlooredSource(_FLOOR, ex_before=_at(date(2025, 6, 2)))
    window = _alpaca_window(qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN30)
    request = window._current_chart_request
    assert request is not None
    provider.calls.clear()

    _older(qtbot, window)

    reload = window._current_chart_request
    assert reload is not None and reload.generation > request.generation
    assert not any(end < _ALPACA_FETCH_START for _tf, _start, end in provider.calls if _start < _ALPACA_FETCH_START - timedelta(days=2))
    with Cache(str(db_path)) as cache:
        assert cache.get_raw_bars(_ALPACA, _SYMBOL, Timeframe.MIN30, 0, _ms(_ALPACA_FETCH_START) - 1) == []
        assert cache.get_basis_changes(_ALPACA, _SYMBOL) == []


def test_deep_intraday_indicators_read_daily_bars_back_to_the_series_start(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # H-12: daily references are read from the series start minus the
    # lookback, not only from the initial window.
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    window = _alpaca_window(qtbot, db_path, monkeypatch, _FlooredSource(_FLOOR))
    _show(qtbot, window, Timeframe.MIN30)
    bars = _older(qtbot, window)
    reads: list[int] = []
    production = window._cache.get_bars

    def get_bars(namespace: str, symbol: str, timeframe: Timeframe, start: int, end: int) -> list[Bar]:
        if timeframe == Timeframe.DAILY:
            reads.append(start)
        return production(namespace, symbol, timeframe, start, end)

    monkeypatch.setattr(window._cache, "get_bars", get_bars)

    window._extension_runtime._daily_bars_for(window._current_series)  # type: ignore[arg-type]

    assert reads and reads[0] <= _ms(bars[0].timestamp - timedelta(days=600))


__all__ = ["ET", "_NOW", "_YAHOO", "_adjusted"]


# ------------------------------------------------------------------
# H-S4: daily and weekly history to the provider's oldest bar (D2: also
# before 1970, on every platform)
# ------------------------------------------------------------------

_LISTED = date(1962, 1, 2)


@pytest.mark.parametrize("timeframe", [Timeframe.DAILY, Timeframe.WEEKLY])
def test_daily_and_weekly_load_back_to_the_listing_in_one_request(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeframe: Timeframe,
) -> None:
    db_path = tmp_path / "test.db"
    provider = _FlooredSource(_at(_LISTED, 0, 0))
    window = _older_window(qtbot, db_path, monkeypatch, provider)
    initial = _show(qtbot, window, timeframe)
    assert initial[0].timestamp.year == 2016
    provider.calls.clear()

    bars = _older(qtbot, window)
    _older(qtbot, window)

    expected_first = _bars(timeframe, _LISTED, date(1962, 1, 31))[0]
    assert bars[0] == expected_first
    assert [(tf, start) for tf, start, _end in provider.calls if tf == timeframe and start.year < 2016] == [
        (timeframe, controller_module._OLDEST_HISTORY_START),
    ]
    assert len(bars) > len(initial)


def test_a_source_with_no_older_daily_history_stops_at_once(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    provider = _FlooredSource(_at(date(2016, 1, 4), 0, 0))
    window = _older_window(qtbot, db_path, monkeypatch, provider)
    initial = _show(qtbot, window, Timeframe.DAILY)

    first = _older(qtbot, window)
    requests = len(provider.calls)
    second = _older(qtbot, window)

    assert first == initial == second
    assert len(provider.calls) == requests


def test_production_code_converts_timestamps_without_platform_limits() -> None:
    # datetime.fromtimestamp rejects times before 1970 on Windows; bars from
    # the 1960s must load there too (D2).
    root = Path(__file__).resolve().parent.parent
    offenders = [
        f"{path.relative_to(root)}:{number}"
        for package in ("app", "chart", "data", "simplechart", "indicators", "tools")
        for path in sorted((root / package).rglob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if "fromtimestamp(" in line
    ]
    assert offenders == []


def test_ms_to_datetime_handles_history_before_1970() -> None:
    from datetime import timezone

    from data.timestamps import ms_to_datetime

    assert ms_to_datetime(-2_208_988_800_000) == datetime(1900, 1, 1, tzinfo=timezone.utc)
    assert ms_to_datetime(1_792_852_200_000) == datetime.fromtimestamp(
        1_792_852_200, tz=timezone.utc,
    )


# ------------------------------------------------------------------
# H-S6: an anchor older than the loaded bars loads older history (H-8)
# ------------------------------------------------------------------


class _AnchorProbe(ChartExtension):
    """Anchored like AVWAP: omits anchors older than the loaded bars."""

    def name(self) -> str:
        return "test_older_anchor_probe"

    def label(self) -> str:
        return "Test Older Anchor Probe"

    def default_params(self) -> dict[str, Any]:
        return {"anchors_ms": []}

    def history_lookback_days(self, params: dict[str, Any]) -> int | None:
        return 0

    def compute(self, series: OHLCVSeries, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def render(self, series: OHLCVSeries, params: dict[str, Any]) -> ChartExtensionRender:
        first_ms = int(series.bars[0].timestamp.timestamp() * 1000)
        anchors: list[int] = params["anchors_ms"]
        unloaded = [
            ms for ms in anchors
            if not params.get("_history_complete", True) and ms < first_ms
        ]
        return ChartExtensionRender(
            horizontal_lines=[
                HorizontalLineRender(
                    key=f"anchor_{ms}", y_value=100.0, label="Anchor",
                    color="#ffffff", line_width=1.0,
                )
                for ms in anchors
                if ms not in unloaded
            ],
            pending_history_start_ms=max(unloaded, default=None),
        )


@pytest.fixture
def _anchor_probe() -> Iterator[None]:
    saved = _registry.snapshot()
    register_extension(_AnchorProbe)
    yield
    _registry.restore(saved)


def _with_anchor(window: MainWindow, anchor: datetime) -> None:
    # Added after the symbol's first load, which installs the defaults.
    window._state.extensions.append(
        ChartExtensionState(
            name="test_older_anchor_probe",
            params={"anchors_ms": [_ms(anchor)]},
        )
    )
    window._reload_extensions(draw_bars=False)


def _anchor_keys(window: MainWindow) -> list[str]:
    probe = window._state.get_extension("test_older_anchor_probe")
    return [] if probe is None else sorted(probe.series_keys)


@pytest.mark.usefixtures("_anchor_probe")
def test_an_anchor_before_the_loaded_bars_loads_older_history_until_covered(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    window = _alpaca_window(qtbot, db_path, monkeypatch, _FlooredSource(_FLOOR))
    anchor = _at(date(2024, 9, 16), 10)
    _show(qtbot, window, Timeframe.MIN30)
    _with_anchor(window, anchor)
    qtbot.waitUntil(lambda: _anchor_keys(window) != [], timeout=20_000)
    bars = _settled(qtbot, window)

    assert bars[0].timestamp <= anchor
    assert _anchor_keys(window) == [f"anchor_{_ms(anchor)}"]
    assert window._extension_runtime.pending_history_start_ms is None


@pytest.mark.usefixtures("_anchor_probe")
def test_an_anchor_older_than_any_history_renders_once_history_is_exhausted(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    provider = _FlooredSource(_FLOOR)
    window = _alpaca_window(qtbot, db_path, monkeypatch, provider)
    anchor = _at(date(2023, 1, 9), 10)
    _show(qtbot, window, Timeframe.MIN30)
    _with_anchor(window, anchor)
    qtbot.waitUntil(lambda: _anchor_keys(window) != [], timeout=20_000)
    bars = _settled(qtbot, window)

    assert bars[0].timestamp == _FLOOR
    assert window._older_history_stopped == window._current_chart_request


# ------------------------------------------------------------------
# History wave code QC regressions (HF-1, HF-2, HF-3, HF-5)
# ------------------------------------------------------------------


def test_default_view_requests_stop_at_the_provider_floor(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HF-1: a default-size request that returns nothing must count as
    # exhausted (it did only for requests larger than one chunk).
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    provider = _FlooredSource(_FLOOR)
    window = _alpaca_window(qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN30)

    for _ in range(8):
        _older(qtbot, window)

    assert window._current_series is not None
    assert window._current_series.bars[0].timestamp == _FLOOR
    assert window._older_history_stopped == window._current_chart_request


def test_a_recent_listing_stops_asking_for_older_history(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HF-1: a short series is always near its oldest bar; once the source
    # has nothing older, the automatic requests stop.
    db_path = tmp_path / "test.db"
    provider = _FlooredSource(_at(date(2026, 9, 28)))
    window = _alpaca_window(qtbot, db_path, monkeypatch, provider)
    tasks: list[int] = []
    production = window._on_older_history_wanted

    def counting(bars_wanted: int) -> None:
        before = len(window._chart_load_tasks)
        production(bars_wanted)
        if len(window._chart_load_tasks) > before:
            tasks.append(bars_wanted)

    monkeypatch.setattr(window, "_on_older_history_wanted", counting)
    window._chart.older_history_wanted.disconnect()
    window._chart.older_history_wanted.connect(counting)

    _show(qtbot, window, Timeframe.MIN30)
    qtbot.wait(1500)
    _settled(qtbot, window)

    assert len(tasks) <= 2
    assert window._older_history_stopped == window._current_chart_request


def test_a_seam_basis_change_resets_the_cache_and_reloads_once(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HF-2: the normal load checks only the newest sessions, so the older
    # path must reset what it found itself.
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    provider = _FlooredSource(_FLOOR, ex_before=_at(date(2025, 6, 2)))
    window = _alpaca_window(qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN30)
    loads: list[int] = []
    production_load = window._load

    def counting_load(*args: Any, **kwargs: Any) -> None:
        loads.append(1)
        production_load(*args, **kwargs)

    monkeypatch.setattr(window, "_load", counting_load)

    _older(qtbot, window)
    qtbot.wait(1000)
    bars = _settled(qtbot, window)

    assert len(loads) == 1
    first_session = [bar for bar in bars if bar.timestamp.date() == _ALPACA_FETCH_START.date()]
    assert first_session and first_session[0] == _scaled_basis(
        _bars(Timeframe.MIN30, _ALPACA_FETCH_START.date(), _ALPACA_FETCH_START.date())[0]
    )


class _YahooLikeSource(_AdjustedSource):
    """Raises, as yfinance does, for a range with no bars at all."""

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        bars = super().fetch_bars(symbol, timeframe, start, end)
        if not bars:
            raise HistoryUnavailableError(symbol, timeframe)
        return bars


def test_yahoo_intraday_does_not_request_a_sliver_before_its_limit(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HF-3: coverage starts at the first session open after the 55-day limit;
    # the gap before it must not be requested.
    db_path = tmp_path / "test.db"
    _seed(
        db_path,
        Timeframe.MIN30,
        _bars(Timeframe.MIN30, _FETCH_START.date(), _WEDNESDAY),
        _FETCH_START,
        _WEDNESDAY_CLOSE,
    )
    provider = _YahooLikeSource(_YAHOO_LIMIT)
    window = _older_window(qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN30)
    provider.calls.clear()

    _older(qtbot, window)

    assert [call for call in provider.calls if call[0].is_intraday] == []
    assert window._older_history_stopped == window._current_chart_request
    status_bar = window.statusBar()
    assert status_bar is not None and status_bar.currentMessage() == ""


def test_history_unavailable_on_the_older_path_means_exhausted(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    provider = _FlooredSource(_FLOOR)
    window = _alpaca_window(qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN30)
    production_fetch = provider.fetch_bars

    def unavailable(symbol: str, timeframe: Timeframe, start: datetime, end: datetime) -> list[Bar]:
        if timeframe.is_intraday and end <= _ALPACA_FETCH_START:
            raise HistoryUnavailableError(symbol, timeframe)
        return production_fetch(symbol, timeframe, start, end)

    monkeypatch.setattr(provider, "fetch_bars", unavailable)

    _older(qtbot, window)

    assert window._older_history_stopped == window._current_chart_request
    status_bar = window.statusBar()
    assert status_bar is not None and status_bar.currentMessage() == ""


def test_saved_visibility_of_removed_series_is_pruned(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HF-5: an extension with nothing pending prunes keys it no longer draws.
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    window = _alpaca_window(qtbot, db_path, monkeypatch, _FlooredSource(_FLOOR))
    _show(qtbot, window, Timeframe.MIN30)
    window._state.extensions.append(
        ChartExtensionState(
            name="sma",
            params={"days": 5, "color": "#FFA500", "line_width": 1.0, "line_style": "solid"},
            series_visibility={"sma_99": False},
        )
    )

    window._reload_extensions(draw_bars=False)

    sma = window._state.extensions[-1]
    assert sma.series_visibility == {}


def test_time_labels_before_1970_use_a_platform_safe_conversion() -> None:
    # HF-6: finplot formats local times with datetime.fromtimestamp, which
    # rejects times before 1970 on Windows; the chart installs a pandas one.
    from zoneinfo import ZoneInfo

    import finplot as fplt
    import pandas as pd

    import chart.viewport as viewport_module

    viewport_module.install_time_label_conversion()
    assert fplt._x2local_t is viewport_module._x2local_t

    class _Source:
        def __init__(self, ns: int) -> None:
            self.ns = ns

        def hilo(self, x0: float, x1: float) -> tuple[int, int, float, float, int]:
            return self.ns, self.ns, 0.0, 0.0, 1

        def timebased(self) -> bool:
            return True

    saved = fplt.display_timezone
    zone = ZoneInfo("America/New_York")
    fplt.display_timezone = zone

    def finplot_original(ns: int) -> tuple[str, bool]:
        # finplot's own conversion; works here (Linux), not on Windows < 1970.
        return fplt._x2t(  # type: ignore[no-any-return]
            _Source(ns),
            0.0,
            lambda t: fplt._millisecond_tz_wrap(
                datetime.fromtimestamp(t / 1e9, tz=zone).strftime(fplt.timestamp_format)
            ),
        )

    try:
        for stamp in ("2026-03-09 14:35", "2026-11-02 21:00", "1970-01-01 00:00"):
            ns = int(pd.Timestamp(stamp, tz="UTC").value)
            assert fplt._x2local_t(_Source(ns), 0.0) == finplot_original(ns)
        # Before 1970 (daily and weekly bars at 12:00 UTC) the UTC date is
        # shown; see test_time_labels_before_1970_avoid_the_local_zone_lookup.
        old = int(pd.Timestamp("1962-01-02 12:00", tz="UTC").value)
        assert fplt._x2local_t(_Source(old), 0.0)[0].startswith("1962-01-02")
    finally:
        fplt.display_timezone = saved


@pytest.mark.usefixtures("_anchor_probe")
def test_a_failed_seam_request_is_an_error_not_exhaustion(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HF2-2: the seam range has cached bars, so a refusal there is an error;
    # anchors keep waiting instead of drawing from the first bar.
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    provider = _FlooredSource(_FLOOR)
    window = _alpaca_window(qtbot, db_path, monkeypatch, provider)
    _show(qtbot, window, Timeframe.MIN30)
    production_fetch = provider.fetch_bars

    def seam_unavailable(symbol: str, timeframe: Timeframe, start: datetime, end: datetime) -> list[Bar]:
        if timeframe.is_intraday and start == _ALPACA_FETCH_START:
            raise HistoryUnavailableError(symbol, timeframe)
        return production_fetch(symbol, timeframe, start, end)

    monkeypatch.setattr(provider, "fetch_bars", seam_unavailable)
    _with_anchor(window, _at(date(2024, 9, 16), 10))
    _settled(qtbot, window)
    qtbot.wait(300)
    _settled(qtbot, window)

    assert window._older_history_stopped == window._current_chart_request
    assert _anchor_keys(window) == []
    status_bar = window.statusBar()
    assert status_bar is not None and "unavailable" in status_bar.currentMessage()


def test_time_labels_before_1970_avoid_the_local_zone_lookup(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    # HF2-1: dateutil's tzlocal() calls time.localtime, which rejects negative
    # times on Windows. Bars before 1970 are daily or weekly (12:00 UTC), so
    # their labels use the UTC date.
    import time as time_module

    import finplot as fplt
    import pandas as pd
    from dateutil.tz import tzlocal  # type: ignore[import-untyped]

    import chart.viewport as viewport_module

    production_localtime = time_module.localtime

    def windows_localtime(seconds: float | None = None) -> time_module.struct_time:
        if seconds is not None and seconds < 0:
            raise OSError(22, "Invalid argument")
        return production_localtime(seconds)

    class _Source:
        def __init__(self, ns: int) -> None:
            self.ns = ns

        def hilo(self, x0: float, x1: float) -> tuple[int, int, float, float, int]:
            return self.ns, self.ns, 0.0, 0.0, 1

        def timebased(self) -> bool:
            return True

    monkeypatch.setenv("TZ", "America/New_York")
    time_module.tzset()
    monkeypatch.setattr(time_module, "localtime", windows_localtime)
    monkeypatch.setattr(fplt, "display_timezone", tzlocal())
    monkeypatch.setattr(fplt, "truncate_timestamp", False)
    try:
        ns = int(pd.Timestamp("1962-01-02 12:00", tz="UTC").value)
        label, timebased = viewport_module._x2local_t(_Source(ns), 0.0)
    finally:
        monkeypatch.undo()
        time_module.tzset()

    assert timebased and label.startswith("1962-01-02")
    assert capfd.readouterr().err == ""


class _NoticeProbe(ChartExtension):
    def name(self) -> str:
        return "test_notice_probe"

    def label(self) -> str:
        return "Test Notice Probe"

    def default_params(self) -> dict[str, Any]:
        return {"notice": None}

    def history_lookback_days(self, params: dict[str, Any]) -> int | None:
        return 0

    def compute(self, series: OHLCVSeries, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def render(self, series: OHLCVSeries, params: dict[str, Any]) -> ChartExtensionRender:
        return ChartExtensionRender(notice=params["notice"])


@pytest.fixture
def _notice_probe() -> Iterator[None]:
    saved = _registry.snapshot()
    register_extension(_NoticeProbe)
    yield
    _registry.restore(saved)


@pytest.mark.usefixtures("_notice_probe")
def test_an_extension_notice_shows_in_the_status_bar_until_it_clears(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    window = _alpaca_window(qtbot, db_path, monkeypatch, _FlooredSource(_FLOOR))
    _show(qtbot, window, Timeframe.MIN30)
    probe = ChartExtensionState(
        name="test_notice_probe",
        params={"notice": "AVWAP X is anchored more than 600 days back."},
    )
    window._state.extensions.append(probe)
    status_bar = window.statusBar()
    assert status_bar is not None

    window._reload_extensions(draw_bars=False)

    assert status_bar.currentMessage() == "AVWAP X is anchored more than 600 days back."

    probe.params = {"notice": None}
    window._reload_extensions(draw_bars=False)

    assert status_bar.currentMessage() == ""
