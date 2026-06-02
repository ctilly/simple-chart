from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import bisect
from typing import Any

from app.extension_store import ChartExtensionStore
from app.state import ChartExtensionState, State
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
from simplechart.api import (
    ChartEvent,
    DrawingSession,
    DrawingToolResult,
    DragSession,
    HitTestResult,
    ChartExtensionAction,
    ChartExtensionConfig,
    ChartExtensionAddMode,
    ChartExtensionMutation,
    ChartExtensionRender,
    all_extensions,
    get_extension,
)


@dataclass
class ChartExtensionRenderPass:
    state: ChartExtensionState
    render: ChartExtensionRender
    render_target: str


@dataclass
class ChartExtensionRemoval:
    series_keys: list[str]
    render_target: str
    release_panel: bool = False


class ChartExtensionRuntime:

    def __init__(
        self,
        state: State,
        cache: Cache,
        extension_store: ChartExtensionStore,
        lookback_days: int,
    ) -> None:
        self._state = state
        self._cache = cache
        self._extension_store = extension_store
        self._lookback_days = lookback_days
        self._drag_state: ChartExtensionState | None = None
        self._drag_session: DragSession | None = None

    def render_all(self, series: OHLCVSeries) -> list[ChartExtensionRenderPass]:
        self._extension_store.prepare_active_extensions()
        daily_bars = self._daily_bars_for(series)

        passes: list[ChartExtensionRenderPass] = []
        for ext_state in self._state.extensions:
            ext_state.params = self._extension_store.params_for(
                ext_state.name,
                ext_state.params,
            )
            passes.append(self.render_one(ext_state, series, daily_bars))
        return passes

    def render_one(
        self,
        ext_state: ChartExtensionState,
        series: OHLCVSeries,
        daily_bars: list[Bar],
    ) -> ChartExtensionRenderPass:
        extension = get_extension(ext_state.name)
        # _daily_bars is a transient compute input (daily history for intraday
        # MA warmup). Merge it into a throwaway params dict for this render only,
        # never into the persisted ext_state.params — otherwise the full bar list
        # rides into per-symbol saved state and gets deep-copied on every switch.
        params = {**ext_state.params, "_daily_bars": daily_bars}
        render = extension.render(series, params)
        ext_state.series_keys = [
            series_render.key for series_render in render.series
        ] + [
            segment.key for segment in render.segments
        ] + [
            line.key for line in render.vertical_lines
        ] + [
            line.key for line in render.horizontal_lines
        ] + [
            label.key for label in render.axis_price_labels
        ]
        ext_state.series_visibility = {
            key: ext_state.series_visibility[key]
            for key in ext_state.series_keys
            if key in ext_state.series_visibility
        }
        return ChartExtensionRenderPass(
            state=ext_state,
            render=render,
            render_target=extension.render_target(),
        )

    def chart_event(
        self,
        series: OHLCVSeries,
        x: float,
        y: float = 0.0,
        button: str = "left",
        pixel_size_x: float = 0.0,
        pixel_size_y: float = 0.0,
    ) -> ChartEvent:
        bar_index = self._nearest_bar_index(series, x)
        timestamp_ms = None
        if bar_index is not None:
            timestamp_ms = int(series.bars[bar_index].timestamp.timestamp() * 1000)
        return ChartEvent(
            x=x,
            y=y,
            bar_index=bar_index,
            timestamp_ms=timestamp_ms,
            button=button,
            pixel_size_x=pixel_size_x,
            pixel_size_y=pixel_size_y,
        )

    def context_actions(
        self,
        series: OHLCVSeries,
        event: ChartEvent,
    ) -> list[ChartExtensionAction]:
        actions: list[ChartExtensionAction] = []
        for name, cls in all_extensions().items():
            extension = cls()
            ext_state = self._state.get_extension(name)
            params = ext_state.params if ext_state is not None else extension.default_params()
            params = self._extension_store.params_for(name, params)
            actions.extend(extension.context_actions(series, params, event))
        return actions

    def apply_action(
        self,
        series: OHLCVSeries,
        action: ChartExtensionAction,
        event: ChartEvent,
    ) -> None:
        extension = get_extension(action.extension_name)
        ext_state = self._state.get_extension(action.extension_name)
        params = ext_state.params if ext_state is not None else extension.default_params()
        params = self._extension_store.params_for(action.extension_name, params)
        mutation = extension.apply_action(series, params, action.action_key, event)
        if mutation is not None:
            self.apply_mutation(mutation)

    def begin_drag(
        self,
        series: OHLCVSeries,
        event: ChartEvent,
    ) -> bool:
        best_state: ChartExtensionState | None = None
        best_hit = None
        for ext_state in self._state.extensions:
            extension = get_extension(ext_state.name)
            params = self._params_for_state(ext_state)
            visible_keys = {
                key
                for key in ext_state.series_keys
                if ext_state.series_visibility.get(key, ext_state.visible)
            }
            hit = extension.hit_test(series, params, event, visible_keys)
            if hit is None:
                continue
            if best_hit is None or hit.priority > best_hit.priority:
                best_state = ext_state
                best_hit = hit
        if best_state is None or best_hit is None:
            return False
        extension = get_extension(best_state.name)
        self._drag_state = best_state
        self._drag_session = extension.begin_drag(
            series,
            self._params_for_state(best_state),
            best_hit,
        )
        return True

    def drag_to(
        self,
        series: OHLCVSeries,
        event: ChartEvent,
    ) -> ChartExtensionRenderPass | None:
        if self._drag_state is None or self._drag_session is None:
            return None
        extension = get_extension(self._drag_state.name)
        render = extension.drag_to(series, self._drag_session, event)
        if render is None:
            return None
        return ChartExtensionRenderPass(
            state=self._drag_state,
            render=render,
            render_target=extension.render_target(),
        )

    def finish_drag(
        self,
        series: OHLCVSeries,
        event: ChartEvent,
    ) -> ChartExtensionMutation | None:
        if self._drag_state is None or self._drag_session is None:
            return None
        extension = get_extension(self._drag_state.name)
        mutation = extension.finish_drag(series, self._drag_session, event)
        self._drag_state = None
        self._drag_session = None
        if mutation is not None:
            self.apply_mutation(mutation)
        return mutation

    def cancel_drag(self) -> ChartExtensionMutation | None:
        if self._drag_state is None or self._drag_session is None:
            return None
        extension = get_extension(self._drag_state.name)
        mutation = extension.cancel_drag(self._drag_session)
        self._drag_state = None
        self._drag_session = None
        if mutation is not None:
            self.apply_mutation(mutation)
        return mutation

    def start_drawing(
        self,
        extension_name: str,
        series: OHLCVSeries,
        event: ChartEvent,
    ) -> DrawingToolResult:
        extension = get_extension(extension_name)
        ext_state = self._state.get_extension(extension_name)
        params = ext_state.params if ext_state is not None else extension.default_params()
        params = self._extension_store.params_for(extension_name, params)
        result = extension.start_drawing(series, params, event)
        if result.mutation is not None:
            self.apply_mutation(result.mutation)
        return result

    def drawing_hit_test(
        self,
        series: OHLCVSeries,
        event: ChartEvent,
    ) -> HitTestResult | None:
        best_hit = None
        for ext_state in self._state.extensions:
            extension = get_extension(ext_state.name)
            if extension.add_mode() != ChartExtensionAddMode.TOOLBAR:
                continue
            params = self._params_for_state(ext_state)
            visible_keys = {
                key
                for key in ext_state.series_keys
                if ext_state.series_visibility.get(key, ext_state.visible)
            }
            hit = extension.hit_test(series, params, event, visible_keys)
            if hit is None:
                continue
            if best_hit is None or hit.priority > best_hit.priority:
                best_hit = hit
        return best_hit

    def preview_drawing(
        self,
        series: OHLCVSeries,
        session: DrawingSession,
        event: ChartEvent,
    ) -> ChartExtensionRenderPass | None:
        extension = get_extension(session.extension_name)
        render = extension.preview_drawing(series, session, event)
        if render is None:
            return None
        return ChartExtensionRenderPass(
            state=self._drawing_state(session, render),
            render=render,
            render_target=extension.render_target(),
        )

    def advance_drawing(
        self,
        series: OHLCVSeries,
        session: DrawingSession,
        event: ChartEvent,
    ) -> DrawingToolResult:
        extension = get_extension(session.extension_name)
        result = extension.advance_drawing(series, session, event)
        if result.mutation is not None:
            self.apply_mutation(result.mutation)
        return result

    def cancel_drawing(
        self,
        session: DrawingSession,
    ) -> ChartExtensionMutation | None:
        extension = get_extension(session.extension_name)
        mutation = extension.cancel_drawing(session)
        if mutation is not None:
            self.apply_mutation(mutation)
        return mutation

    def apply_mutation(self, mutation: ChartExtensionMutation) -> None:
        self._extension_store.apply(mutation)

    def toggle_visibility(self, series_key: str) -> tuple[list[str], bool] | None:
        ext_state = self._state.get_extension_by_series_key(series_key)
        if ext_state is None:
            return None
        extension = get_extension(ext_state.name)
        params = self._params_for_state(ext_state)
        if extension.toggles_series_independently(series_key, params):
            visible = not ext_state.series_visibility.get(series_key, ext_state.visible)
            ext_state.series_visibility[series_key] = visible
            return [series_key], visible

        ext_state.visible = not ext_state.visible
        return list(ext_state.series_keys), ext_state.visible

    def config_request(self, series_key: str) -> ChartExtensionConfig | None:
        ext_state = self._state.get_extension_by_series_key(series_key)
        if ext_state is None:
            return None
        extension = get_extension(ext_state.name)
        params = self._params_for_state(ext_state)
        request = extension.config_for_series(series_key, params)
        if request is not None:
            return request
        return ChartExtensionConfig(
            label=extension.label(),
            params=ext_state.params,
        )

    def apply_config(self, series_key: str, params: dict[str, Any]) -> None:
        ext_state = self._state.get_extension_by_series_key(series_key)
        if ext_state is None:
            return
        extension = get_extension(ext_state.name)
        mutation = extension.apply_config_to_series(
            series_key,
            self._params_for_state(ext_state),
            params,
        )
        if mutation is not None:
            self.apply_mutation(mutation)
            return
        ext_state.params = params

    def remove(self, series_key: str) -> ChartExtensionRemoval | None:
        ext_state = self._state.get_extension_by_series_key(series_key)
        if ext_state is None:
            return None
        extension = get_extension(ext_state.name)
        mutation = extension.remove_series(
            series_key,
            self._params_for_state(ext_state),
        )
        if mutation is not None:
            self.apply_mutation(mutation)
            return ChartExtensionRemoval(
                series_keys=[series_key],
                render_target=extension.render_target(),
            )

        render_target = extension.render_target()
        series_keys = list(ext_state.series_keys)
        self._state.extensions = [
            state for state in self._state.extensions
            if state is not ext_state
        ]
        release_panel = False
        if render_target != "chart":
            release_panel = not any(
                get_extension(state.name).render_target() == render_target
                for state in self._state.extensions
            )
        return ChartExtensionRemoval(
            series_keys=series_keys,
            render_target=render_target,
            release_panel=release_panel,
        )

    def _params_for_state(self, ext_state: ChartExtensionState) -> dict[str, Any]:
        return self._extension_store.params_for(ext_state.name, ext_state.params)

    def _drawing_state(
        self,
        session: DrawingSession,
        render: ChartExtensionRender,
    ) -> ChartExtensionState:
        keys = [
            series_render.key for series_render in render.series
        ] + [
            segment.key for segment in render.segments
        ] + [
            line.key for line in render.vertical_lines
        ] + [
            line.key for line in render.horizontal_lines
        ] + [
            label.key for label in render.axis_price_labels
        ]
        return ChartExtensionState(
            name=session.extension_name,
            params=session.working_params,
            series_keys=keys,
        )

    def _daily_bars_for(self, series: OHLCVSeries) -> list[Bar]:
        if not series.timeframe.is_intraday:
            return []
        now = datetime.now(tz=timezone.utc)
        start_ms = int(
            (now - timedelta(days=self._lookback_days)).timestamp() * 1000
        )
        end_ms = int(now.timestamp() * 1000)
        return self._cache.get_bars(
            series.symbol,
            Timeframe.DAILY,
            start_ms,
            end_ms,
        )

    def _nearest_bar_index(self, series: OHLCVSeries, x: float) -> int | None:
        if not series.bars:
            return None
        idx = int(x + 0.5)
        if 0 <= idx < len(series.bars) and abs(x - idx) <= 0.75:
            return idx

        return self._nearest_timestamp_bar_index(series, x)

    def _nearest_timestamp_bar_index(self, series: OHLCVSeries, x: float) -> int | None:
        if x < 100_000_000:
            return None
        x_ms = int(x * 1000) if x < 10_000_000_000 else int(x)
        timestamps = [
            int(bar.timestamp.timestamp() * 1000)
            for bar in series.bars
        ]
        idx = bisect.bisect_left(timestamps, x_ms)
        candidates: list[int] = []
        if idx < len(timestamps):
            candidates.append(idx)
        if idx > 0:
            candidates.append(idx - 1)
        if not candidates:
            return None
        return min(candidates, key=lambda candidate: abs(timestamps[candidate] - x_ms))
