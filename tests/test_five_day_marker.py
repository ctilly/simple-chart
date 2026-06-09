from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.extension_runtime import ChartExtensionRuntime
from app.extension_store import AppChartExtensionStoreContext, ChartExtensionStore
from app.state import State
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
import indicators.five_day_marker  # noqa: F401
from indicators.five_day_marker import FiveDayMarkerIndicator
from indicators.five_day_marker.store import FiveDayMarkerStore
from simplechart.api import (
    ChartExtensionAddMode,
    ChoiceParam,
    LINE_STYLE_OPTIONS,
    bars_for_n_days,
)


def test_five_day_marker_declares_hidden_add_mode() -> None:
    assert FiveDayMarkerIndicator().add_mode() == ChartExtensionAddMode.HIDDEN


def test_five_day_marker_marks_first_daily_bar_in_current_ma_window() -> None:
    render = FiveDayMarkerIndicator().render(
        _series(Timeframe.DAILY, 8),
        FiveDayMarkerIndicator().default_params(),
    )

    assert len(render.vertical_lines) == 1
    assert render.vertical_lines[0].key == "five_day_marker"
    assert render.vertical_lines[0].label == "5-Day Marker"
    assert render.vertical_lines[0].x_index == 3.0
    assert render.vertical_lines[0].color == "#800080"
    assert render.vertical_lines[0].line_width == 1.0
    assert render.vertical_lines[0].line_style == "dash"


def test_five_day_marker_intraday_uses_project_ma_bar_count() -> None:
    for timeframe in (
        Timeframe.MIN5,
        Timeframe.MIN15,
        Timeframe.MIN30,
        Timeframe.MIN39,
        Timeframe.MIN65,
    ):
        period = bars_for_n_days(5, timeframe)
        render = FiveDayMarkerIndicator().render(
            _series(timeframe, period + 7),
            FiveDayMarkerIndicator().default_params(),
        )

        assert len(render.vertical_lines) == 1
        assert render.vertical_lines[0].x_index == 7.0


def test_five_day_marker_hides_on_weekly() -> None:
    render = FiveDayMarkerIndicator().render(
        _series(Timeframe.WEEKLY, 10),
        FiveDayMarkerIndicator().default_params(),
    )

    assert render.vertical_lines == []


def test_five_day_marker_hides_without_enough_history() -> None:
    render = FiveDayMarkerIndicator().render(
        _series(Timeframe.DAILY, 4),
        FiveDayMarkerIndicator().default_params(),
    )

    assert render.vertical_lines == []


def test_five_day_marker_config_mutation_uses_stable_marker_key() -> None:
    indicator = FiveDayMarkerIndicator()
    request = indicator.config_for_series(
        "five_day_marker",
        indicator.default_params(),
    )
    assert request is not None

    edited = dict(request.params)
    edited["color"] = "#ff0000"
    edited["line_width"] = 2.5
    edited["line_style"] = ChoiceParam("dot", LINE_STYLE_OPTIONS)
    edited["visible"] = False
    mutation = indicator.apply_config_to_series(
        "five_day_marker",
        indicator.default_params(),
        edited,
    )

    assert mutation is not None
    assert mutation.extension_name == "five_day_marker"
    assert mutation.operation == "update_settings"
    assert mutation.payload == {
        "color": "#ff0000",
        "line_width": 2.5,
        "line_style": "dot",
        "visible": False,
    }


def test_five_day_marker_store_creates_default_symbol_settings(tmp_path: Path) -> None:
    state = State(symbol="AAPL")

    with Cache(str(tmp_path / "test.db")) as cache:
        store = FiveDayMarkerStore(AppChartExtensionStoreContext(state, cache))
        store.load_for_symbol("AAPL")
        store.prepare_active_extensions()
        records = cache.get_extension_records("five_day_marker.settings", "AAPL")

    settings = store.settings()
    assert settings is not None
    assert settings.symbol == "AAPL"
    assert settings.enabled
    assert settings.color == "#800080"
    assert settings.line_style == "dash"
    assert len(records) == 1
    assert state.get_extension("five_day_marker") is not None


