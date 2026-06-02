"""
simplechart/extensions/_base.py

Abstract base class for all chart extensions.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW THE EXTENSION SYSTEM WORKS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

There are three layers involved in chart extensions:

  1. The ChartExtension subclass (this file / plugin packages)
     - Implements the ChartExtension ABC
     - Compute indicators extract arrays from OHLCVSeries and delegate
       to compiled kernels when needed; tools can render directly
     - Plain Python — not compiled

  2. Compiled kernels (optional, per compute-indicator _kernel.py)
     - Pure numeric functions: numpy array(s) in, numpy array out
     - No I/O, no dicts, no strings, no dynamic dispatch
     - Compiled to native extensions via mypyc for maximum speed
     - Live alongside the compute indicator in its directory (e.g. indicators/ema/)

  3. The plugin registry (simplechart/extensions/_registry.py)
     - Maps extension names to ChartExtension classes
     - All extensions are registered at import time via register()
     - simplechart.plugins imports plugin modules so registration runs

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
THE MYPYC BOUNDARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

mypyc compiles Python source to C extensions. The rule is:

  Code that CAN be compiled lives in _kernel.py files inside compute-indicator
  directories (e.g. indicators/ema/_kernel.py). It must:
    - Use concrete types only (no Any, no Union in hot paths)
    - Accept and return numpy arrays or plain scalars
    - Perform no I/O (no file access, no SQLite, no network)
    - Use no ABCMeta, no dynamic attribute setting

  Code that CANNOT be compiled (this file, _registry.py, ChartExtension
  subclasses) handles the "glue": extracting arrays from OHLCVSeries,
  calling compiled functions, packing results back into dicts.

To compile a kernel, run:
    python scripts/build_compiled.py

During development you can skip compilation entirely — the _kernel.py
source files run as normal Python. The compiled .so extensions are
drop-in replacements; Python imports the .so if present, falls back to
the .py if not.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMPUTE() CONTRACT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

compute() returns a dict[str, np.ndarray]. Each key is a unique series
name; each value is an array aligned to series.bars (same length).
Use np.nan for bars where the compute indicator has no value.

  Example — 50-day SMA:
      {"sma_50": np.array([nan, nan, ..., 14.56, 14.61])}

  Example — two AVWAP anchors (keys include the anchor timestamp so
  each anchor gets its own distinct plot line):
      {
          "avwap_1704067200000": np.array([nan, ..., 14.20, 14.25]),
          "avwap_1706745600000": np.array([nan, ..., nan,   14.31]),
      }

The chart layer uses these keys to identify and toggle individual plot
lines. Stable, unique keys are important — if a key changes between
compute() calls, the chart will create a new line instead of updating
the existing one.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

import numpy as np

from data.models import ChartExtensionStoreRecord, OHLCVSeries


LINE_STYLE_OPTIONS: list[str] = ["solid", "dash", "dot", "dash_dot"]

RENDER_CHART: str = "chart"


class ChartExtensionAddMode(str, Enum):
    DIALOG = "dialog"
    CONTEXT = "context"
    TOOLBAR = "toolbar"
    HIDDEN = "hidden"


@dataclass
class SeriesFill:
    """
    Declares a shaded fill between two named series produced by compute().

    series_a and series_b must be keys returned by the extension's compute()
    method. The fill is drawn between those two lines using the extension's
    color param at the given alpha (0.0 = fully transparent, 1.0 = opaque).
    Values between 0.1 and 0.3 are typical for chart fills.

    Example (Bollinger Bands):
        SeriesFill("bb_upper", "bb_lower", alpha=0.15)
    """

    series_a: str
    series_b: str
    alpha: float = 0.15


@dataclass
class ChoiceParam:
    """
    A parameter that must be one of a fixed set of string options.

    Extensions put this in their default_params() for any field that maps to
    a dropdown in the config dialog. The value and choices travel together so
    the dialog can reconstruct the combo box without knowing anything about
    the specific extension.

    Example:
        "line_style": ChoiceParam("solid", LINE_STYLE_OPTIONS)
    """

    value: str
    options: list[str]


@dataclass
class SeriesRender:
    key: str
    values: np.ndarray
    label: str
    color: str
    line_width: float
    line_style: str = "solid"
    render_target: str = RENDER_CHART
    visible: bool = True
    # Reference lines (e.g. RSI overbought/oversold) are drawn but kept out of
    # the legend. Set explicitly by the extension rather than inferred from key.
    reference: bool = False


@dataclass
class HorizontalSegmentRender:
    key: str
    x_start: float
    x_end: float
    y_value: float
    label: str
    color: str
    line_width: float
    line_style: str = "solid"
    render_target: str = RENDER_CHART
    visible: bool = True
    reference: bool = False


@dataclass
class VerticalLineRender:
    key: str
    x_index: float
    label: str
    color: str
    line_width: float
    line_style: str = "solid"
    render_target: str = RENDER_CHART
    visible: bool = True


@dataclass
class HorizontalLineRender:
    key: str
    y_value: float
    label: str
    color: str
    line_width: float
    line_style: str = "solid"
    render_target: str = RENDER_CHART
    visible: bool = True


@dataclass
class AxisPriceLabelRender:
    key: str
    y_value: float
    text: str
    fill_color: str
    text_color: str
    render_target: str = RENDER_CHART
    visible: bool = True


@dataclass(frozen=True)
class ToolIconLine:
    x1: int
    y1: int
    x2: int
    y2: int


@dataclass(frozen=True)
class ToolIconSpec:
    lines: tuple[ToolIconLine, ...]
    color: str = "#555555"
    line_width: int = 2


@dataclass
class MarkerRender:
    """
    Text marker anchored to a bar/price coordinate on the main price chart.

    Markers intentionally do not accept a render_target. Panel extensions
    should use line/segment render primitives inside their own panel.
    """

    key: str
    x_index: int
    y_value: float
    text: str
    color: str
    font_size: int = 14
    z: int = 20
    visible: bool = True


@dataclass
class ChartExtensionRender:
    series: list[SeriesRender] = field(default_factory=list)
    segments: list[HorizontalSegmentRender] = field(default_factory=list)
    vertical_lines: list[VerticalLineRender] = field(default_factory=list)
    horizontal_lines: list[HorizontalLineRender] = field(default_factory=list)
    axis_price_labels: list[AxisPriceLabelRender] = field(default_factory=list)
    markers: list[MarkerRender] = field(default_factory=list)


@dataclass(frozen=True)
class ChartEvent:
    x: float
    y: float
    bar_index: int | None
    timestamp_ms: int | None
    button: str = "left"
    modifiers: frozenset[str] = field(default_factory=frozenset)
    pixel_size_x: float = 0.0
    pixel_size_y: float = 0.0


@dataclass(frozen=True)
class ChartExtensionAction:
    extension_name: str
    action_key: str
    label: str


@dataclass(frozen=True)
class ChartExtensionMutation:
    extension_name: str
    operation: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ChartExtensionConfig:
    label: str
    params: dict[str, Any]


@dataclass(frozen=True)
class HitTestResult:
    extension_name: str
    handle_key: str
    cursor: str | None = None
    priority: int = 0


@dataclass
class DragSession:
    extension_name: str
    handle_key: str
    original_params: dict[str, Any]
    working_params: dict[str, Any]


@dataclass
class DrawingSession:
    extension_name: str
    tool_key: str
    original_params: dict[str, Any]
    working_params: dict[str, Any]


@dataclass
class DrawingToolResult:
    session: DrawingSession | None = None
    render: ChartExtensionRender | None = None
    mutation: ChartExtensionMutation | None = None
    done: bool = False
    cancel: bool = False
    deactivate_tool: bool = False


class ChartExtensionStoreContext(Protocol):

    def current_symbol(self) -> str | None: ...

    def current_timeframe(self) -> str | None: ...

    def get_extension_records(
        self,
        store_key: str,
        symbol: str,
    ) -> list[ChartExtensionStoreRecord]: ...

    def put_extension_record(
        self,
        store_key: str,
        symbol: str,
        sort_key: int,
        payload: dict[str, Any],
    ) -> ChartExtensionStoreRecord: ...

    def update_extension_record(
        self,
        record_id: int,
        sort_key: int,
        payload: dict[str, Any],
    ) -> None: ...

    def delete_extension_record(self, record_id: int) -> None: ...

    def ensure_extension_state(
        self,
        name: str,
        params: dict[str, Any],
    ) -> None: ...

    def remove_extension_state(self, name: str) -> None: ...

    def remove_series_visibility(
        self,
        extension_name: str,
        series_key: str,
    ) -> None: ...


class ChartExtensionStoreHandler(Protocol):

    def load_for_symbol(self, symbol: str) -> None: ...

    def apply(self, mutation: ChartExtensionMutation) -> None: ...

    def prepare_active_extensions(self) -> None: ...

    def params_for(
        self,
        extension_name: str,
        base_params: dict[str, Any],
    ) -> dict[str, Any]: ...


class ChartExtension(ABC):

    @abstractmethod
    def name(self) -> str:
        """
        Unique machine-readable identifier. Used as a registry key and
        as a prefix for plot series names returned by compute().

        Example: "sma", "avwap", "rvol"
        """

    @abstractmethod
    def label(self) -> str:
        """
        Human-readable display name shown in the chart legend and the
        extension config dialog.

        Example: "Simple Moving Average", "Anchored VWAP"
        """

    @abstractmethod
    def default_params(self) -> dict[str, Any]:
        """
        Return the default parameter set for this extension.

        The config dialog calls this to build its input form. Keys are
        parameter names; values are the defaults. The same dict structure
        is passed back to compute() after the user edits it.

        Example for SMA:
            {"days": 50, "color": "#00BFFF"}

        Example for AVWAP:
            {"anchors": [], "color": "#00FF88"}
        """

    @abstractmethod
    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        """
        Compute plot series from bar data and return named arrays.

        series  — the currently loaded bars for the active symbol/timeframe
        params  — the active parameter set (from default_params(), possibly
                  edited by the user via the config dialog)

        Returns a dict of named arrays, each the same length as series.bars,
        aligned to the bar timestamps. Use np.nan for bars where the
        compute indicator has no value (e.g. before an MA has accumulated enough
        bars to produce its first valid output).

        Delegate heavy numeric work to a _kernel.py module when the
        computation involves a tight loop over thousands of bars.
        """

    def render(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> ChartExtensionRender:
        result = self.compute(series, params)
        return render_from_legacy(self, params, result)

    def context_actions(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        event: ChartEvent,
    ) -> list[ChartExtensionAction]:
        return []

    def apply_action(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        action_key: str,
        event: ChartEvent,
    ) -> ChartExtensionMutation | None:
        return None

    def hit_test(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        event: ChartEvent,
        visible_keys: set[str],
    ) -> HitTestResult | None:
        return None

    def begin_drag(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        hit: HitTestResult,
    ) -> DragSession:
        return DragSession(
            extension_name=hit.extension_name,
            handle_key=hit.handle_key,
            original_params=dict(params),
            working_params=dict(params),
        )

    def drag_to(
        self,
        series: OHLCVSeries,
        session: DragSession,
        event: ChartEvent,
    ) -> ChartExtensionRender | None:
        return None

    def finish_drag(
        self,
        series: OHLCVSeries,
        session: DragSession,
        event: ChartEvent,
    ) -> ChartExtensionMutation | None:
        return None

    def cancel_drag(
        self,
        session: DragSession,
    ) -> ChartExtensionMutation | None:
        return None

    def start_drawing(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        event: ChartEvent,
    ) -> DrawingToolResult:
        return DrawingToolResult()

    def preview_drawing(
        self,
        series: OHLCVSeries,
        session: DrawingSession,
        event: ChartEvent,
    ) -> ChartExtensionRender | None:
        return None

    def advance_drawing(
        self,
        series: OHLCVSeries,
        session: DrawingSession,
        event: ChartEvent,
    ) -> DrawingToolResult:
        return DrawingToolResult(session=session)

    def cancel_drawing(
        self,
        session: DrawingSession,
    ) -> ChartExtensionMutation | None:
        return None

    def add_mode(self) -> ChartExtensionAddMode:
        return ChartExtensionAddMode.DIALOG

    def toolbar_icon(self) -> "ToolIconSpec | None":
        return None

    def preserve_ui_state_per_symbol(self) -> bool:
        return True

    def toggles_series_independently(
        self,
        series_key: str,
        params: dict[str, Any],
    ) -> bool:
        return False

    def config_for_series(
        self,
        series_key: str,
        params: dict[str, Any],
    ) -> ChartExtensionConfig | None:
        return None

    def apply_config_to_series(
        self,
        series_key: str,
        params: dict[str, Any],
        edited_params: dict[str, Any],
    ) -> ChartExtensionMutation | None:
        return None

    def remove_series(
        self,
        series_key: str,
        params: dict[str, Any],
    ) -> ChartExtensionMutation | None:
        return None

    def render_target(self) -> str:
        """
        Return the render target for this extension.

        Chart extensions return RENDER_CHART (the default — no override needed).
        They draw directly on the price chart, sharing its time × price axes.

        Panel extensions return a short lowercase string naming their panel
        (e.g. "rsi", "macd"). Each unique string gets its own dedicated panel
        below the chart, sharing only the x (time) axis. Two instances
        returning the same string share one panel.
        """
        return RENDER_CHART

    def series_fills(self) -> list[SeriesFill]:
        """
        Declare shaded fills between pairs of series produced by compute().

        Override this to have the chart draw a translucent fill between two
        named series (e.g. Bollinger Bands upper and lower). Returns an empty
        list by default — chart extensions need not override it.

        Note: fill rendering support in PlotManager is planned but not yet
        implemented. Declaring fills here is forward-compatible; they will
        be drawn automatically once support is added.
        """
        return []


def render_from_legacy(
    extension: ChartExtension,
    params: dict[str, Any],
    result: dict[str, np.ndarray],
) -> ChartExtensionRender:
    color: str = str(params.get("color", "#ffffff"))
    line_width: float = float(params.get("line_width", 1.0))
    line_style: str = _line_style_value(
        params.get("line_style", ChoiceParam("solid", LINE_STYLE_OPTIONS))
    )
    render_target: str = extension.render_target()
    return ChartExtensionRender(
        series=[
            SeriesRender(
                key=key,
                values=values,
                label=_legacy_series_label(key),
                color="#AAAAAA" if _is_legacy_reference(key) else color,
                line_width=0.8 if _is_legacy_reference(key) else line_width,
                line_style="dash" if _is_legacy_reference(key) else line_style,
                render_target=render_target,
                reference=_is_legacy_reference(key),
            )
            for key, values in result.items()
        ]
    )


def _is_legacy_reference(series_key: str) -> bool:
    # Legacy dict-keyed indicators (SMA/EMA/RSI) signal a reference line via the
    # "_ref_" key convention. This is the one place that convention is read;
    # downstream layers use the explicit SeriesRender.reference flag instead.
    return "_ref_" in series_key


def _line_style_value(value: Any) -> str:
    if isinstance(value, ChoiceParam):
        return value.value
    return str(value)


def _legacy_series_label(series_key: str) -> str:
    if series_key.startswith("sma_"):
        return f"SMA {series_key[4:]}"
    if series_key.startswith("ema_"):
        return f"EMA {series_key[4:]}"
    return series_key
