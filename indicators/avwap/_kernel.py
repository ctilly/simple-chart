"""
indicators/avwap/_kernel.py

Compiled AVWAP (Anchored Volume Weighted Average Price) kernel.

This module is eligible for mypyc compilation. The same rules apply as
in ema/_kernel.py: strict types, no I/O, no dynamic dispatch, Python
scalars preferred in loop bodies.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHAT IS AVWAP?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Standard VWAP resets every session (daily). Anchored VWAP starts
accumulating from a user-defined bar and never resets. This makes it
useful for identifying price levels relative to significant events:
earnings, breakouts, prior highs/lows, etc.

Formula (from the anchor bar forward):
    typical_price[i] = (high[i] + low[i] + close[i]) / 3
    cumulative_tp_vol += typical_price[i] * volume[i]
    cumulative_vol    += volume[i]
    avwap[i]           = cumulative_tp_vol / cumulative_vol

Bars before the anchor are NaN — AVWAP has no value there.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ANCHOR INDEX vs. ANCHOR TIMESTAMP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This kernel works with integer array indexes, not timestamps. The
translation from UTC millisecond timestamp (as stored in AnchorRecord)
to the bar index in the current array is done by AVWAPIndicator before
calling this function. This keeps the kernel free of any awareness of
dates, timeframes, or the calendar — it only sees numbers.
"""

import numpy as np


def avwap_multi(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    anchor_indexes: list[int],
) -> list[np.ndarray]:
    """
    Compute AVWAP for multiple anchor points in a single pass over the bars.

    More efficient than computing each anchor separately — the bar arrays
    are traversed once rather than once per anchor.

    Args:
        highs, lows, closes, volumes: price/volume arrays, oldest-first
        anchor_indexes: list of bar indexes where each AVWAP begins,
                        not required to be sorted

    Returns:
        List of arrays in the same order as anchor_indexes. Each array
        is the same length as closes.
    """
    n: int = len(closes)
    num_anchors: int = len(anchor_indexes)

    if num_anchors == 0:
        return []

    # One accumulator pair per anchor.
    cum_tp_vols: list[float] = [0.0] * num_anchors
    cum_vols: list[float] = [0.0] * num_anchors
    results: list[np.ndarray] = [np.full(n, np.nan) for _ in range(num_anchors)]

    for i in range(n):
        tp: float = (float(highs[i]) + float(lows[i]) + float(closes[i])) / 3.0
        vol: float = float(volumes[i])

        for j in range(num_anchors):
            if i >= anchor_indexes[j]:
                cum_tp_vols[j] += tp * vol
                cum_vols[j] += vol
                if cum_vols[j] > 0.0:
                    results[j][i] = cum_tp_vols[j] / cum_vols[j]

    return results
