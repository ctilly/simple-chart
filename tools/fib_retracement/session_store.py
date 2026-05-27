from dataclasses import replace
from typing import Any

from tools.fib_retracement.models import FibRetracementRecord
from simplechart.api import AxisPolicy, DrawingStore

_STORE_KEY = "fib_retracement.drawings"


class FibRetracementStore(DrawingStore[FibRetracementRecord]):

    extension_name = "fib_retracement"
    store_key = _STORE_KEY
    params_key = "drawings"
    timeframe_axis = AxisPolicy.FIXED_OFF
    session_axis = AxisPolicy.FIXED_OFF

    def to_payload(self, record: FibRetracementRecord) -> dict[str, Any]:
        return {
            "timeframe": record.timeframe,
            "end_timestamp_ms": record.end_timestamp_ms,
            "direction": record.direction,
            "anchor_price_mode": record.anchor_price_mode,
            "color": record.color,
            "line_width": record.line_width,
            "line_style": record.line_style,
            "show_price_labels": record.show_price_labels,
            "label_position": record.label_position,
            "show_anchor_handles": record.show_anchor_handles,
            "visible_levels": list(record.visible_levels),
        }

    def from_payload(
        self,
        record_id: int,
        symbol: str,
        sort_key: int,
        payload: dict[str, Any],
    ) -> FibRetracementRecord:
        return FibRetracementRecord(
            symbol=symbol,
            timeframe=str(payload["timeframe"]),
            start_timestamp_ms=sort_key,
            end_timestamp_ms=int(payload["end_timestamp_ms"]),
            direction=str(payload["direction"]),
            anchor_price_mode=str(payload["anchor_price_mode"]),
            color=str(payload["color"]),
            line_width=float(payload["line_width"]),
            line_style=str(payload["line_style"]),
            show_price_labels=bool(payload["show_price_labels"]),
            label_position=str(payload["label_position"]),
            show_anchor_handles=bool(payload["show_anchor_handles"]),
            visible_levels=tuple(payload["visible_levels"]),
            drawing_id=record_id,
        )

    def sort_key(self, record: FibRetracementRecord) -> int:
        return record.start_timestamp_ms

    def record_id(self, record: FibRetracementRecord) -> int | None:
        return record.drawing_id

    def with_id(self, record: FibRetracementRecord, record_id: int) -> FibRetracementRecord:
        return replace(record, drawing_id=record_id)

    def series_key(self, record: FibRetracementRecord) -> str:
        return fib_drawing_key(record)

    def created_timeframe(self, record: FibRetracementRecord) -> str:
        return record.timeframe


def fib_drawing_key(drawing: FibRetracementRecord) -> str:
    if drawing.drawing_id is not None:
        return f"fib_retracement_ref_{drawing.drawing_id}"
    return f"fib_retracement_ref_ts_{drawing.start_timestamp_ms}_{drawing.end_timestamp_ms}"
