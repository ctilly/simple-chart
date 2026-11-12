"""
Per-series NumPy views of bar data, built once and shared.

Indicators and tools that need arrays (timestamps, prices, volumes) call
series_arrays(series) instead of rebuilding them from the Bar list on every
render or drag frame. The arrays are read-only. A series' bars are treated as
immutable once built; replacing or resizing the bar list rebuilds the arrays.
"""

import weakref
from dataclasses import dataclass

import numpy as np

from data.models import Bar, OHLCVSeries


@dataclass(frozen=True)
class SeriesArrays:
    timestamps_ms: np.ndarray  # int64 UTC milliseconds, oldest first
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray  # float64


# id(series) -> (weak reference to the series, its bar list, arrays). Keyed
# by id because OHLCVSeries is an unhashable dataclass; the weak reference's
# callback drops the entry when the series is collected.
_cache: dict[int, tuple["weakref.ref[OHLCVSeries]", list[Bar], SeriesArrays]] = {}


def series_arrays(series: OHLCVSeries) -> SeriesArrays:
    key = id(series)
    entry = _cache.get(key)
    if entry is not None:
        ref, bars, arrays = entry
        if ref() is series and bars is series.bars and len(arrays.close) == len(bars):
            return arrays
    arrays = _build(series)

    def forget(_ref: "weakref.ref[OHLCVSeries]") -> None:
        _cache.pop(key, None)

    _cache[key] = (weakref.ref(series, forget), series.bars, arrays)
    return arrays


def _build(series: OHLCVSeries) -> SeriesArrays:
    bars = series.bars
    arrays = SeriesArrays(
        timestamps_ms=np.fromiter(
            (int(bar.timestamp.timestamp() * 1000) for bar in bars),
            dtype=np.int64,
            count=len(bars),
        ),
        open=np.fromiter((bar.open for bar in bars), dtype=float, count=len(bars)),
        high=np.fromiter((bar.high for bar in bars), dtype=float, count=len(bars)),
        low=np.fromiter((bar.low for bar in bars), dtype=float, count=len(bars)),
        close=np.fromiter((bar.close for bar in bars), dtype=float, count=len(bars)),
        volume=np.fromiter((bar.volume for bar in bars), dtype=float, count=len(bars)),
    )
    for array in (
        arrays.timestamps_ms,
        arrays.open,
        arrays.high,
        arrays.low,
        arrays.close,
        arrays.volume,
    ):
        array.setflags(write=False)
    return arrays
