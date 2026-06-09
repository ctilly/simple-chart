from dataclasses import dataclass


@dataclass(frozen=True)
class FiveDayMarkerSettings:
    symbol: str
    enabled: bool
    color: str
    line_width: float
    line_style: str
    visible: bool
    record_id: int | None = None
