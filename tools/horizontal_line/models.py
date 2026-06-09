from dataclasses import dataclass


@dataclass(frozen=True)
class HorizontalLineRecord:
    symbol: str
    price: float
    timeframe: str
    color: str = "#8b5a2b"
    line_width: float = 1.0
    line_style: str = "solid"
    persist_across_timeframes: bool = False
    persist_across_sessions: bool = False
    updated_at_ms: int = 0
    age_off_days: float = 365.0
    line_id: int | None = None
