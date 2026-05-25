from dataclasses import dataclass


@dataclass(frozen=True)
class VerticalLineRecord:
    symbol: str
    timestamp_ms: int
    color: str = "#7a7f8c"
    line_width: float = 1.0
    line_style: str = "solid"
    line_id: int | None = None