def test_five_day_marker_store_keeps_symbols_independent(tmp_path: Path) -> None:
    state = State(symbol="AAPL")

    with Cache(str(tmp_path / "test.db")) as cache:
        store = FiveDayMarkerStore(AppChartExtensionStoreContext(state, cache))
        store.load_for_symbol("AAPL")
        mutation = FiveDayMarkerIndicator().apply_config_to_series(
            "five_day_marker",
            FiveDayMarkerIndicator().default_params(),
            {
                "color": "#ff0000",
                "line_width": 2.5,
                "line_style": ChoiceParam("dot", LINE_STYLE_OPTIONS),
                "visible": False,
            },
        )
        assert mutation is not None
        store.apply(mutation)

        state.symbol = "MSFT"
        store.load_for_symbol("MSFT")
        msft = store.settings()

        state.symbol = "AAPL"
        store.load_for_symbol("AAPL")
        aapl = store.settings()

    assert msft is not None
    assert msft.color == "#800080"
    assert msft.visible
    assert aapl is not None
    assert aapl.color == "#ff0000"
    assert not aapl.visible


def test_five_day_marker_runtime_config_and_remove_are_symbol_scoped(
    tmp_path: Path,
) -> None:
    state = State(symbol="SPY", timeframe=Timeframe.DAILY)
    series = _series(Timeframe.DAILY, 8)

    with Cache(str(tmp_path / "test.db")) as cache:
        store = FiveDayMarkerStore(AppChartExtensionStoreContext(state, cache))
        store.load_for_symbol("SPY")
        runtime = _runtime(state, cache, store)
        render_passes = runtime.render_all(series)

        assert len(render_passes) == 1
        assert render_passes[0].state.series_keys == ["five_day_marker"]

        request = runtime.config_request("five_day_marker")
        assert request is not None
        edited = dict(request.params)
        edited["color"] = "#00ff00"
        edited["line_width"] = 3.0
        edited["line_style"] = ChoiceParam("dash_dot", LINE_STYLE_OPTIONS)
        edited["visible"] = False
        runtime.apply_config("five_day_marker", edited)
        configured = runtime.render_all(series)[0].render.vertical_lines[0]

        removal = runtime.remove("five_day_marker")
        assert state.get_extension("five_day_marker") is None

        state.symbol = "QQQ"
        store.load_for_symbol("QQQ")
        store.prepare_active_extensions()
        qqq_settings = store.settings()
        spy_records = cache.get_extension_records("five_day_marker.settings", "SPY")

    assert configured.color == "#00ff00"
    assert configured.line_width == 3.0
    assert configured.line_style == "dash_dot"
    assert not configured.visible
    assert removal is not None
    assert removal.series_keys == ["five_day_marker"]
    assert state.get_extension("five_day_marker") is not None
    assert spy_records[0].payload["enabled"] is False
    assert qqq_settings is not None
    assert qqq_settings.enabled


def _series(timeframe: Timeframe, count: int) -> OHLCVSeries:
    start = datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)
    if timeframe == Timeframe.DAILY:
        step = timedelta(days=1)
    elif timeframe == Timeframe.WEEKLY:
        step = timedelta(days=7)
    else:
        minutes = timeframe.minutes
        assert minutes is not None
        step = timedelta(minutes=minutes)
    return OHLCVSeries(
        symbol="SPY",
        timeframe=timeframe,
        bars=[
            Bar(
                timestamp=start + (step * i),
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.0 + i,
                volume=1_000,
            )
            for i in range(count)
        ],
    )


def _runtime(
    state: State,
    cache: Cache,
    store: FiveDayMarkerStore,
) -> ChartExtensionRuntime:
    return ChartExtensionRuntime(
        state,
        cache,
        ChartExtensionStore(state, cache, [store]),
        lookback_days=600,
    )
