from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.indicator_runtime import IndicatorRuntime
from app.indicator_store import AppIndicatorStoreContext, IndicatorStore
from app.state import State
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
import indicators.vertical_line  # noqa: F401
from indicators.vertical_line import VerticalLineIndicator
from indicators.vertical_line.session_store import VerticalLineSessionStore
from simplechart.api import ChoiceParam, IndicatorAddMode


def test_vertical_line_declares_toolbar_add_mode() -> None:
    assert VerticalLineIndicator().add_mode() == IndicatorAddMode.TOOLBAR


def test_vertical_line_one_click_commit_renders_line(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _daily_series()

    with Cache(str(tmp_path / "test.db")) as cache:
        store = VerticalLineSessionStore(AppIndicatorStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")

        result = runtime.start_drawing(
            "vertical_line",
            series,
            runtime.chart_event(series, 2.0, 103.0),
        )
        render_passes = runtime.render_all(series)

    assert result.done
    assert result.deactivate_tool
    assert state.get_indicator("vertical_line") is not None
    assert len(render_passes) == 1
    assert render_passes[0].render.vertical_lines[0].key == "vertical_line_1"
    assert render_passes[0].render.vertical_lines[0].x_index == 2.0
    assert state.get_indicator("vertical_line").series_keys == ["vertical_line_1"]  # type: ignore[union-attr]


def test_vertical_line_renders_same_timestamp_on_intraday_series(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    daily = _daily_series()
    intraday = _intraday_series(daily.bars[2].timestamp)

    with Cache(str(tmp_path / "test.db")) as cache:
        store = VerticalLineSessionStore(AppIndicatorStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        runtime.start_drawing(
            "vertical_line",
            daily,
            runtime.chart_event(daily, 2.0, 103.0),
        )
        render_pass = runtime.render_all(intraday)[0]

    assert render_pass.render.vertical_lines[0].x_index == 0.0


def test_vertical_line_hit_test_drag_config_and_remove(tmp_path: Path) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _daily_series()

    with Cache(str(tmp_path / "test.db")) as cache:
        store = VerticalLineSessionStore(AppIndicatorStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        store.load_for_symbol("SPY")
        runtime.start_drawing(
            "vertical_line",
            series,
            runtime.chart_event(series, 1.0, 100.0),
        )
        runtime.render_all(series)

        hit = runtime.drawing_hit_test(series, runtime.chart_event(series, 1.05, 120.0))
        assert hit is not None
        assert hit.handle_key == "vertical_line_1"

        assert runtime.begin_drag(series, runtime.chart_event(series, 1.0, 120.0))
        runtime.finish_drag(series, runtime.chart_event(series, 3.0, 120.0))
        dragged = runtime.render_all(series)[0].render.vertical_lines[0]
        assert dragged.x_index == 3.0

        request = runtime.config_request("vertical_line_1")
        assert request is not None
        edited = dict(request.params)
        edited["color"] = "#ff0000"
        edited["line_width"] = 2.5
        edited["line_style"] = ChoiceParam("dash", ["solid", "dash", "dot", "dash_dot"])
        runtime.apply_config("vertical_line_1", edited)
        configured = runtime.render_all(series)[0].render.vertical_lines[0]
        assert configured.color == "#ff0000"
        assert configured.line_width == 2.5
        assert configured.line_style == "dash"

        removal = runtime.remove("vertical_line_1")

    assert removal is not None
    assert removal.series_keys == ["vertical_line_1"]
    assert state.get_indicator("vertical_line") is None


def _daily_series() -> OHLCVSeries:
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    return OHLCVSeries(
        symbol="SPY",
        timeframe=Timeframe.DAILY,
        bars=[
            Bar(
                timestamp=start + timedelta(days=i),
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.0 + i,
                volume=1_000,
            )
            for i in range(5)
        ],
    )


def _intraday_series(day_start: datetime) -> OHLCVSeries:
    return OHLCVSeries(
        symbol="SPY",
        timeframe=Timeframe.MIN5,
        bars=[
            Bar(
                timestamp=day_start + timedelta(minutes=5 * i),
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.0 + i,
                volume=1_000,
            )
            for i in range(3)
        ],
    )


def _runtime(
    state: State,
    cache: Cache,
    store: VerticalLineSessionStore,
) -> IndicatorRuntime:
    return IndicatorRuntime(
        state,
        cache,
        IndicatorStore(state, cache, [store]),
        lookback_days=600,
    )
