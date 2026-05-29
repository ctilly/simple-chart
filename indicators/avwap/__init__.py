"""
indicators/avwap/__init__.py

Anchored VWAP indicator.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW AVWAP ANCHORS WORK IN THIS SYSTEM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1. User clicks a bar on the chart.
  2. The chart layer emits bar_clicked(utc_ts_ms: int).
  3. This indicator returns an add-anchor mutation.
  4. The runtime persists the AnchorRecord and injects anchors into params.
  5. The runtime calls compute() which resolves each anchor timestamp to
     a bar index and delegates to the avwap_multi() compiled kernel.
  6. compute() returns one named array per anchor.
  7. The chart layer draws each generic render series.

The anchor timestamp is stored in UTC milliseconds (integer). It is calendar
time, not a bar index. This means:
  - Switching timeframes: the same anchor_ts resolves to a different bar
    index in the new timeframe, but the AVWAP still starts from the same
    point in time. Correct behavior.
  - Restarting the app: anchors are loaded from SQLite, the same timestamps
    resolve to the same bars. Persistent behavior.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MULTIPLE ANCHORS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

There is one AVWAPIndicator instance on the chart. It holds all anchors
for the current symbol in its params["anchors"] list. Each anchor produces
one plot series, keyed by stable anchor identity. Persisted anchors use
"avwap_anchor_{anchor_id}" so the key does not change when an anchor is
dragged to a new timestamp. Adding a new anchor appends to the list and
triggers a recompute; removing one removes it from the list.

The avwap_multi() kernel processes all anchors in a single pass over the
bar arrays, which is more efficient than computing each anchor separately.
"""

from datetime import datetime, timezone
from typing import Any

import numpy as np

from simplechart.api import (
    ChartEvent,
    ChoiceParam,
    DragSession,
    HitTestResult,
    ChartExtension,
    ChartExtensionAction,
    ChartExtensionAddMode,
    ChartExtensionConfig,
    ChartExtensionMutation,
    ChartExtensionRender,
    LINE_STYLE_OPTIONS,
    MarkerRender,
    OHLCVSeries,
    SeriesRender,
    register_extension,
    register_store_handler,
    timestamp_ms_to_bar_index,
)
from indicators.avwap._kernel import avwap_multi as _avwap_multi_kernel
from indicators.avwap.models import AnchorRecord

_AVWAP_PALETTE: list[str] = [
    "#800080",
    "#FFB000",
    "#00BFFF",
    "#FF5C8A",
    "#6A5ACD",
    "#00FF88",
    "#B388FF",
]
_ANCHOR_HIT_BUFFER_FRACTION = 0.35
_ANCHOR_HIT_MIN_PRICE_FRACTION = 0.0004


