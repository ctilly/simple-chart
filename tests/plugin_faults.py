"""Fault-injection plugins for the plugin-isolation tests.

A fault plugin is a real plugin file loaded through the real loader. It
registers one extension that implements every host-called hook, and
optionally a DrawingStore-based store handler implementing every store-handler
call. Each hook records its name in CALLS / STORE_CALLS; once the test sets
ARMED, the hook named FAIL_IN / STORE_FAIL_IN raises a RuntimeError whose
message carries the synthetic secret.

register_via_helper and store_handler are a shared registration helper and a
shared decorator: two plugins calling them from their module bodies must
each own their own registrations.
"""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from textwrap import dedent
from types import ModuleType
from typing import Any

from realistic_errors import SECRET
from simplechart.api import ChartExtension, register_extension, register_store_handler
from simplechart.extensions import _registry, _store_registry
from simplechart.extensions._store_registry import StoreHandlerFactory


USER_PLUGIN_NAMESPACE = "simplechart_user_plugins"
# Temporary plugin packages are named with this prefix.
TEMP_PACKAGE_PREFIX = "iso_pkg_"

# Plugin modules append their name here each time their body executes.
EXECUTIONS: list[str] = []

_TEMPLATE = '''
from typing import Any

import numpy as np

from simplechart.api import (
    AxisPolicy,
    ChartEvent,
    ChartExtension,
    ChartExtensionAction,
    ChartExtensionAddMode,
    ChartExtensionConfig,
    ChartExtensionMutation,
    ChartExtensionRender,
    ChartExtensionStoreContext,
    DragSession,
    DrawingSession,
    DrawingStore,
    DrawingToolResult,
    HitTestResult,
    MarkerRender,
    OHLCVSeries,
    SeriesRender,
    ToolIconSpec,
    register_extension,
    register_store_handler,
)

NAME = {name!r}
LABEL = {label!r}
KEY = NAME + "_line"
FAIL_IN = {fail_in!r}
STORE_FAIL_IN = {store_fail_in!r}
ADD_MODE = ChartExtensionAddMode({add_mode!r})
TARGET = {render_target!r}
HITS = {hits!r}
CLEAR_ON_START = {clear_on_start!r}
SECRET = {secret!r}
ARMED = False
CALLS: list[str] = []
STORE_CALLS: list[str] = []


def _hook(hook: str) -> None:
    CALLS.append(hook)
    if ARMED and FAIL_IN == hook:
        raise RuntimeError(f"{{hook}} exploded: api_key={{SECRET}}")


def _store_hook(hook: str) -> None:
    STORE_CALLS.append(hook)
    if ARMED and STORE_FAIL_IN == hook:
        raise RuntimeError(f"store {{hook}} exploded: api_key={{SECRET}}")


def _render(series: OHLCVSeries, suffix: str = "") -> ChartExtensionRender:
    markers = []
    if TARGET == "chart":
        markers = [
            MarkerRender(
                key=KEY + suffix + "_marker",
                x_index=1.0,
                y_value=100.0,
                text="F",
                color="#ff0000",
            )
        ]
    return ChartExtensionRender(
        series=[
            SeriesRender(
                key=KEY + suffix,
                values=np.full(len(series.bars), 100.0),
                label=LABEL,
                color="#ff0000",
                line_width=1.0,
                render_target=TARGET,
            )
        ],
        markers=markers,
    )


def _noop(name: str) -> ChartExtensionMutation:
    return ChartExtensionMutation(name, "noop", {{}})


class FaultExtension(ChartExtension):

    def __init__(self) -> None:
        _hook("__init__")

    def name(self) -> str:
        return NAME

    def label(self) -> str:
        _hook("label")
        return LABEL

    def default_params(self) -> dict[str, Any]:
        _hook("default_params")
        return {{"color": "#ff0000"}}

    def compute(self, series: OHLCVSeries, params: dict[str, Any]) -> dict[str, Any]:
        _hook("compute")
        return {{}}

    def render(self, series: OHLCVSeries, params: dict[str, Any]) -> ChartExtensionRender:
        _hook("render")
        return _render(series)

    def render_target(self) -> str:
        _hook("render_target")
        return TARGET

    def add_mode(self) -> ChartExtensionAddMode:
        _hook("add_mode")
        return ADD_MODE

    def toolbar_icon(self) -> ToolIconSpec | None:
        _hook("toolbar_icon")
        return None

    def history_lookback_days(self, params: dict[str, Any]) -> int | None:
        _hook("history_lookback_days")
        return 0

    def preserve_ui_state_per_symbol(self) -> bool:
        _hook("preserve_ui_state_per_symbol")
        return True

    def context_actions(
        self, series: OHLCVSeries, params: dict[str, Any], event: ChartEvent,
    ) -> list[ChartExtensionAction]:
        _hook("context_actions")
        return [ChartExtensionAction(NAME, "act", LABEL + " action")]

    def apply_action(
        self, series: OHLCVSeries, params: dict[str, Any], action_key: str,
        event: ChartEvent,
    ) -> ChartExtensionMutation | None:
        _hook("apply_action")
        return _noop(NAME)

    def hit_test(
        self, series: OHLCVSeries, params: dict[str, Any], event: ChartEvent,
        visible_keys: set[str],
    ) -> HitTestResult | None:
        _hook("hit_test")
        if HITS and KEY in visible_keys:
            return HitTestResult(NAME, KEY, priority=10)
        return None

    def begin_drag(
        self, series: OHLCVSeries, params: dict[str, Any], hit: HitTestResult,
    ) -> DragSession:
        _hook("begin_drag")
        return super().begin_drag(series, params, hit)

    def drag_to(
        self, series: OHLCVSeries, session: DragSession, event: ChartEvent,
    ) -> ChartExtensionRender | None:
        _hook("drag_to")
        return _render(series)

    def finish_drag(
        self, series: OHLCVSeries, session: DragSession, event: ChartEvent,
    ) -> ChartExtensionMutation | None:
        _hook("finish_drag")
        return _noop(NAME)

    def cancel_drag(self, session: DragSession) -> ChartExtensionMutation | None:
        _hook("cancel_drag")
        return None

    def start_drawing(
        self, series: OHLCVSeries, params: dict[str, Any], event: ChartEvent,
    ) -> DrawingToolResult:
        _hook("start_drawing")
        if CLEAR_ON_START:
            return DrawingToolResult(done=True, deactivate_tool=True, clear_transient=True)
        return DrawingToolResult(
            session=DrawingSession(NAME, "draw", dict(params), dict(params)),
            render=_render(series, "_preview"),
        )

    def preview_drawing(
        self, series: OHLCVSeries, session: DrawingSession, event: ChartEvent,
    ) -> ChartExtensionRender | None:
        _hook("preview_drawing")
        return _render(series, "_preview")

    def advance_drawing(
        self, series: OHLCVSeries, session: DrawingSession, event: ChartEvent,
    ) -> DrawingToolResult:
        _hook("advance_drawing")
        return DrawingToolResult(session=session)

    def commit_drawing(
        self, series: OHLCVSeries, session: DrawingSession,
    ) -> DrawingToolResult:
        _hook("commit_drawing")
        return DrawingToolResult(done=True, deactivate_tool=True)

    def cancel_drawing(self, session: DrawingSession) -> ChartExtensionMutation | None:
        _hook("cancel_drawing")
        return None

    def toggles_series_independently(self, series_key: str, params: dict[str, Any]) -> bool:
        _hook("toggles_series_independently")
        return False

    def config_for_series(
        self, series_key: str, params: dict[str, Any],
    ) -> ChartExtensionConfig | None:
        _hook("config_for_series")
        return None

    def apply_config_to_series(
        self, series_key: str, params: dict[str, Any], edited_params: dict[str, Any],
        y_range: tuple[float, float] | None = None,
    ) -> ChartExtensionMutation | None:
        _hook("apply_config_to_series")
        return None

    def remove_series(
        self, series_key: str, params: dict[str, Any],
    ) -> ChartExtensionMutation | None:
        _hook("remove_series")
        return None


class FaultStore(DrawingStore[dict[str, Any]]):
    # Its own record name: the base would otherwise drop the extension's chart
    # state whenever no drawings exist.
    extension_name = NAME + "_drawings"
    store_key = "iso_" + NAME
    params_key = "_iso_records"
    timeframe_axis = AxisPolicy.FIXED_OFF
    session_axis = AxisPolicy.FIXED_OFF

    def __init__(self, context: ChartExtensionStoreContext) -> None:
        _store_hook("factory")
        super().__init__(context)

    def to_payload(self, record: dict[str, Any]) -> dict[str, Any]:
        return dict(record)

    def from_payload(
        self, record_id: int, symbol: str, sort_key: int, payload: dict[str, Any],
    ) -> dict[str, Any]:
        return dict(payload, id=record_id)

    def sort_key(self, record: dict[str, Any]) -> int:
        return 0

    def record_id(self, record: dict[str, Any]) -> int | None:
        return record.get("id")

    def with_id(self, record: dict[str, Any], record_id: int) -> dict[str, Any]:
        return dict(record, id=record_id)

    def series_key(self, record: dict[str, Any]) -> str:
        return KEY

    def created_timeframe(self, record: dict[str, Any]) -> str:
        return "5m"

    def load_for_symbol(self, symbol: str) -> None:
        _store_hook("load_for_symbol")
        super().load_for_symbol(symbol)

    def apply(self, mutation: ChartExtensionMutation) -> None:
        _store_hook("apply")
        super().apply(mutation)

    def prepare_active_extensions(self) -> None:
        _store_hook("prepare_active_extensions")
        super().prepare_active_extensions()

    def params_for(self, extension_name: str, base_params: dict[str, Any]) -> dict[str, Any]:
        _store_hook("params_for")
        return super().params_for(extension_name, base_params)

    def clear_transient(self) -> None:
        _store_hook("clear_transient")
        super().clear_transient()


register_extension(FaultExtension)
if {with_store!r}:
    register_store_handler(FaultStore)
'''


