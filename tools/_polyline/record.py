from dataclasses import dataclass


@dataclass(frozen=True)
class PolylineRecord:
    """
    A free-form line: an ordered sequence of (timestamp_ms, price) vertices.

    Two vertices is a trend line; up to ten form a poly-line. The same record
    backs both tools — they differ only in how the vertices are drawn, not in
    how they are stored, rendered, hit-tested, dragged, or configured.
    """

    symbol: str
    timeframe: str
    vertices: tuple[tuple[int, float], ...]
    color: str = "#4f7cff"
    line_width: float = 1.0
    line_style: str = "solid"
    persist_across_timeframes: bool = True
    persist_across_sessions: bool = True
    updated_at_ms: int = 0
    age_off_days: float = 365.0
    drawing_id: int | None = None
