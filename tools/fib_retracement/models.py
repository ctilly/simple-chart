from dataclasses import dataclass


FIB_LEVELS: tuple[float, ...] = (0.0, 0.236, 0.382, 0.5, 0.618, 1.0)


@dataclass(frozen=True)
class FibRetracementRecord:
    symbol: str
    timeframe: str
    start_timestamp_ms: int
    end_timestamp_ms: int
    direction: str = "up"
    anchor_price_mode: str = "swing_wick"
    color: str = "#8b5a2b"
    line_width: float = 1.0
    line_style: str = "solid"
    show_price_labels: bool = False
    label_position: str = "right"
    show_anchor_handles: bool = True
    visible_levels: tuple[float, ...] = FIB_LEVELS
    persist_across_sessions: bool = False
    updated_at_ms: int = 0
    age_off_days: float = 2.0
    drawing_id: int | None = None
