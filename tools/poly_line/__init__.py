from tools._polyline import PolylineTool
from tools.poly_line.session_store import PolyLineStore
from simplechart.api import (
    ToolIconLine,
    ToolIconSpec,
    register_extension,
    register_store_handler,
)

_DEFAULT_AGE_OFF_DAYS = 365.0
_MAX_VERTICES = 15


class PolyLineTool(PolylineTool):

    def name(self) -> str:
        return "poly_line"

    def label(self) -> str:
        return "Poly-Line"

    def key_prefix(self) -> str:
        return "poly_line"

    def max_vertices(self) -> int:
        return _MAX_VERTICES

    def default_age_off_days(self) -> float:
        return _DEFAULT_AGE_OFF_DAYS

    def toolbar_icon(self) -> ToolIconSpec:
        return ToolIconSpec(lines=(
            ToolIconLine(3, 17, 9, 7),
            ToolIconLine(9, 7, 14, 14),
            ToolIconLine(14, 14, 21, 5),
        ))


register_extension(PolyLineTool)
register_store_handler(PolyLineStore)