def fault_plugin_source(
    *,
    name: str,
    label: str,
    fail_in: str = "",
    store_fail_in: str = "",
    add_mode: str = "dialog",
    render_target: str = "chart",
    hits: bool = False,
    clear_on_start: bool = False,
    with_store: bool = True,
) -> str:
    return _TEMPLATE.format(
        name=name,
        label=label,
        fail_in=fail_in,
        store_fail_in=store_fail_in,
        add_mode=add_mode,
        render_target=render_target,
        hits=hits,
        clear_on_start=clear_on_start,
        with_store=with_store,
        secret=SECRET,
    )


def write_plugin(directory: Path, stem: str, source: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.py"
    path.write_text(dedent(source))
    return path


def plugin_module(stem: str) -> Any:
    return sys.modules[f"{USER_PLUGIN_NAMESPACE}.{stem}"]


def arm(*modules: ModuleType) -> None:
    for module in modules:
        setattr(module, "ARMED", True)


@contextmanager
def isolated_plugin_state() -> Iterator[None]:
    # Restores both registries and drops every module a test's plugins added,
    # so no test's plugins reach another test.
    extensions = _registry.snapshot()
    stores = _store_registry.snapshot()
    modules = set(sys.modules)
    try:
        yield
    finally:
        _registry.restore(extensions)
        _store_registry.restore(stores)
        for name in set(sys.modules) - modules:
            if name.startswith((f"{USER_PLUGIN_NAMESPACE}.", TEMP_PACKAGE_PREFIX)):
                del sys.modules[name]


# ------------------------------------------------------------------
# Shared registration helper and decorator
# ------------------------------------------------------------------


def register_via_helper(cls: type[ChartExtension]) -> None:
    register_extension(cls)


def store_handler(factory: StoreHandlerFactory) -> StoreHandlerFactory:
    register_store_handler(factory)
    return factory
