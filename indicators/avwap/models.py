from dataclasses import dataclass


@dataclass
class AnchorRecord:
    symbol: str
    anchor_ts: int
    label: str
    color: str
    line_width: float = 2.0
    line_style: str = "solid"
    show_anchor: bool = False
    anchor_id: int | None = None
