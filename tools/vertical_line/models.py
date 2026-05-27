from dataclasses import dataclass


@dataclass(frozen=True)
class VerticalLineRecord:
    symbol: str
    timestamp_ms: int
    timeframe: str
    color: str = "#7a7f8c"
    line_width: float = 1.0
    line_style: str = "solid"
    persist_across_timeframes: bool = True
    persist_across_sessions: bool = True
    line_id: int | None = None
