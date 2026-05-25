from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.indicator_runtime import ChartExtensionRuntime
from app.indicator_runtime import IndicatorRuntime as LegacyIndicatorRuntime
from app.indicator_store import AppChartExtensionStoreContext, ChartExtensionStore
from app.indicator_store import IndicatorStore as LegacyIndicatorStore
from app.state import ChartExtensionState, State
from app.state import IndicatorState as LegacyIndicatorState
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
import indicators.avwap  # noqa: F401
import indicators.sma  # noqa: F401
from simplechart.api import (
    ChartEvent,
    DrawingSession,
    DrawingToolResult,
    HorizontalSegmentRender,
    Indicator,
    IndicatorAddMode,
    IndicatorMutation,
    IndicatorRender,
    VerticalLineRender,
    register_indicator,
)
from indicators.avwap.anchor_store import AvwapAnchorStore
from indicators.avwap.models import AnchorRecord


def test_app_layer_legacy_names_alias_chart_extension_names() -> None:
    assert LegacyIndicatorRuntime is ChartExtensionRuntime
    assert LegacyIndicatorStore is ChartExtensionStore
    assert LegacyIndicatorState is ChartExtensionState


class SegmentOnlyIndicator(Indicator):

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
    ) -> IndicatorRender:
        return IndicatorRender(
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


register_indicator(SegmentOnlyIndicator)


class VerticalLineOnlyIndicator(Indicator):

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
    ) -> IndicatorRender:
        return IndicatorRender(
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


register_indicator(VerticalLineOnlyIndicator)


class DrawingFixtureIndicator(Indicator):

    def name(self) -> str:
        return "test_drawing_fixture"

    def label(self) -> str:
        return "Test Drawing Fixture"

    def default_params(self) -> dict[str, Any]:
        return {"starts": 0}

    def add_mode(self) -> IndicatorAddMode:
        return IndicatorAddMode.TOOLBAR

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
                indicator_name=self.name(),
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
    ) -> IndicatorRender | None:
        return IndicatorRender(
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
            mutation=IndicatorMutation(
                indicator_name=self.name(),
                operation="commit",
                payload={"x": event.x, "y": event.y},
            ),
            done=True,
        )

    def cancel_drawing(
        self,
        session: DrawingSession,
    ) -> IndicatorMutation | None:
        return IndicatorMutation(
            indicator_name=self.name(),
            operation="cancel",
            payload={"tool_key": session.tool_key},
        )


register_indicator(DrawingFixtureIndicator)


class RecordingStore:

    def __init__(self) -> None:
        self.mutations: list[IndicatorMutation] = []

    def load_for_symbol(self, symbol: str) -> None:
        pass

    def apply(self, mutation: IndicatorMutation) -> None:
        self.mutations.append(mutation)

    def prepare_active_indicators(self) -> None:
        pass

    def params_for(
        self,
        indicator_name: str,
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

    assert state.get_indicator("avwap") is not None
    assert len(passes) == 1
    assert passes[0].state.name == "avwap"
    assert passes[0].state.params["anchors"] == store.anchors()
    assert passes[0].state.series_keys == ["avwap_anchor_10"]


def test_runtime_updates_series_keys_and_preserves_current_visibility(
    tmp_path: Path,
) -> None:
    state = State(symbol="SPY")
    state.indicators = [
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
    assert state.indicators[0].series_keys == ["sma_2"]
    assert state.indicators[0].series_visibility == {"sma_2": False}


def test_runtime_tracks_horizontal_segment_keys_and_visibility(
    tmp_path: Path,
) -> None:
    state = State(symbol="SPY")
    state.indicators = [
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
    assert state.indicators[0].series_keys == ["test_segment_only_line"]
    assert state.indicators[0].series_visibility == {"test_segment_only_line": False}


def test_runtime_tracks_vertical_line_keys_and_visibility(tmp_path: Path) -> None:
    state = State(symbol="SPY")
    state.indicators = [
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
    assert state.indicators[0].series_keys == ["test_vertical_line_only_line"]
    assert state.indicators[0].series_visibility == {"test_vertical_line_only_line": False}


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
        indicator_name="test_drawing_fixture",
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
    assert state.get_indicator("avwap") is not None


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
        state.indicators = [ChartExtensionState(name="avwap", params={"anchors": []})]
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
        state.indicators = [ChartExtensionState(name="avwap", params={"anchors": []})]
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
        state.indicators = [
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
    assert state.get_indicator("avwap") is None


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
        state.indicators = [
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
            IndicatorMutation(
                indicator_name="avwap",
                operation="add_anchor",
                payload={
                    "anchor_ts": 1_700_000_000_000,
                    "label": "2023-11-14",
                    "color": "#00FF88",
                },
            )
        )

        assert state.get_indicator("avwap") is not None
        assert len(store.anchors()) == 1

        store.apply(
            IndicatorMutation(
                indicator_name="avwap",
                operation="delete_anchor",
                payload={"anchor": store.anchors()[0]},
            )
        )
        anchors = _get_avwap_anchors(cache, "SPY")

    assert store.anchors() == []
    assert anchors == []
    assert state.get_indicator("avwap") is None


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
    indicator_store = ChartExtensionStore(state, cache, handlers)
    return ChartExtensionRuntime(
        state,
        cache,
        indicator_store,
        lookback_days=600,
    )


def _put_avwap_anchor(cache: Cache, anchor: AnchorRecord) -> AnchorRecord:
    record = cache.put_indicator_record(
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
        for record in cache.get_indicator_records("avwap.anchors", symbol)
    ]


def _anchor_payload(anchor: AnchorRecord) -> dict[str, object]:
    return {
        "label": anchor.label,
        "color": anchor.color,
        "line_width": anchor.line_width,
        "line_style": anchor.line_style,
        "show_anchor": anchor.show_anchor,
    }
