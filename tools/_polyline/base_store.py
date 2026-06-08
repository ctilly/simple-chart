from dataclasses import replace
from typing import Any

from tools._polyline.record import PolylineRecord
from simplechart.api import AxisPolicy, DrawingStore


class PolylineStore(DrawingStore[PolylineRecord]):
    """
    Shared persistence for free-form line tools (trend line, poly-line).

    Both persistence axes are USER: each drawing carries its own choice of
    cross-timeframe and cross-session persistence, surfaced as Configure
    toggles. Concrete subclasses only set the class-level identity
    (extension_name, store_key, key_prefix) and a default age-off.
    """

    params_key = "drawings"
    timeframe_axis = AxisPolicy.USER
    session_axis = AxisPolicy.USER
    key_prefix: str
    default_age_off_days: float = 365.0

    def to_payload(self, record: PolylineRecord) -> dict[str, Any]:
        return {
            "timeframe": record.timeframe,
            "vertices": [[ts, price] for ts, price in record.vertices],
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
    ) -> PolylineRecord:
        return PolylineRecord(
            symbol=symbol,
            timeframe=str(payload["timeframe"]),
            vertices=tuple((int(ts), float(price)) for ts, price in payload["vertices"]),
            color=str(payload["color"]),
            line_width=float(payload["line_width"]),
            line_style=str(payload["line_style"]),
            persist_across_timeframes=bool(payload["persist_across_timeframes"]),
            persist_across_sessions=bool(payload["persist_across_sessions"]),
            updated_at_ms=int(payload.get("updated_at_ms", 0)),
            age_off_days=float(payload.get("age_off_days", self.default_age_off_days)),
            drawing_id=record_id,
        )

    def sort_key(self, record: PolylineRecord) -> int:
        return record.vertices[0][0]

    def record_id(self, record: PolylineRecord) -> int | None:
        return record.drawing_id

    def with_id(self, record: PolylineRecord, record_id: int) -> PolylineRecord:
        return replace(record, drawing_id=record_id)

    def series_key(self, record: PolylineRecord) -> str:
        return polyline_series_key(self.key_prefix, record)

    def created_timeframe(self, record: PolylineRecord) -> str:
        return record.timeframe

    def wants_timeframe_persistence(self, record: PolylineRecord) -> bool:
        return record.persist_across_timeframes

    def wants_session_persistence(self, record: PolylineRecord) -> bool:
        return record.persist_across_sessions

    def updated_at_ms(self, record: PolylineRecord) -> int:
        return record.updated_at_ms

    def age_off_days(self, record: PolylineRecord) -> float:
        return record.age_off_days

    def touch_record(self, record: PolylineRecord, updated_at_ms: int) -> PolylineRecord:
        return replace(record, updated_at_ms=updated_at_ms)


def polyline_series_key(prefix: str, record: PolylineRecord) -> str:
    if record.drawing_id is not None:
        return f"{prefix}_{record.drawing_id}"
    first_ts = record.vertices[0][0]
    last_ts = record.vertices[-1][0]
    return f"{prefix}_ts_{first_ts}_{last_ts}"


def polyline_record_for_key(
    prefix: str,
    drawings: list[PolylineRecord],
    series_key: str,
) -> PolylineRecord | None:
    ts_prefix = f"{prefix}_ts_"
    if series_key.startswith(ts_prefix):
        first_ts, last_ts = (int(value) for value in series_key.removeprefix(ts_prefix).split("_", 1))
        return next(
            (
                drawing for drawing in drawings
                if drawing.vertices[0][0] == first_ts
                and drawing.vertices[-1][0] == last_ts
            ),
            None,
        )
    id_prefix = f"{prefix}_"
    if series_key.startswith(id_prefix):
        drawing_id = int(series_key.removeprefix(id_prefix))
        return next((drawing for drawing in drawings if drawing.drawing_id == drawing_id), None)
    return None