class AVWAPIndicator(ChartExtension):

    def name(self) -> str:
        return "avwap"

    def label(self) -> str:
        return "Anchored VWAP"

    def default_params(self) -> dict[str, Any]:
        # anchors is a list of AnchorRecord. Starts empty — anchors are
        # added interactively by clicking bars on the chart.
        return {"anchors": []}

    def add_mode(self) -> ChartExtensionAddMode:
        return ChartExtensionAddMode.CONTEXT

    def preserve_ui_state_per_symbol(self) -> bool:
        return False

    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        """
        Compute one AVWAP series per anchor in params["anchors"].

        If there are no anchors, returns an empty dict — nothing to draw.

        Steps:
          1. Extract price/volume arrays from the series
          2. Build a list of bar timestamps (ms) for anchor resolution
          3. Resolve each anchor's UTC timestamp to a bar index
          4. Call avwap_multi() with all anchor indexes at once
          5. Return one named array per anchor
        """
        anchors: list[AnchorRecord] = params.get("anchors", [])
        if not anchors:
            return {}

        bars = series.bars
        highs   = np.array([b.high   for b in bars], dtype=float)
        lows    = np.array([b.low    for b in bars], dtype=float)
        closes  = np.array([b.close  for b in bars], dtype=float)
        volumes = np.array([b.volume for b in bars], dtype=float)

        # Build the list of bar timestamps once, reused for all anchors.
        bar_ts_ms: list[int] = [
            int(b.timestamp.timestamp() * 1000) for b in bars
        ]

        anchor_indexes: list[int] = [
            timestamp_ms_to_bar_index(anchor.anchor_ts, bar_ts_ms)
            for anchor in anchors
        ]

        arrays: list[np.ndarray] = _avwap_multi_kernel(
            highs, lows, closes, volumes, anchor_indexes
        )

        # Persisted anchors use their DB id so visibility survives drag
        # timestamp edits.
        return {
            avwap_anchor_key(anchor): array
            for anchor, array in zip(anchors, arrays)
        }

    def render(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> ChartExtensionRender:
        anchors: list[AnchorRecord] = params.get("anchors", [])
        result = self.compute(series, params)
        render = ChartExtensionRender()
        anchors_by_key: dict[str, AnchorRecord] = {
            avwap_anchor_key(anchor): anchor
            for anchor in anchors
        }

        for key, values in result.items():
            anchor = anchors_by_key[key]
            render.series.append(
                SeriesRender(
                    key=key,
                    values=values,
                    label=f"AVWAP {anchor.label}",
                    color=anchor.color,
                    line_width=anchor.line_width,
                    line_style=anchor.line_style,
                )
            )
            if anchor.show_anchor:
                valid_indexes = np.flatnonzero(~np.isnan(values))
                if len(valid_indexes) > 0:
                    marker_idx = int(valid_indexes[0])
                    render.markers.append(
                        MarkerRender(
                            key=key,
                            x_index=marker_idx,
                            y_value=float(values[marker_idx]),
                            text="⚓️",
                            color=anchor.color,
                        )
                    )

        return render

    def context_actions(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        event: ChartEvent,
    ) -> list[ChartExtensionAction]:
        if event.timestamp_ms is None:
            return []
        if event.bar_index is not None and _anchor_target_is_duplicate(
            series,
            params.get("anchors", []),
            event.bar_index,
            None,
        ):
            return []
        return [
            ChartExtensionAction(
                extension_name=self.name(),
                action_key="add_anchor",
                label="Add AVWAP here",
            )
        ]

    def apply_action(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        action_key: str,
        event: ChartEvent,
    ) -> ChartExtensionMutation | None:
        if action_key != "add_anchor" or event.timestamp_ms is None:
            return None
        anchors: list[AnchorRecord] = params.get("anchors", [])
        label = datetime.fromtimestamp(
            event.timestamp_ms / 1000,
            tz=timezone.utc,
        ).strftime("%Y-%m-%d")
        color = _AVWAP_PALETTE[len(anchors) % len(_AVWAP_PALETTE)]
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="add_anchor",
            payload={
                "anchor_ts": event.timestamp_ms,
                "label": label,
                "color": color,
            },
        )

    def hit_test(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        event: ChartEvent,
        visible_keys: set[str],
    ) -> HitTestResult | None:
        if event.bar_index is None:
            return None
        if event.bar_index < 0 or event.bar_index >= len(series.bars):
            return None
        anchors: list[AnchorRecord] = params.get("anchors", [])
        for anchor in anchors:
            key = avwap_anchor_key(anchor)
            if key not in visible_keys:
                continue
            if (
                _anchor_resolves_to_bar(series, anchor, event.bar_index)
                and _anchor_y_hit_test(series, event.bar_index, event.y)
            ):
                return HitTestResult(
                    extension_name=self.name(),
                    handle_key=key,
                    cursor="drag",
                    priority=10,
                )
        return None

    def begin_drag(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
        hit: HitTestResult,
    ) -> DragSession:
        anchors: list[AnchorRecord] = params.get("anchors", [])
        original = [_copy_anchor(anchor) for anchor in anchors]
        return DragSession(
            extension_name=self.name(),
            handle_key=hit.handle_key,
            original_params={"anchors": original},
            working_params={"anchors": [_copy_anchor(anchor) for anchor in anchors]},
        )

    def drag_to(
        self,
        series: OHLCVSeries,
        session: DragSession,
        event: ChartEvent,
    ) -> ChartExtensionRender | None:
        if event.bar_index is None:
            return None
        updated = _updated_drag_anchor(series, session, event.bar_index)
        if updated is None:
            return None
        session.working_params["anchors"] = [
            updated if avwap_anchor_key(anchor) == session.handle_key else anchor
            for anchor in session.working_params["anchors"]
        ]
        return self.render(series, {"anchors": [updated]})

    def finish_drag(
        self,
        series: OHLCVSeries,
        session: DragSession,
        event: ChartEvent,
    ) -> ChartExtensionMutation | None:
        if event.bar_index is not None:
            self.drag_to(series, session, event)
        anchor = avwap_anchor_for_key(
            session.working_params["anchors"],
            session.handle_key,
        )
        if anchor is None:
            return None
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="update_anchor",
            payload={"anchor": anchor},
        )

    def cancel_drag(
        self,
        session: DragSession,
    ) -> ChartExtensionMutation | None:
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="restore_anchors",
            payload={"anchors": session.original_params["anchors"]},
        )

    def toggles_series_independently(
        self,
        series_key: str,
        params: dict[str, Any],
    ) -> bool:
        return avwap_anchor_for_key(params.get("anchors", []), series_key) is not None

    def config_for_series(
        self,
        series_key: str,
        params: dict[str, Any],
    ) -> ChartExtensionConfig | None:
        anchor = avwap_anchor_for_key(params.get("anchors", []), series_key)
        if anchor is None:
            return None
        return ChartExtensionConfig(
            label=f"AVWAP {anchor.label}",
            params={
                "color": anchor.color,
                "line_width": anchor.line_width,
                "line_style": ChoiceParam(anchor.line_style, LINE_STYLE_OPTIONS),
                "show_anchor": anchor.show_anchor,
            },
        )

    def apply_config_to_series(
        self,
        series_key: str,
        params: dict[str, Any],
        edited_params: dict[str, Any],
    ) -> ChartExtensionMutation | None:
        anchor = avwap_anchor_for_key(params.get("anchors", []), series_key)
        if anchor is None:
            return None
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="update_anchor",
            payload={
                "anchor": AnchorRecord(
                    symbol=anchor.symbol,
                    anchor_ts=anchor.anchor_ts,
                    label=anchor.label,
                    color=edited_params["color"],
                    line_width=edited_params["line_width"],
                    line_style=edited_params["line_style"].value,
                    show_anchor=edited_params["show_anchor"],
                    anchor_id=anchor.anchor_id,
                )
            },
        )

    def remove_series(
        self,
        series_key: str,
        params: dict[str, Any],
    ) -> ChartExtensionMutation | None:
        anchor = avwap_anchor_for_key(params.get("anchors", []), series_key)
        if anchor is None or anchor.anchor_id is None:
            return None
        return ChartExtensionMutation(
            extension_name=self.name(),
            operation="delete_anchor",
            payload={"anchor": anchor},
        )


