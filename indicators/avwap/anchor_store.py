from typing import Any

from indicators.avwap import avwap_anchor_key
from indicators.avwap.models import AnchorRecord
from simplechart.api import (
    IndicatorMutation,
    IndicatorStoreContext,
    IndicatorStoreRecord,
)


_AVWAP_ANCHOR_STORE_KEY = "avwap.anchors"


class AvwapAnchorStore:

    def __init__(self, context: IndicatorStoreContext) -> None:
        self._context = context
        self._anchors: list[AnchorRecord] = []

    def load_for_symbol(self, symbol: str) -> None:
        self._anchors = [
            _record_to_anchor(record)
            for record in self._context.get_indicator_records(
                _AVWAP_ANCHOR_STORE_KEY,
                symbol,
            )
        ]

    def anchors(self) -> list[AnchorRecord]:
        return self._anchors

    def params_for(
        self,
        indicator_name: str,
        base_params: dict[str, Any],
    ) -> dict[str, Any]:
        if indicator_name != "avwap":
            return base_params
        params = dict(base_params)
        params["anchors"] = self._anchors
        return params

    def apply(self, mutation: IndicatorMutation) -> None:
        if mutation.indicator_name != "avwap":
            return
        if mutation.operation == "add_anchor":
            self.add_anchor(mutation.payload)
            self.prepare_active_indicators()
        elif mutation.operation == "update_anchor":
            self.update_anchor(mutation.payload["anchor"])
        elif mutation.operation == "restore_anchors":
            self.restore_anchors(mutation.payload["anchors"])
        elif mutation.operation == "delete_anchor":
            anchor = mutation.payload["anchor"]
            self.delete_anchor(anchor)
            self._cleanup_deleted_anchor(anchor)

    def add_anchor(self, payload: dict[str, Any]) -> None:
        symbol = self._context.current_symbol()
        if symbol is None:
            return
        record = AnchorRecord(
            symbol=symbol,
            anchor_ts=int(payload["anchor_ts"]),
            label=str(payload["label"]),
            color=str(payload["color"]),
        )
        persisted = self._context.put_indicator_record(
            _AVWAP_ANCHOR_STORE_KEY,
            record.symbol,
            record.anchor_ts,
            _anchor_payload(record),
        )
        self._anchors.append(_record_to_anchor(persisted))

    def update_anchor(self, anchor: AnchorRecord) -> None:
        if anchor.anchor_id is None:
            return
        self._context.update_indicator_record(
            anchor.anchor_id,
            anchor.anchor_ts,
            _anchor_payload(anchor),
        )
        self._anchors = [
            anchor if current.anchor_id == anchor.anchor_id else current
            for current in self._anchors
        ]

    def restore_anchors(self, anchors: list[AnchorRecord]) -> None:
        self._anchors = anchors

    def delete_anchor(self, anchor: AnchorRecord) -> None:
        if anchor.anchor_id is None:
            return
        self._context.delete_indicator_record(anchor.anchor_id)
        self._anchors = [
            current for current in self._anchors
            if current.anchor_id != anchor.anchor_id
        ]

    def prepare_active_indicators(self) -> None:
        if self._anchors:
            self._context.ensure_indicator_state("avwap", {"anchors": []})

    def _cleanup_deleted_anchor(self, anchor: AnchorRecord) -> None:
        self._context.remove_series_visibility("avwap", avwap_anchor_key(anchor))
        if not self._anchors:
            self._context.remove_indicator_state("avwap")


def _anchor_payload(anchor: AnchorRecord) -> dict[str, Any]:
    return {
        "label": anchor.label,
        "color": anchor.color,
        "line_width": anchor.line_width,
        "line_style": anchor.line_style,
        "show_anchor": anchor.show_anchor,
    }


def _record_to_anchor(record: IndicatorStoreRecord) -> AnchorRecord:
    payload = record.payload
    return AnchorRecord(
        symbol=record.symbol,
        anchor_ts=record.sort_key,
        label=str(payload["label"]),
        color=str(payload["color"]),
        line_width=float(payload["line_width"]),
        line_style=str(payload["line_style"]),
        show_anchor=bool(payload["show_anchor"]),
        anchor_id=record.record_id,
    )
