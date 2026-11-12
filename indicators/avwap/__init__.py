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
     a bar index and takes running sums of typical price x volume and of
     volume from that bar.
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

AVWAP is a running sum, not a recurrence, so it needs no compiled kernel:
each anchor is two NumPy cumulative sums over the bars from its anchor bar,
using the series' shared arrays (series_arrays). A drag frame therefore costs
time proportional to the bars after the anchor, not to all loaded bars.
"""

from datetime import datetime, timedelta, timezone
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
    series_arrays,
)
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
# Horizontal grab radius around an anchor's first bar, in screen pixels, but
# never less than this many bars when zoomed in.
_ANCHOR_GRAB_RADIUS_PX = 6.0
_ANCHOR_GRAB_MIN_BARS = 0.75
# Intraday charts draw anchors at most this many calendar days before their
# newest bar: the app's initial intraday window. Older anchors would make an
# intraday chart load years of bars; daily and weekly charts draw them.
_MAX_INTRADAY_ANCHOR_DAYS = 600


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

    def history_lookback_days(self, params: dict[str, Any]) -> int | None:
        # Each anchor needs bars back to its own timestamp; anchors older than
        # the loaded bars are omitted until history reaches them (render()).
        return 0

    def compute(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> dict[str, np.ndarray]:
        """
        Compute one AVWAP series per anchor in params["anchors"].

        If there are no anchors, returns an empty dict — nothing to draw.

        Each value is the cumulative typical price x volume divided by the
        cumulative volume from the anchor's bar; NaN before the anchor and
        until volume is seen.
        """
        anchors, _ = _split_loaded_anchors(series, params)
        if not anchors:
            return {}

        arrays = series_arrays(series)
        n = len(arrays.close)
        # Persisted anchors use their DB id so visibility survives drag
        # timestamp edits.
        return {
            avwap_anchor_key(anchor): _avwap_from(
                arrays.high,
                arrays.low,
                arrays.close,
                arrays.volume,
                _anchor_bar_index(arrays.timestamps_ms, anchor.anchor_ts),
                n,
            )
            for anchor in anchors
        }

    def render(
        self,
        series: OHLCVSeries,
        params: dict[str, Any],
    ) -> ChartExtensionRender:
        anchors: list[AnchorRecord] = params.get("anchors", [])
        result = self.compute(series, params)
        render = ChartExtensionRender()
        render.notice = _too_far_back_notice(_too_far_back(series, anchors))
        _, unloaded = _split_loaded_anchors(series, params)
        if unloaded:
            render.pending_history_start_ms = max(
                anchor.anchor_ts for anchor in unloaded
            )
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
        label = _utc_from_ms(event.timestamp_ms).strftime("%Y-%m-%d")
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
        # An anchor is grabbed at the bar its line starts on, within a radius
        # measured in pixels so it stays grabbable when zoomed out.
        radius = max(_ANCHOR_GRAB_MIN_BARS, _ANCHOR_GRAB_RADIUS_PX * event.pixel_size_x)
        timestamps_ms = series_arrays(series).timestamps_ms
        best_key: str | None = None
        best_distance = radius
        for anchor in params.get("anchors", []):
            key = avwap_anchor_key(anchor)
            if key not in visible_keys:
                continue
            anchor_index = _anchor_bar_index(timestamps_ms, anchor.anchor_ts)
            if anchor_index >= len(timestamps_ms):
                continue
            # The event's resolved bar is an exact match on any axis; the
            # pixel radius applies to the bar-index x of the indexed chart.
            distance = 0.0 if event.bar_index == anchor_index else abs(event.x - anchor_index)
            if distance <= best_distance and _anchor_y_hit_test(series, anchor_index, event.y):
                best_key = key
                best_distance = distance
        if best_key is None:
            return None
        return HitTestResult(
            extension_name=self.name(),
            handle_key=best_key,
            cursor="drag",
            priority=10,
        )

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
        y_range: tuple[float, float] | None = None,
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


def _too_far_back(
    series: OHLCVSeries,
    anchors: list[AnchorRecord],
) -> list[AnchorRecord]:
    """
    Anchors an intraday chart does not draw: more than
    _MAX_INTRADAY_ANCHOR_DAYS before its newest bar. Drawing them would load
    years of intraday bars; daily and weekly charts draw them.
    """
    if not series.timeframe.is_intraday or not series.bars:
        return []
    cutoff_ms = int(
        (
            series.bars[-1].timestamp - timedelta(days=_MAX_INTRADAY_ANCHOR_DAYS)
        ).timestamp()
        * 1000
    )
    return [anchor for anchor in anchors if anchor.anchor_ts < cutoff_ms]


def _too_far_back_notice(anchors: list[AnchorRecord]) -> str | None:
    if not anchors:
        return None
    if len(anchors) == 1:
        subject = f"AVWAP {anchors[0].label} is"
        pronoun = "it shows"
    else:
        subject = f"{len(anchors)} AVWAPs are"
        pronoun = "they show"
    return (
        f"{subject} anchored more than {_MAX_INTRADAY_ANCHOR_DAYS} days back, "
        f"so {pronoun} only on daily and weekly charts."
    )


def _split_loaded_anchors(
    series: OHLCVSeries,
    params: dict[str, Any],
) -> tuple[list[AnchorRecord], list[AnchorRecord]]:
    """
    Split anchors into those computable on the loaded bars and those older
    than the first loaded bar while older history can still arrive (the chart
    loads it until it reaches them). Once nothing older exists every anchor is
    computable (an anchor older than all history starts at bar 0).
    """
    too_far_back = _too_far_back(series, params.get("anchors", []))
    anchors: list[AnchorRecord] = [
        anchor for anchor in params.get("anchors", []) if anchor not in too_far_back
    ]
    if params.get("_history_complete", True) or not series.bars:
        return anchors, []
    first_ts_ms = int(series.bars[0].timestamp.timestamp() * 1000)
    loaded = [anchor for anchor in anchors if anchor.anchor_ts >= first_ts_ms]
    unloaded = [anchor for anchor in anchors if anchor.anchor_ts < first_ts_ms]
    return loaded, unloaded


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


def _anchor_bar_index(timestamps_ms: np.ndarray, anchor_ts: int) -> int:
    """
    The bar an anchor's line starts on: the last bar opening at or before
    anchor_ts (the bar containing it), 0 for an anchor older than every bar,
    and len(timestamps_ms) for one at least a full bar past the newest bar.
    Same rule as simplechart.api.timestamp_ms_to_bar_index, on an array.
    """
    n = len(timestamps_ms)
    if n == 0:
        return 0
    index = int(np.searchsorted(timestamps_ms, anchor_ts, side="right")) - 1
    if index < 0:
        return 0
    if index == n - 1 and n > 1 and anchor_ts > timestamps_ms[-1]:
        last_step = int(timestamps_ms[-1] - timestamps_ms[-2])
        if last_step > 0 and anchor_ts >= int(timestamps_ms[-1]) + last_step:
            return n
    return index


def _avwap_from(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    start: int,
    n: int,
) -> np.ndarray:
    values = np.full(n, np.nan)
    if start >= n:
        return values
    volume = volumes[start:]
    cum_volume = np.cumsum(volume)
    cum_tp_volume = np.cumsum((highs[start:] + lows[start:] + closes[start:]) / 3.0 * volume)
    traded = cum_volume > 0.0
    values[start:][traded] = cum_tp_volume[traded] / cum_volume[traded]
    return values


def _anchor_target_is_duplicate(
    series: OHLCVSeries,
    anchors: list[AnchorRecord],
    target_idx: int,
    moving_key: str | None,
) -> bool:
    timestamps_ms = series_arrays(series).timestamps_ms
    for anchor in anchors:
        if moving_key is not None and avwap_anchor_key(anchor) == moving_key:
            continue
        if _anchor_bar_index(timestamps_ms, anchor.anchor_ts) == target_idx:
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
        label=_utc_from_ms(target_ts).strftime("%Y-%m-%d"),
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


def _utc_from_ms(timestamp_ms: int) -> datetime:
    # Not datetime.fromtimestamp: it rejects times before 1970 on Windows.
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=timestamp_ms)
