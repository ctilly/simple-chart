from dataclasses import replace
from typing import Any

from tools.vertical_line.models import VerticalLineRecord
from simplechart.api import AxisPolicy, DrawingStore

_STORE_KEY = "vertical_line.lines"


class VerticalLineStore(DrawingStore[VerticalLineRecord]):

    extension_name = "vertical_line"
    store_key = _STORE_KEY
    params_key = "lines"
    timeframe_axis = AxisPolicy.USER
    session_axis = AxisPolicy.USER

    def to_payload(self, record: VerticalLineRecord) -> dict[str, Any]:
        return {
            "timeframe": record.timeframe,
            "color": record.color,
            "line_width": record.line_width,
            "line_style": record.line_style,
            "persist_across_timeframes": record.persist_across_timeframes,
            "persist_across_sessions": record.persist_across_sessions,
        }

    def from_payload(
        self,
        record_id: int,
        symbol: str,
        sort_key: int,
        payload: dict[str, Any],
    ) -> VerticalLineRecord:
        return VerticalLineRecord(
            symbol=symbol,
            timestamp_ms=sort_key,
            timeframe=str(payload["timeframe"]),
            color=str(payload["color"]),
            line_width=float(payload["line_width"]),
            line_style=str(payload["line_style"]),
            persist_across_timeframes=bool(payload["persist_across_timeframes"]),
            persist_across_sessions=bool(payload["persist_across_sessions"]),
            line_id=record_id,
        )

    def sort_key(self, record: VerticalLineRecord) -> int:
        return record.timestamp_ms

    def record_id(self, record: VerticalLineRecord) -> int | None:
        return record.line_id

    def with_id(self, record: VerticalLineRecord, record_id: int) -> VerticalLineRecord:
        return replace(record, line_id=record_id)

    def series_key(self, record: VerticalLineRecord) -> str:
        return vertical_line_key(record)

    def created_timeframe(self, record: VerticalLineRecord) -> str:
        return record.timeframe

    def wants_timeframe_persistence(self, record: VerticalLineRecord) -> bool:
        return record.persist_across_timeframes

    def wants_session_persistence(self, record: VerticalLineRecord) -> bool:
        return record.persist_across_sessions


def vertical_line_key(line: VerticalLineRecord) -> str:
    if line.line_id is not None:
        return f"vertical_line_{line.line_id}"
    return f"vertical_line_ts_{line.timestamp_ms}"
