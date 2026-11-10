from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import bisect
import math
from typing import Any

from app.extension_store import ChartExtensionStore
from app.state import ChartExtensionState, State
from chart.window import INITIAL_VISIBLE_BARS
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
from simplechart.extensions._guard import call_extension, is_disabled
from simplechart.api import (
    ChartEvent,
    ChartExtension,
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
    bars_for_n_days,
)

_DAY_MS = 86_400_000


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
        cache_namespace: str = "yfinance",
    ) -> None:
        self._state = state
        self._cache = cache
        self._extension_store = extension_store
        self._lookback_days = lookback_days
        self._cache_namespace = cache_namespace
        self._deferred_names: set[str] = set()
        self._history_complete = True
        self._pending_history_ms: dict[str, int] = {}
        self._drag_state: ChartExtensionState | None = None
        self._drag_session: DragSession | None = None
        # Render target each extension last rendered to, recorded by the
        # host so cleanup after a failure never calls plugin code.
        self._render_targets: dict[str, str] = {}

    @property
    def drag_active(self) -> bool:
        return self._drag_session is not None

    def set_cache_namespace(self, cache_namespace: str) -> None:
        self._cache_namespace = cache_namespace

    @property
    def pending_history_start_ms(self) -> int | None:
        """
        While older history loads, the newest timestamp loaded history must
        reach for another extension (or omitted extension content) to render.
        None when nothing is waiting on history.
        """
        return max(self._pending_history_ms.values(), default=None)

    def render_all(
        self,
        series: OHLCVSeries,
        history_complete: bool = True,
    ) -> list[ChartExtensionRenderPass]:
        """
        Render every active extension. While older history is still loading
        (history_complete=False), an extension whose history lookback the
        loaded bars do not yet cover is skipped and excluded from hit tests;
        its state is left untouched so saved visibility survives until it
        renders. Turned-off extensions are skipped.
        """
        self._extension_store.prepare_active_extensions()
        daily_bars = self._daily_bars_for(series)

        passes: list[ChartExtensionRenderPass] = []
        self._deferred_names = set()
        self._history_complete = history_complete
        self._pending_history_ms = {}
        for ext_state in self._state.extensions:
            if is_disabled(ext_state.name):
                continue
            ext_state.params = self._extension_store.params_for(
                ext_state.name,
                ext_state.params,
            )
            if not history_complete:
                extension = guarded_extension(ext_state.name)
                if extension is None:
                    continue
                lookback_days = call_extension(
                    ext_state.name,
                    extension.history_lookback_days,
                    ext_state.params,
                )
                if is_disabled(ext_state.name):
                    continue
                if not _lookback_loaded(series, lookback_days):
                    self._deferred_names.add(ext_state.name)
                    if lookback_days is not None and series.bars:
                        self._pending_history_ms[ext_state.name] = (
                            _lookback_start_ms(series, lookback_days)
                        )
                    continue
            render_pass = self.render_one(ext_state, series, daily_bars)
            if render_pass is not None:
                passes.append(render_pass)
        return passes

    def render_one(
        self,
        ext_state: ChartExtensionState,
        series: OHLCVSeries,
        daily_bars: list[Bar],
    ) -> ChartExtensionRenderPass | None:
        extension = guarded_extension(ext_state.name)
        if extension is None:
            return None
        # _daily_bars is a transient compute input (daily history for intraday
        # MA warmup). Merge it into a throwaway params dict for this render only,
        # never into the persisted ext_state.params — otherwise the full bar list
        # rides into per-symbol saved state and gets deep-copied on every switch.
        # _history_complete tells anchored extensions whether bars older than
        # the loaded series may still arrive.
        params = {
            **ext_state.params,
            "_daily_bars": daily_bars,
            "_history_complete": self._history_complete,
        }
        render = call_extension(ext_state.name, extension.render, series, params)
        if render is None:
            return None
        render_target = call_extension(ext_state.name, extension.render_target)
        if render_target is None:
            return None
        if render.pending_history_start_ms is None:
            self._pending_history_ms.pop(ext_state.name, None)
        else:
            self._pending_history_ms[ext_state.name] = (
                render.pending_history_start_ms
            )
        ext_state.series_keys = [
            series_render.key for series_render in render.series
        ] + [
            segment.key for segment in render.segments
        ] + [
            line.key for line in render.vertical_lines
        ] + [
            line.key for line in render.horizontal_lines
        ] + [
            polyline.key for polyline in render.polylines
        ] + [
            label.key for label in render.axis_price_labels
        ]
        # Content omitted while history loads keeps its saved visibility.
        if self._history_complete:
            ext_state.series_visibility = {
                key: ext_state.series_visibility[key]
                for key in ext_state.series_keys
                if key in ext_state.series_visibility
            }
        self._render_targets[ext_state.name] = render_target
        return ChartExtensionRenderPass(
            state=ext_state,
            render=render,
            render_target=render_target,
        )

    def rendered_target(self, extension_name: str) -> str | None:
        """The render target an extension last rendered to, if it has rendered."""
        return self._render_targets.get(extension_name)

    def release_turned_off_drag(self) -> None:
        """Drop the drag session of an extension that was turned off."""
        if self._drag_state is not None and is_disabled(self._drag_state.name):
            self._drag_state = None
            self._drag_session = None

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
        for name in all_extensions():
            extension = guarded_extension(name)
            if extension is None:
                continue
            params = self._base_params(name, extension)
            if params is None:
                continue
            params = self._extension_store.params_for(name, params)
            found = call_extension(
                name,
                extension.context_actions,
                series,
                params,
                event,
            )
            if found is not None:
                actions.extend(found)
        return actions

    def apply_action(
        self,
        series: OHLCVSeries,
        action: ChartExtensionAction,
        event: ChartEvent,
    ) -> None:
        name = action.extension_name
        extension = guarded_extension(name)
        if extension is None:
            return
        params = self._base_params(name, extension)
        if params is None:
            return
        params = self._extension_store.params_for(name, params)
        mutation = call_extension(
            name,
            extension.apply_action,
            series,
            params,
            action.action_key,
            event,
        )
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
            if ext_state.name in self._deferred_names:
                continue
            extension = guarded_extension(ext_state.name)
            if extension is None:
                continue
            params = self._params_for_state(ext_state)
            visible_keys = {
                key
                for key in ext_state.series_keys
                if ext_state.series_visibility.get(key, ext_state.visible)
            }
            hit = call_extension(
                ext_state.name,
                extension.hit_test,
                series,
                params,
                event,
                visible_keys,
            )
            if hit is None:
                continue
            if best_hit is None or hit.priority > best_hit.priority:
                best_state = ext_state
                best_hit = hit
        if best_state is None or best_hit is None:
            return False
        extension = guarded_extension(best_state.name)
        if extension is None:
            return False
        session = call_extension(
            best_state.name,
            extension.begin_drag,
            series,
            self._params_for_state(best_state),
            best_hit,
        )
        if session is None or is_disabled(best_state.name):
            return False
        self._drag_state = best_state
        self._drag_session = session
        return True

    def drag_to(
        self,
        series: OHLCVSeries,
        event: ChartEvent,
    ) -> ChartExtensionRenderPass | None:
        if self._drag_state is None or self._drag_session is None:
            return None
        drag_state = self._drag_state
        extension = guarded_extension(drag_state.name)
        if extension is None:
            return None
        render = call_extension(
            drag_state.name,
            extension.drag_to,
            series,
            self._drag_session,
            event,
        )
        if render is None:
            return None
        render_target = call_extension(drag_state.name, extension.render_target)
        if render_target is None:
            return None
        return ChartExtensionRenderPass(
            state=drag_state,
            render=render,
            render_target=render_target,
        )

    def finish_drag(
        self,
        series: OHLCVSeries,
        event: ChartEvent,
    ) -> ChartExtensionMutation | None:
        if self._drag_state is None or self._drag_session is None:
            return None
        name = self._drag_state.name
        session = self._drag_session
        extension = guarded_extension(name)
        mutation = None
        if extension is not None:
            mutation = call_extension(name, extension.finish_drag, series, session, event)
        self._drag_state = None
        self._drag_session = None
        if mutation is not None:
            self.apply_mutation(mutation)
        return mutation

    def cancel_drag(self) -> ChartExtensionMutation | None:
        if self._drag_state is None or self._drag_session is None:
            return None
        name = self._drag_state.name
        session = self._drag_session
        extension = guarded_extension(name)
        mutation = None
        if extension is not None:
            mutation = call_extension(name, extension.cancel_drag, session)
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
        extension = guarded_extension(extension_name)
        if extension is None:
            return _ended_drawing()
        params = self._base_params(extension_name, extension)
        if params is None:
            return _ended_drawing()
        params = self._extension_store.params_for(extension_name, params)
        result = call_extension(
            extension_name,
            extension.start_drawing,
            series,
            params,
            event,
        )
        if result is None:
            return _ended_drawing()
        if result.mutation is not None:
            self.apply_mutation(result.mutation)
        if result.clear_transient:
            self._extension_store.clear_transient_drawings()
        return _unless_disabled(extension_name, result)

    def drawing_hit_test(
        self,
        series: OHLCVSeries,
        event: ChartEvent,
    ) -> HitTestResult | None:
        best_hit = None
        for ext_state in self._state.extensions:
            if ext_state.name in self._deferred_names:
                continue
            extension = guarded_extension(ext_state.name)
            if extension is None:
                continue
            add_mode = call_extension(ext_state.name, extension.add_mode)
            if add_mode != ChartExtensionAddMode.TOOLBAR:
                continue
            params = self._params_for_state(ext_state)
            visible_keys = {
                key
                for key in ext_state.series_keys
                if ext_state.series_visibility.get(key, ext_state.visible)
            }
            hit = call_extension(
                ext_state.name,
                extension.hit_test,
                series,
                params,
                event,
                visible_keys,
            )
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
        name = session.extension_name
        extension = guarded_extension(name)
        if extension is None:
            return None
        render = call_extension(name, extension.preview_drawing, series, session, event)
        if render is None:
            return None
        render_target = call_extension(name, extension.render_target)
        if render_target is None:
            return None
        return ChartExtensionRenderPass(
            state=self._drawing_state(session, render),
            render=render,
            render_target=render_target,
        )

    def advance_drawing(
        self,
        series: OHLCVSeries,
        session: DrawingSession,
        event: ChartEvent,
    ) -> DrawingToolResult:
        name = session.extension_name
        extension = guarded_extension(name)
        if extension is None:
            return _ended_drawing()
        result = call_extension(name, extension.advance_drawing, series, session, event)
        if result is None:
            return _ended_drawing()
        if result.mutation is not None:
            self.apply_mutation(result.mutation)
        return _unless_disabled(name, result)

    def commit_drawing(
        self,
        series: OHLCVSeries,
        session: DrawingSession,
    ) -> DrawingToolResult:
        name = session.extension_name
        extension = guarded_extension(name)
        if extension is None:
            return _ended_drawing()
        result = call_extension(name, extension.commit_drawing, series, session)
        if result is None:
            return _ended_drawing()
        if result.mutation is not None:
            self.apply_mutation(result.mutation)
        return _unless_disabled(name, result)

    def cancel_drawing(
        self,
        session: DrawingSession,
    ) -> ChartExtensionMutation | None:
        name = session.extension_name
        extension = guarded_extension(name)
        if extension is None:
            return None
        mutation = call_extension(name, extension.cancel_drawing, session)
        if mutation is not None:
            self.apply_mutation(mutation)
        return mutation

    def apply_mutation(self, mutation: ChartExtensionMutation) -> None:
        self._extension_store.apply(mutation)

    def toggle_visibility(self, series_key: str) -> tuple[list[str], bool] | None:
        ext_state = self._state.get_extension_by_series_key(series_key)
        if ext_state is None:
            return None
        extension = guarded_extension(ext_state.name)
        if extension is None:
            return None
        params = self._params_for_state(ext_state)
        independent = call_extension(
            ext_state.name,
            extension.toggles_series_independently,
            series_key,
            params,
        )
        if is_disabled(ext_state.name):
            return None
        if independent:
            visible = not ext_state.series_visibility.get(series_key, ext_state.visible)
            ext_state.series_visibility[series_key] = visible
            return [series_key], visible

        ext_state.visible = not ext_state.visible
        return list(ext_state.series_keys), ext_state.visible

    def config_request(self, series_key: str) -> ChartExtensionConfig | None:
        ext_state = self._state.get_extension_by_series_key(series_key)
        if ext_state is None:
            return None
        extension = guarded_extension(ext_state.name)
        if extension is None:
            return None
        params = self._params_for_state(ext_state)
        request = call_extension(
            ext_state.name,
            extension.config_for_series,
            series_key,
            params,
        )
        if is_disabled(ext_state.name):
            return None
        if request is not None:
            return request
        label = call_extension(ext_state.name, extension.label)
        if label is None:
            return None
        return ChartExtensionConfig(
            label=label,
            params=ext_state.params,
        )

    def apply_config(
        self,
        series_key: str,
        params: dict[str, Any],
        y_range: tuple[float, float] | None = None,
    ) -> None:
        ext_state = self._state.get_extension_by_series_key(series_key)
        if ext_state is None:
            return
        extension = guarded_extension(ext_state.name)
        if extension is None:
            return
        mutation = call_extension(
            ext_state.name,
            extension.apply_config_to_series,
            series_key,
            self._params_for_state(ext_state),
            params,
            y_range=y_range,
        )
        if is_disabled(ext_state.name):
            return
        if mutation is not None:
            self.apply_mutation(mutation)
            return
        ext_state.params = params

    def remove(self, series_key: str) -> ChartExtensionRemoval | None:
        ext_state = self._state.get_extension_by_series_key(series_key)
        if ext_state is None:
            return None
        extension = guarded_extension(ext_state.name)
        if extension is None:
            return None
        mutation = call_extension(
            ext_state.name,
            extension.remove_series,
            series_key,
            self._params_for_state(ext_state),
        )
        if is_disabled(ext_state.name):
            return None
        render_target = call_extension(ext_state.name, extension.render_target)
        if render_target is None:
            return None
        if mutation is not None:
            self.apply_mutation(mutation)
            return ChartExtensionRemoval(
                series_keys=[series_key],
                render_target=render_target,
            )

        series_keys = list(ext_state.series_keys)
        self._state.extensions = [
            state for state in self._state.extensions
            if state is not ext_state
        ]
        release_panel = False
        if render_target != "chart":
            release_panel = not any(
                _render_target(state.name) == render_target
                for state in self._state.extensions
            )
        return ChartExtensionRemoval(
            series_keys=series_keys,
            render_target=render_target,
            release_panel=release_panel,
        )

    def _base_params(
        self,
        name: str,
        extension: ChartExtension,
    ) -> dict[str, Any] | None:
        ext_state = self._state.get_extension(name)
        if ext_state is not None:
            return ext_state.params
        return call_extension(name, extension.default_params)

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
            polyline.key for polyline in render.polylines
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
            self._cache_namespace,
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


