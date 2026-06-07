from dataclasses import replace
from typing import Any

from tools.horizontal_line.models import HorizontalLineRecord
from simplechart.api import AxisPolicy, DrawingStore

_STORE_KEY = "horizontal_line.lines"
_DEFAULT_AGE_OFF_DAYS = 365.0


class HorizontalLineStore(DrawingStore[HorizontalLineRecord]):

    extension_name = "horizontal_line"
    store_key = _STORE_KEY
    params_key = "lines"
    timeframe_axis = AxisPolicy.USER
    session_axis = AxisPolicy.USER

    def to_payload(self, record: HorizontalLineRecord) -> dict[str, Any]:
        return {
            "timeframe": record.timeframe,
            "color": record.color,
            "line_width": record.line_width,
            "line_style": record.line_style,
            "persist_across_timeframes": record.persist_across_timeframes,
            "persist_across_sessions": record.persist_across_sessions,
            "updated_at_ms": record.updated_at_ms,
            "age_off_days": record.age_off_days,
        }

    def from_payload(
        self,
        record_id: int,
        symbol: str,
        sort_key: int,
        payload: dict[str, Any],
    ) -> HorizontalLineRecord:
        return HorizontalLineRecord(
            symbol=symbol,
            price=sort_key / 1_000_000.0,
            timeframe=str(payload["timeframe"]),
            color=str(payload["color"]),
            line_width=float(payload["line_width"]),
            line_style=str(payload["line_style"]),
            persist_across_timeframes=bool(payload["persist_across_timeframes"]),
            persist_across_sessions=bool(payload["persist_across_sessions"]),
            updated_at_ms=int(payload.get("updated_at_ms", 0)),
            age_off_days=float(payload.get("age_off_days", _DEFAULT_AGE_OFF_DAYS)),
            line_id=record_id,
        )

    def sort_key(self, record: HorizontalLineRecord) -> int:
        return int(record.price * 1_000_000)

    def record_id(self, record: HorizontalLineRecord) -> int | None:
        return record.line_id

    def with_id(self, record: HorizontalLineRecord, record_id: int) -> HorizontalLineRecord:
        return replace(record, line_id=record_id)

    def series_key(self, record: HorizontalLineRecord) -> str:
        return horizontal_line_key(record)

    def created_timeframe(self, record: HorizontalLineRecord) -> str:
        return record.timeframe

    def wants_timeframe_persistence(self, record: HorizontalLineRecord) -> bool:
        return record.persist_across_timeframes

    def wants_session_persistence(self, record: HorizontalLineRecord) -> bool:
        return record.persist_across_sessions

    def updated_at_ms(self, record: HorizontalLineRecord) -> int:
        return record.updated_at_ms

    def age_off_days(self, record: HorizontalLineRecord) -> float:
        return record.age_off_days

    def touch_record(
        self,
        record: HorizontalLineRecord,
        updated_at_ms: int,
    ) -> HorizontalLineRecord:
        return replace(record, updated_at_ms=updated_at_ms)


def horizontal_line_key(line: HorizontalLineRecord) -> str:
    if line.line_id is not None:
        return f"horizontal_line_{line.line_id}"
    return f"horizontal_line_price_{int(line.price * 1_000_000)}"