register_extension(AVWAPIndicator)


def avwap_anchor_key(anchor: AnchorRecord) -> str:
    if anchor.anchor_id is not None:
        return f"avwap_anchor_{anchor.anchor_id}"
    return f"avwap_anchor_ts_{anchor.anchor_ts}"


def avwap_anchor_for_key(
    anchors: list[AnchorRecord],
    series_key: str,
) -> AnchorRecord | None:
    if series_key.startswith("avwap_anchor_ts_"):
        anchor_ts = int(series_key.removeprefix("avwap_anchor_ts_"))
        return next((a for a in anchors if a.anchor_ts == anchor_ts), None)
    if series_key.startswith("avwap_anchor_"):
        anchor_id = int(series_key.removeprefix("avwap_anchor_"))
        return next((a for a in anchors if a.anchor_id == anchor_id), None)
    return None


def _anchor_resolves_to_bar(
    series: OHLCVSeries,
    anchor: AnchorRecord,
    bar_index: int,
) -> bool:
    if bar_index < 0 or bar_index >= len(series.bars):
        return False
    bar_ts = int(series.bars[bar_index].timestamp.timestamp() * 1000)
    if bar_ts < anchor.anchor_ts:
        return False
    if bar_index == 0:
        return True
    prev_ts = int(series.bars[bar_index - 1].timestamp.timestamp() * 1000)
    return bool(prev_ts < anchor.anchor_ts)


