from typing import Any

import numpy as np

from simplechart.api import (
    ChartEvent,
    ChartExtension,
    ChartExtensionAddMode,
    ChartExtensionRender,
    DrawingToolResult,
    OHLCVSeries,
    ToolIconLine,
    ToolIconSpec,
    register_extension,
)


class EraseTool(ChartExtension):
    """
    Toolbar action that wipes the chart's transient drawings.

    Selecting Erase and clicking the chart deletes every drawing for the current
    symbol/timeframe that persists on neither axis — i.e. drawings that are both
    timeframe-local and volatile. Anything a tool persists across timeframes or
    across sessions is left untouched. The tool owns no records: it requests the
    generic clear capability and the runtime fans it out across the stores.
    """

    def name(self) -> str:
        return "erase"

    def label(self) -> str:
        return "Erase"

    def default_params(self) -> dict[str, Any]:
        return {}

    def add_mode(self) -> ChartExtensionAddMode:
        return ChartExtensionAddMode.TOOLBAR

    def toolbar_icon(self) -> ToolIconSpec:
        return ToolIconSpec(lines=(
            ToolIconLine(5, 17, 11, 8),
            ToolIconLine(11, 8, 20, 8),
            ToolIconLine(20, 8, 14, 17),
            ToolIconLine(14, 17, 5, 17),
            ToolIconLine(9, 17, 15, 8),
        ))

    def preserve_ui_state_per_symbol(self) -> bool:
        return False

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
        return ChartExtensionRender()

    def start_drawing(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        event: ChartEvent,
    ) -> DrawingToolResult:
        return DrawingToolResult(clear_transient=True, done=True, deactivate_tool=True)


register_extension(EraseTool)
