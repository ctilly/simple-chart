from tools._polyline import PolylineTool
from tools.trend_line.session_store import TrendLineStore
from simplechart.api import (
    ToolIconLine,
    ToolIconSpec,
    register_extension,
    register_store_handler,
)

_DEFAULT_AGE_OFF_DAYS = 365.0


class TrendLineTool(PolylineTool):

    def name(self) -> str:
        return "trend_line"

    def label(self) -> str:
        return "Trend Line"

    def key_prefix(self) -> str:
        return "trend_line"

    def max_vertices(self) -> int:
        return 2

    def default_age_off_days(self) -> float:
        return _DEFAULT_AGE_OFF_DAYS

    def toolbar_icon(self) -> ToolIconSpec:
        return ToolIconSpec(lines=(ToolIconLine(4, 19, 20, 5),))


register_extension(TrendLineTool)
register_store_handler(TrendLineStore)