def _anchor_target_is_duplicate(
    series: OHLCVSeries,
    anchors: list[AnchorRecord],
    target_idx: int,
    moving_key: str | None,
) -> bool:
    for anchor in anchors:
        if moving_key is not None and avwap_anchor_key(anchor) == moving_key:
            continue
        if _anchor_resolves_to_bar(series, anchor, target_idx):
            return True
    return False


def _anchor_y_hit_test(series: OHLCVSeries, bar_index: int, y_value: float) -> bool:
    bar = series.bars[bar_index]
    buffer = _anchor_hit_buffer(series, bar_index)
    candle_low = min(bar.low, bar.high)
    candle_high = max(bar.low, bar.high)
    if candle_low - buffer <= y_value <= candle_high + buffer:
        return True

    marker_y = (bar.high + bar.low + bar.close) / 3.0
    return bool(abs(y_value - marker_y) <= buffer)


def _anchor_hit_buffer(series: OHLCVSeries, bar_index: int) -> float:
    start = max(0, bar_index - 10)
    end = min(len(series.bars), bar_index + 11)
    ranges = [
        abs(bar.high - bar.low)
        for bar in series.bars[start:end]
        if abs(bar.high - bar.low) > 0.0
    ]
    local_range = float(np.median(np.array(ranges, dtype=float))) if ranges else 0.0
    price = max(abs(series.bars[bar_index].close), 1.0)
    return float(max(
        local_range * _ANCHOR_HIT_BUFFER_FRACTION,
        price * _ANCHOR_HIT_MIN_PRICE_FRACTION,
    ))


def _updated_drag_anchor(
    series: OHLCVSeries,
    session: DragSession,
    target_idx: int,
) -> AnchorRecord | None:
    anchors: list[AnchorRecord] = session.working_params["anchors"]
    anchor = avwap_anchor_for_key(anchors, session.handle_key)
    if anchor is None:
        return None
    if target_idx < 0 or target_idx >= len(series.bars):
        return None
    if _anchor_target_is_duplicate(series, anchors, target_idx, session.handle_key):
        return None
    target_ts = int(series.bars[target_idx].timestamp.timestamp() * 1000)
    if target_ts == anchor.anchor_ts:
        return None
    return AnchorRecord(
        symbol=anchor.symbol,
        anchor_ts=target_ts,
        label=datetime.fromtimestamp(target_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        color=anchor.color,
        line_width=anchor.line_width,
        line_style=anchor.line_style,
        show_anchor=anchor.show_anchor,
        anchor_id=anchor.anchor_id,
    )


def _copy_anchor(anchor: AnchorRecord) -> AnchorRecord:
    return AnchorRecord(
        symbol=anchor.symbol,
        anchor_ts=anchor.anchor_ts,
        label=anchor.label,
        color=anchor.color,
        line_width=anchor.line_width,
        line_style=anchor.line_style,
        show_anchor=anchor.show_anchor,
        anchor_id=anchor.anchor_id,
    )


from indicators.avwap.anchor_store import AvwapAnchorStore  # noqa: E402

register_store_handler(AvwapAnchorStore)
