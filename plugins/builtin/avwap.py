"""
plugins/builtin/avwap.py

Anchored VWAP indicator.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW AVWAP ANCHORS WORK IN THIS SYSTEM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1. User clicks a bar on the chart.
  2. The chart layer emits bar_clicked(utc_ts_ms: int).
  3. The controller creates an AnchorRecord and persists it via cache.put_anchor().
  4. The controller adds the anchor to this indicator's params["anchors"] list.
  5. The controller calls compute() which resolves each anchor timestamp to
     a bar index and delegates to the avwap_multi() compiled kernel.
  6. compute() returns one named array per anchor.
  7. The chart layer draws each as a separate plot line.

The anchor timestamp is stored in UTC milliseconds (integer). It never
changes — it is calendar time, not a bar index. This means:
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
one plot series, keyed by "avwap_{anchor_ts}". Adding a new anchor appends
to the list and triggers a recompute; removing one removes it from the list.

The avwap_multi() kernel processes all anchors in a single pass over the
bar arrays, which is more efficient than calling avwap() once per anchor.
"""

from typing import Any

import numpy as np

from data.calendar import timestamp_ms_to_bar_index
from data.models import AnchorRecord, OHLCVSeries
from indicators._fast.avwap import avwap_multi as _avwap_multi_kernel
from indicators.base import Indicator
from indicators.registry import register


class AVWAPIndicator(Indicator):

    def name(self) -> str:
        return "avwap"

    def label(self) -> str:
        return "Anchored VWAP"

    def default_params(self) -> dict[str, Any]:
        # anchors is a list of AnchorRecord. Starts empty — anchors are
        # added interactively by clicking bars on the chart.
        return {"anchors": []}

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

        # Key each result by the anchor's timestamp so the chart layer can
        # match the array back to the correct AnchorRecord for display
        # (label, color). The key is stable across timeframe switches.
        return {
            f"avwap_{anchor.anchor_ts}": array
            for anchor, array in zip(anchors, arrays)
        }


register(AVWAPIndicator)
