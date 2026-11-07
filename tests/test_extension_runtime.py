from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.extension_runtime import ChartExtensionRuntime
from app.extension_store import AppChartExtensionStoreContext, ChartExtensionStore
from app.state import ChartExtensionState, State
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
import indicators.avwap  # noqa: F401
import indicators.sma  # noqa: F401
from simplechart.api import (
    ChartEvent,
    DrawingSession,
    DrawingToolResult,
    HorizontalSegmentRender,
    ChartExtension,
    ChartExtensionAddMode,
    ChartExtensionMutation,
    ChartExtensionRender,
    HitTestResult,
    VerticalLineRender,
    register_extension,
)
from indicators.avwap.anchor_store import AvwapAnchorStore
from indicators.avwap.models import AnchorRecord


class SegmentOnlyIndicator(ChartExtension):

    def name(self) -> str:
        return "test_segment_only"

    def label(self) -> str:
        return "Test Segment Only"

    def default_params(self) -> dict[str, Any]:
        return {}

    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        return {}

    def render(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> ChartExtensionRender:
        return ChartExtensionRender(
            segments=[
                HorizontalSegmentRender(
                    key="test_segment_only_line",
                    x_start=0.0,
                    x_end=1.0,
                    y_value=100.0,
                    label="Test Segment Only",
                    color="#ffffff",
                    line_width=1.0,
                )
            ]
        )


register_extension(SegmentOnlyIndicator)


class VerticalLineOnlyIndicator(ChartExtension):

    def name(self) -> str:
        return "test_vertical_line_only"

    def label(self) -> str:
        return "Test Vertical Line Only"

    def default_params(self) -> dict[str, Any]:
        return {}

    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        return {}

    def render(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> ChartExtensionRender:
        return ChartExtensionRender(
            vertical_lines=[
                VerticalLineRender(
                    key="test_vertical_line_only_line",
                    x_index=1.0,
                    label="Test Vertical Line Only",
                    color="#ffffff",
                    line_width=1.0,
                )
            ]
        )


register_extension(VerticalLineOnlyIndicator)


class DrawingFixtureIndicator(ChartExtension):

    def name(self) -> str:
        return "test_drawing_fixture"

    def label(self) -> str:
        return "Test Drawing Fixture"

    def default_params(self) -> dict[str, Any]:
        return {"starts": 0}

    def add_mode(self) -> ChartExtensionAddMode:
        return ChartExtensionAddMode.TOOLBAR

    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        return {}

    def start_drawing(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        event: ChartEvent,
    ) -> DrawingToolResult:
        return DrawingToolResult(
            session=DrawingSession(
                extension_name=self.name(),
                tool_key="line",
                original_params=dict(params),
                working_params={"start_x": event.x, "start_y": event.y},
            )
        )

    def preview_drawing(
        self,
        series: OHLCVSeries,
        session: DrawingSession,
        event: ChartEvent,
    ) -> ChartExtensionRender | None:
        return ChartExtensionRender(
            segments=[
                HorizontalSegmentRender(
                    key="test_drawing_preview",
                    x_start=float(session.working_params["start_x"]),
                    x_end=event.x,
                    y_value=event.y,
                    label="Preview",
                    color="#00ff00",
                    line_width=1.0,
                )
            ]
        )

    def advance_drawing(
        self,
        series: OHLCVSeries,
        session: DrawingSession,
        event: ChartEvent,
    ) -> DrawingToolResult:
        return DrawingToolResult(
            mutation=ChartExtensionMutation(
                extension_name=self.name(),
                operation="commit",
                payload={"x": event.x, "y": event.y},
            ),
            done=True,
        )

    def cancel_drawing(
        self,
        session: DrawingSession,
    ) -> ChartExtensionMutation | None:
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="cancel",
            payload={"tool_key": session.tool_key},
        )


register_extension(DrawingFixtureIndicator)


class RecordingStore:

    def __init__(self) -> None:
        self.mutations: list[ChartExtensionMutation] = []

    def load_for_symbol(self, symbol: str) -> None:
        pass

    def apply(self, mutation: ChartExtensionMutation) -> None:
        self.mutations.append(mutation)

    def prepare_active_extensions(self) -> None:
        pass

    def params_for(
        self,
        extension_name: str,
        base_params: dict[str, Any],
    ) -> dict[str, Any]:
        return base_params


def test_runtime_adds_avwap_state_when_anchors_exist(tmp_path: Path) -> None:
    state = State(symbol="SPY")

    with Cache(str(tmp_path / "test.db")) as cache:
        store = AvwapAnchorStore(AppChartExtensionStoreContext(state, cache))
        store.restore_anchors([
            AnchorRecord(
                symbol="SPY",
                anchor_ts=int(_series().bars[1].timestamp.timestamp() * 1000),
                label="2026-01-03",
                color="#00FF88",
                anchor_id=10,
            )
        ])
        runtime = _runtime(state, cache, store)
        passes = runtime.render_all(_series())

    assert state.get_extension("avwap") is not None
    assert len(passes) == 1
    assert passes[0].state.name == "avwap"
    assert passes[0].state.params["anchors"] == store.anchors()
    assert passes[0].state.series_keys == ["avwap_anchor_10"]


def test_runtime_updates_series_keys_and_preserves_current_visibility(
    tmp_path: Path,
) -> None:
    state = State(symbol="SPY")
    state.extensions = [
        ChartExtensionState(
            name="sma",
            params={"days": 2, "color": "#ffffff"},
            series_visibility={"sma_2": False, "stale": True},
        )
    ]

    with Cache(str(tmp_path / "test.db")) as cache:
        runtime = _runtime(state, cache)
        passes = runtime.render_all(_series())

    assert len(passes) == 1
    assert state.extensions[0].series_keys == ["sma_2"]
    assert state.extensions[0].series_visibility == {"sma_2": False}


def test_runtime_tracks_horizontal_segment_keys_and_visibility(
    tmp_path: Path,
) -> None:
    state = State(symbol="SPY")
    state.extensions = [
        ChartExtensionState(
            name="test_segment_only",
            params={},
            series_visibility={"test_segment_only_line": False, "stale": True},
        )
    ]

    with Cache(str(tmp_path / "test.db")) as cache:
        runtime = _runtime(state, cache)
        passes = runtime.render_all(_series())

    assert len(passes) == 1
    assert state.extensions[0].series_keys == ["test_segment_only_line"]
    assert state.extensions[0].series_visibility == {"test_segment_only_line": False}


def test_runtime_tracks_vertical_line_keys_and_visibility(tmp_path: Path) -> None:
    state = State(symbol="SPY")
    state.extensions = [
        ChartExtensionState(
            name="test_vertical_line_only",
            params={},
            series_visibility={"test_vertical_line_only_line": False, "stale": True},
        )
    ]

    with Cache(str(tmp_path / "test.db")) as cache:
        runtime = _runtime(state, cache)
        passes = runtime.render_all(_series())

    assert len(passes) == 1
    assert state.extensions[0].series_keys == ["test_vertical_line_only_line"]
    assert state.extensions[0].series_visibility == {"test_vertical_line_only_line": False}


def test_runtime_routes_toolbar_drawing_lifecycle(tmp_path: Path) -> None:
    state = State(symbol="SPY")
    series = _series()

    with Cache(str(tmp_path / "test.db")) as cache:
        store = RecordingStore()
        runtime = _runtime(state, cache, store)
        start = runtime.start_drawing(
            "test_drawing_fixture",
            series,
            runtime.chart_event(series, 1.0, 100.0),
        )
        session = start.session
        assert session is not None
        assert session.working_params == {"start_x": 1.0, "start_y": 100.0}

        preview = runtime.preview_drawing(
            series,
            session,
            runtime.chart_event(series, 2.0, 101.0),
        )
        assert preview is not None
        assert preview.state.name == "test_drawing_fixture"
        assert preview.state.series_keys == ["test_drawing_preview"]
        assert preview.render.segments[0].x_start == 1.0
        assert preview.render.segments[0].x_end == 2.0
        assert store.mutations == []

        result = runtime.advance_drawing(
            series,
            session,
            runtime.chart_event(series, 3.0, 102.0),
        )
        assert result.done
        assert len(store.mutations) == 1
        assert store.mutations[0].operation == "commit"


def test_runtime_applies_toolbar_drawing_cancel_mutation(tmp_path: Path) -> None:
    state = State(symbol="SPY")
    session = DrawingSession(
        extension_name="test_drawing_fixture",
        tool_key="line",
        original_params={},
        working_params={},
    )

    with Cache(str(tmp_path / "test.db")) as cache:
        store = RecordingStore()
        runtime = _runtime(state, cache, store)
        mutation = runtime.cancel_drawing(session)

    assert mutation is not None
    assert mutation.operation == "cancel"
    assert [stored.operation for stored in store.mutations] == ["cancel"]


def test_runtime_chart_event_resolves_timestamp_x(tmp_path: Path) -> None:
    state = State(symbol="SPY")
    series = _series()
    x_value = series.bars[2].timestamp.timestamp()

    with Cache(str(tmp_path / "test.db")) as cache:
        runtime = _runtime(state, cache)
        event = runtime.chart_event(series, x_value)

    assert event.bar_index == 2
    assert event.timestamp_ms == int(series.bars[2].timestamp.timestamp() * 1000)


def test_runtime_applies_plugin_context_action_to_add_avwap_anchor(
    tmp_path: Path,
) -> None:
    state = State(symbol="SPY")
    series = _series()

    with Cache(str(tmp_path / "test.db")) as cache:
        store = AvwapAnchorStore(AppChartExtensionStoreContext(state, cache))
        runtime = _runtime(state, cache, store)
        event = runtime.chart_event(series, 2.0, button="right")
        actions = runtime.context_actions(series, event)

        assert [action.label for action in actions] == ["Add AVWAP here"]

        runtime.apply_action(series, actions[0], event)
        anchors = _get_avwap_anchors(cache, "SPY")

    assert len(store.anchors()) == 1
    assert len(anchors) == 1
    assert store.anchors()[0].anchor_ts == int(series.bars[2].timestamp.timestamp() * 1000)
    assert state.get_extension("avwap") is not None


def test_runtime_routes_avwap_drag_and_persists_finish(tmp_path: Path) -> None:
    state = State(symbol="SPY")
    series = _series()
    original_ts = int(series.bars[1].timestamp.timestamp() * 1000)
    target_ts = int(series.bars[3].timestamp.timestamp() * 1000)

    with Cache(str(tmp_path / "test.db")) as cache:
        persisted = _put_avwap_anchor(
            cache,
            AnchorRecord(
                symbol="SPY",
                anchor_ts=original_ts,
                label="2026-01-03",
                color="#00FF88",
                show_anchor=True,
            ),
        )
        store = AvwapAnchorStore(AppChartExtensionStoreContext(state, cache))
        store.restore_anchors([persisted])
        state.extensions = [ChartExtensionState(name="avwap", params={"anchors": []})]
        runtime = _runtime(state, cache, store)
        runtime.render_all(series)

        assert runtime.begin_drag(series, runtime.chart_event(series, 1.0, 100.0))
        render_pass = runtime.drag_to(series, runtime.chart_event(series, 3.0, 102.0))
        assert render_pass is not None
        assert render_pass.render.series[0].key == f"avwap_anchor_{persisted.anchor_id}"
        assert render_pass.render.markers[0].x_index == 3

        runtime.finish_drag(series, runtime.chart_event(series, 3.0, 102.0))
        anchors = _get_avwap_anchors(cache, "SPY")

    assert store.anchors()[0].anchor_ts == target_ts
    assert anchors[0].anchor_ts == target_ts


def test_runtime_skips_avwap_drag_redraw_until_anchor_bar_changes(tmp_path: Path) -> None:
    state = State(symbol="SPY")
    series = _series()
    anchor_ts = int(series.bars[1].timestamp.timestamp() * 1000)

    with Cache(str(tmp_path / "test.db")) as cache:
        persisted = _put_avwap_anchor(
            cache,
            AnchorRecord(
                symbol="SPY",
                anchor_ts=anchor_ts,
                label="2026-01-03",
                color="#00FF88",
            ),
        )
        store = AvwapAnchorStore(AppChartExtensionStoreContext(state, cache))
        store.restore_anchors([persisted])
        state.extensions = [ChartExtensionState(name="avwap", params={"anchors": []})]
        runtime = _runtime(state, cache, store)
        runtime.render_all(series)

        assert runtime.begin_drag(series, runtime.chart_event(series, 1.0, 100.0))
        render_pass = runtime.drag_to(series, runtime.chart_event(series, 1.2, 100.5))

    assert render_pass is None


def test_runtime_removes_avwap_anchor_through_store(tmp_path: Path) -> None:
    state = State(symbol="SPY")
    series = _series()
    anchor_ts = int(series.bars[1].timestamp.timestamp() * 1000)

    with Cache(str(tmp_path / "test.db")) as cache:
        persisted = _put_avwap_anchor(
            cache,
            AnchorRecord(
                symbol="SPY",
                anchor_ts=anchor_ts,
                label="2026-01-03",
                color="#00FF88",
                anchor_id=10,
            ),
        )
        store = AvwapAnchorStore(AppChartExtensionStoreContext(state, cache))
        store.restore_anchors([persisted])
        state.extensions = [
            ChartExtensionState(
                name="avwap",
                params={"anchors": []},
                series_keys=[f"avwap_anchor_{persisted.anchor_id}"],
                series_visibility={f"avwap_anchor_{persisted.anchor_id}": False},
            )
        ]
        runtime = _runtime(state, cache, store)

        removal = runtime.remove(f"avwap_anchor_{persisted.anchor_id}")
        anchors = _get_avwap_anchors(cache, "SPY")

    assert removal is not None
    assert removal.series_keys == [f"avwap_anchor_{persisted.anchor_id}"]
    assert store.anchors() == []
    assert anchors == []
    assert state.get_extension("avwap") is None


def test_runtime_uses_indicator_owned_avwap_config(tmp_path: Path) -> None:
    state = State(symbol="SPY")
    series = _series()
    anchor_ts = int(series.bars[1].timestamp.timestamp() * 1000)

    with Cache(str(tmp_path / "test.db")) as cache:
        persisted = _put_avwap_anchor(
            cache,
            AnchorRecord(
                symbol="SPY",
                anchor_ts=anchor_ts,
                label="2026-01-03",
                color="#00FF88",
                anchor_id=10,
            ),
        )
        store = AvwapAnchorStore(AppChartExtensionStoreContext(state, cache))
        store.restore_anchors([persisted])
        state.extensions = [
            ChartExtensionState(
                name="avwap",
                params={"anchors": []},
                series_keys=[f"avwap_anchor_{persisted.anchor_id}"],
            )
        ]
        runtime = _runtime(state, cache, store)

        request = runtime.config_request(f"avwap_anchor_{persisted.anchor_id}")
        assert request is not None
        assert request.label == "AVWAP 2026-01-03"

        edited = dict(request.params)
        edited["color"] = "#FFB000"
        runtime.apply_config(f"avwap_anchor_{persisted.anchor_id}", edited)
        anchors = _get_avwap_anchors(cache, "SPY")

    assert anchors[0].color == "#FFB000"


def test_avwap_anchor_store_applies_avwap_mutations(tmp_path: Path) -> None:
    state = State(symbol="SPY")

    with Cache(str(tmp_path / "test.db")) as cache:
        store = AvwapAnchorStore(AppChartExtensionStoreContext(state, cache))
        store.apply(
            ChartExtensionMutation(
                extension_name="avwap",
                operation="add_anchor",
                payload={
                    "anchor_ts": 1_700_000_000_000,
                    "label": "2023-11-14",
                    "color": "#00FF88",
                },
            )
        )

        assert state.get_extension("avwap") is not None
        assert len(store.anchors()) == 1

        store.apply(
            ChartExtensionMutation(
                extension_name="avwap",
                operation="delete_anchor",
                payload={"anchor": store.anchors()[0]},
            )
        )
        anchors = _get_avwap_anchors(cache, "SPY")

    assert store.anchors() == []
    assert anchors == []
    assert state.get_extension("avwap") is None


def _series() -> OHLCVSeries:
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    bars = [
        Bar(
            timestamp=start + timedelta(days=i),
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.0 + i,
            volume=1_000,
        )
        for i in range(5)
    ]
    return OHLCVSeries(symbol="SPY", timeframe=Timeframe.DAILY, bars=bars)


def _runtime(
    state: State,
    cache: Cache,
    store: AvwapAnchorStore | RecordingStore | None = None,
) -> ChartExtensionRuntime:
    handlers = [store] if store is not None else None
    extension_store = ChartExtensionStore(state, cache, handlers)
    return ChartExtensionRuntime(
        state,
        cache,
        extension_store,
        lookback_days=600,
    )


def _put_avwap_anchor(cache: Cache, anchor: AnchorRecord) -> AnchorRecord:
    record = cache.put_extension_record(
        "avwap.anchors",
        anchor.symbol,
        anchor.anchor_ts,
        _anchor_payload(anchor),
    )
    return AnchorRecord(
        symbol=record.symbol,
        anchor_ts=record.sort_key,
        label=anchor.label,
        color=anchor.color,
        line_width=anchor.line_width,
        line_style=anchor.line_style,
        show_anchor=anchor.show_anchor,
        anchor_id=record.record_id,
    )


def _get_avwap_anchors(cache: Cache, symbol: str) -> list[AnchorRecord]:
    return [
        AnchorRecord(
            symbol=record.symbol,
            anchor_ts=record.sort_key,
            label=str(record.payload["label"]),
            color=str(record.payload["color"]),
            line_width=float(record.payload["line_width"]),
            line_style=str(record.payload["line_style"]),
            show_anchor=bool(record.payload["show_anchor"]),
            anchor_id=record.record_id,
        )
        for record in cache.get_extension_records("avwap.anchors", symbol)
    ]


def _anchor_payload(anchor: AnchorRecord) -> dict[str, object]:
    return {
        "label": anchor.label,
        "color": anchor.color,
        "line_width": anchor.line_width,
        "line_style": anchor.line_style,
        "show_anchor": anchor.show_anchor,
    }


class FullHistoryTool(ChartExtension):
    """Toolbar drawing that always hits; requires full history by default."""

    def name(self) -> str:
        return "test_full_history_tool"

    def label(self) -> str:
        return "Test Full History Tool"

    def default_params(self) -> dict[str, Any]:
        return {}

    def add_mode(self) -> ChartExtensionAddMode:
        return ChartExtensionAddMode.TOOLBAR

    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        return {}

    def render(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> ChartExtensionRender:
        return ChartExtensionRender(
            vertical_lines=[
                VerticalLineRender(
                    key="test_full_history_line",
                    x_index=0.0,
                    label="Test",
                    color="#ffffff",
                    line_width=1.0,
                )
            ]
        )

    def hit_test(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        event: ChartEvent,
        visible_keys: set[str],
    ) -> HitTestResult | None:
        if "test_full_history_line" not in visible_keys:
            return None
        return HitTestResult(self.name(), "test_full_history_line")


register_extension(FullHistoryTool)


class InterimSegmentIndicator(SegmentOnlyIndicator):
    """Opts out of full history, so it renders while history loads."""

    def name(self) -> str:
        return "test_interim_segment"

    def history_lookback_days(self, params: dict[str, Any]) -> int | None:
        return 0


register_extension(InterimSegmentIndicator)


def test_interim_render_skips_full_history_extensions_without_pruning_state(
    tmp_path: Path,
) -> None:
    state = State(symbol="SPY")
    state.extensions = [
        ChartExtensionState(
            name="sma",
            params={"days": 2, "color": "#ffffff"},
            series_keys=["sma_2"],
            series_visibility={"sma_2": False},
        ),
        ChartExtensionState(name="test_segment_only", params={}),
        ChartExtensionState(name="test_interim_segment", params={}),
    ]

    with Cache(str(tmp_path / "test.db")) as cache:
        runtime = _runtime(state, cache)
        interim = runtime.render_all(_series(), history_complete=False)
        interim_sma = state.extensions[0]
        # Default (unknown plugin): deferred. Explicit opt-out: shown.
        assert [render_pass.state.name for render_pass in interim] == [
            "test_interim_segment"
        ]
        assert interim_sma.series_keys == ["sma_2"]
        assert interim_sma.series_visibility == {"sma_2": False}

        complete = runtime.render_all(_series())

    assert [render_pass.state.name for render_pass in complete] == [
        "sma",
        "test_segment_only",
        "test_interim_segment",
    ]
    assert state.extensions[0].series_visibility == {"sma_2": False}


def test_deferred_drawing_is_not_hit_by_drag_or_context_menu(
    tmp_path: Path,
) -> None:
    state = State(symbol="SPY")
    state.extensions = [ChartExtensionState(name="test_full_history_tool", params={})]
    series = _series()

    with Cache(str(tmp_path / "test.db")) as cache:
        runtime = _runtime(state, cache)
        runtime.render_all(series)
        event = runtime.chart_event(series, 0.0, 100.0)
        assert runtime.drawing_hit_test(series, event) is not None

        runtime.render_all(series, history_complete=False)
        assert runtime.drawing_hit_test(series, event) is None
        assert not runtime.begin_drag(series, event)

        runtime.render_all(series)
        assert runtime.drawing_hit_test(series, event) is not None
        assert runtime.begin_drag(series, event)


def test_builtin_extensions_declare_interim_history_needs() -> None:
    import indicators.avwap  # noqa: F401
    import indicators.ema  # noqa: F401
    import indicators.five_day_marker  # noqa: F401
    import indicators.pivot_points  # noqa: F401
    import indicators.rsi  # noqa: F401
    import tools.erase  # noqa: F401
    import tools.fib_retracement  # noqa: F401
    import tools.horizontal_line  # noqa: F401
    import tools.poly_line  # noqa: F401
    import tools.trend_line  # noqa: F401
    import tools.vertical_line  # noqa: F401
    from simplechart.api import get_extension

    days = {"days": 20}
    expected: dict[str, int | None] = {
        "pivot_points": 0, "five_day_marker": 0, "horizontal_line": 0,
        "fib_retracement": 0, "erase": 0, "avwap": 0,
        "sma": 20, "ema": 80, "rsi": 160,
        "trend_line": None, "poly_line": None, "vertical_line": None,
    }

    for name, lookback in expected.items():
        assert get_extension(name).history_lookback_days(days) == lookback, name


def _intraday_series(count: int) -> OHLCVSeries:
    start = datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc)
    bars = [
        Bar(
            timestamp=start + timedelta(minutes=5 * i),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0 + (i % 7),
            volume=1_000,
        )
        for i in range(count)
    ]
    return OHLCVSeries(symbol="SPY", timeframe=Timeframe.MIN5, bars=bars)


def test_interim_render_shows_extensions_whose_lookback_is_loaded(
    tmp_path: Path,
) -> None:
    # One 5m session is 78 bars; the initial view is 200 bars. 300 bars cover
    # a 1-day lookback (278) but not a 2-day lookback (356).
    series = _intraday_series(300)
    state = State(symbol="SPY")
    state.extensions = [
        ChartExtensionState(name="sma", params={"days": 1, "color": "#ffffff"}),
        ChartExtensionState(name="sma", params={"days": 2, "color": "#ffffff"}),
    ]

    with Cache(str(tmp_path / "test.db")) as cache:
        runtime = _runtime(state, cache)
        interim = runtime.render_all(series, history_complete=False)

        assert [p.state.params["days"] for p in interim] == [1]
        pending = runtime.pending_history_start_ms
        first_ms = int(series.bars[0].timestamp.timestamp() * 1000)
        assert pending is not None and pending < first_ms

        runtime.render_all(series)
        assert runtime.pending_history_start_ms is None


def test_interim_avwap_omits_unloaded_anchors_and_keeps_their_visibility(
    tmp_path: Path,
) -> None:
    series = _intraday_series(300)
    first_ms = int(series.bars[0].timestamp.timestamp() * 1000)
    hour_ms = 3_600_000
    anchors = [
        AnchorRecord("SPY", first_ms + hour_ms, "loaded", "#ffffff", anchor_id=1),
        AnchorRecord("SPY", first_ms - 2 * hour_ms, "older", "#ffffff", anchor_id=2),
        AnchorRecord("SPY", first_ms - 9 * hour_ms, "oldest", "#ffffff", anchor_id=3),
    ]
    state = State(symbol="SPY")
    state.extensions = [
        ChartExtensionState(
            name="avwap",
            params={},
            series_visibility={"avwap_anchor_2": False},
        )
    ]

    with Cache(str(tmp_path / "test.db")) as cache:
        store = AvwapAnchorStore(AppChartExtensionStoreContext(state, cache))
        store.restore_anchors(anchors)
        runtime = _runtime(state, cache, store)
        runtime.render_all(series, history_complete=False)
        avwap = state.extensions[0]

        assert avwap.series_keys == ["avwap_anchor_1"]
        assert avwap.series_visibility == {"avwap_anchor_2": False}
        # The newest omitted anchor is the next one history must reach.
        assert runtime.pending_history_start_ms == first_ms - 2 * hour_ms

        runtime.render_all(series)
        assert avwap.series_keys == [
            "avwap_anchor_1", "avwap_anchor_2", "avwap_anchor_3",
        ]
        assert avwap.series_visibility == {"avwap_anchor_2": False}
        assert runtime.pending_history_start_ms is None