def guarded_extension(name: str) -> ChartExtension | None:
    # A guarded construction: None when the extension is turned off, failed
    # to construct, or is not registered (its plugin was skipped at load).
    cls = all_extensions().get(name)
    if cls is None:
        return None
    return call_extension(name, cls)


def _render_target(name: str) -> str | None:
    extension = guarded_extension(name)
    if extension is None:
        return None
    return call_extension(name, extension.render_target)


def _ended_drawing() -> DrawingToolResult:
    # What a drawing call returns when its tool is turned off: the session
    # ends and the tool is deactivated.
    return DrawingToolResult(cancel=True, deactivate_tool=True)


def _unless_disabled(name: str, result: DrawingToolResult) -> DrawingToolResult:
    # A store handler of the tool's own plugin can fail while the result's
    # mutation is applied; the session must not continue for a turned-off tool.
    return _ended_drawing() if is_disabled(name) else result


def _lookback_loaded(series: OHLCVSeries, lookback_days: int | None) -> bool:
    """
    Whether the loaded bars cover lookback_days trading days before the first
    bar of the initial view.
    """
    if lookback_days is None:
        return False
    if lookback_days == 0:
        return True
    needed = bars_for_n_days(lookback_days, series.timeframe) + INITIAL_VISIBLE_BARS
    return len(series.bars) >= needed


def _lookback_start_ms(series: OHLCVSeries, lookback_days: int) -> int:
    """
    Estimate the timestamp loaded history must reach for lookback_days to be
    covered. Weekdays approximate sessions, so the estimate may be early by a
    holiday or two; render_all re-checks against the actual bars.
    """
    needed = bars_for_n_days(lookback_days, series.timeframe) + INITIAL_VISIBLE_BARS
    missing_bars = needed - len(series.bars)
    missing_sessions = math.ceil(missing_bars / bars_for_n_days(1, series.timeframe))
    calendar_days = math.ceil(missing_sessions * 7 / 5)
    first_ts_ms = int(series.bars[0].timestamp.timestamp() * 1000)
    return first_ts_ms - calendar_days * _DAY_MS
