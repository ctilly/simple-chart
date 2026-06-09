from abc import abstractmethod
from dataclasses import Field, replace
from typing import Any, ClassVar, Protocol, TypeVar

from simplechart.api import AxisPolicy, DrawingStore


class LineRecordLike(Protocol):
    """
    Shared field surface every single-anchor line record exposes.

    Fields are declared read-only (via property) so the concrete records,
    which are frozen dataclasses, satisfy the protocol. __dataclass_fields__
    marks it dataclass-like so dataclasses.replace() type-checks.
    """

    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]

    @property
    def symbol(self) -> str: ...
    @property
    def timeframe(self) -> str: ...
    @property
    def color(self) -> str: ...
    @property
    def line_width(self) -> float: ...
    @property
    def line_style(self) -> str: ...
    @property
    def persist_across_timeframes(self) -> bool: ...
    @property
    def persist_across_sessions(self) -> bool: ...
    @property
    def updated_at_ms(self) -> int: ...
    @property
    def age_off_days(self) -> float: ...
    @property
    def line_id(self) -> int | None: ...


R = TypeVar("R", bound=LineRecordLike)


class LineShape(Protocol[R]):
    """
    Axis adapter for a single-anchor line record.

    Implemented once per record type (price-anchored or timestamp-anchored)
    and mixed into both the tool and the store, so the framework can build,
    read, and key-encode records without knowing which axis they live on.
    """

    key_prefix: ClassVar[str]
    coord_infix: ClassVar[str]

    def _coord_of(self, record: R) -> float: ...
    def _encode_coord(self, coord: float) -> int: ...
    def _decode_coord(self, encoded: int) -> float: ...
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
    ) -> R: ...


class LineStore(DrawingStore[R]):
    """
    Shared persistence for single-anchor line tools (horizontal, vertical).

    Both persistence axes are USER: each line carries its own cross-timeframe
    and cross-session choice, surfaced as Configure toggles. The anchor value
    is the sort key, encoded to an int via the record's axis adapter. Concrete
    subclasses mix in that adapter and set the class-level identity
    (extension_name, store_key) and a default age-off.
    """

    params_key = "lines"
    timeframe_axis = AxisPolicy.USER
    session_axis = AxisPolicy.USER
    key_prefix: ClassVar[str]
    coord_infix: ClassVar[str]
    default_age_off_days: float = 365.0

    @abstractmethod
    def _coord_of(self, record: R) -> float: ...

    @abstractmethod
    def _encode_coord(self, coord: float) -> int: ...

    @abstractmethod
    def _decode_coord(self, encoded: int) -> float: ...

    @abstractmethod
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
    ) -> R: ...

    def to_payload(self, record: R) -> dict[str, Any]:
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
    ) -> R:
        return self._assemble_record(
            coord=self._decode_coord(sort_key),
            symbol=symbol,
            timeframe=str(payload["timeframe"]),
            color=str(payload["color"]),
            line_width=float(payload["line_width"]),
            line_style=str(payload["line_style"]),
            persist_across_timeframes=bool(payload["persist_across_timeframes"]),
            persist_across_sessions=bool(payload["persist_across_sessions"]),
            updated_at_ms=int(payload.get("updated_at_ms", 0)),
            age_off_days=float(payload.get("age_off_days", self.default_age_off_days)),
            line_id=record_id,
        )

    def sort_key(self, record: R) -> int:
        return self._encode_coord(self._coord_of(record))

    def record_id(self, record: R) -> int | None:
        return record.line_id

    def with_id(self, record: R, record_id: int) -> R:
        return replace(record, line_id=record_id)

    def series_key(self, record: R) -> str:
        return line_series_key(
            self.key_prefix,
            self.coord_infix,
            self._encode_coord(self._coord_of(record)),
            record.line_id,
        )

    def created_timeframe(self, record: R) -> str:
        return record.timeframe

    def wants_timeframe_persistence(self, record: R) -> bool:
        return record.persist_across_timeframes

    def wants_session_persistence(self, record: R) -> bool:
        return record.persist_across_sessions

    def updated_at_ms(self, record: R) -> int:
        return record.updated_at_ms

    def age_off_days(self, record: R) -> float:
        return record.age_off_days

    def touch_record(self, record: R, updated_at_ms: int) -> R:
        return replace(record, updated_at_ms=updated_at_ms)


def line_series_key(prefix: str, infix: str, encoded_coord: int, record_id: int | None) -> str:
    if record_id is not None:
        return f"{prefix}_{record_id}"
    return f"{prefix}_{infix}_{encoded_coord}"
