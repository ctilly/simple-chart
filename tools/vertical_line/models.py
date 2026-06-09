from dataclasses import dataclass


@dataclass(frozen=True)
class VerticalLineRecord:
    symbol: str
    timestamp_ms: int
    timeframe: str
    color: str = "#8b5a2b"
    line_width: float = 1.0
    line_style: str = "solid"
    persist_across_timeframes: bool = False
    persist_across_sessions: bool = False
    updated_at_ms: int = 0
    age_off_days: float = 60.0
    line_id: int | None = None


class VerticalLineShape:
    """Timestamp-anchored adapter shared by the vertical-line tool and store."""

    key_prefix = "vertical_line"
    coord_infix = "ts"

    def _coord_of(self, record: VerticalLineRecord) -> float:
        return float(record.timestamp_ms)

    def _encode_coord(self, coord: float) -> int:
        return int(coord)

    def _decode_coord(self, encoded: int) -> float:
        return float(encoded)

    def _assemble_record(
        self,
        *,
        coord: float,
        symbol: str,
        timeframe: str,
        color: str,
        line_width: float,
        line_style: str,
        persist_across_timeframes: bool,
        persist_across_sessions: bool,
        updated_at_ms: int,
        age_off_days: float,
        line_id: int | None,
    ) -> VerticalLineRecord:
        return VerticalLineRecord(
            symbol=symbol,
            timestamp_ms=int(coord),
            timeframe=timeframe,
            color=color,
            line_width=line_width,
            line_style=line_style,
            persist_across_timeframes=persist_across_timeframes,
            persist_across_sessions=persist_across_sessions,
            updated_at_ms=updated_at_ms,
            age_off_days=age_off_days,
            line_id=line_id,
        )
