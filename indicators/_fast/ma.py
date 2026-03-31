"""
indicators/_fast/ma.py

Compiled moving average kernels.

This module is compiled to a native C extension by mypyc. Rules that must
be followed to keep it compilable:

  - All function parameters and return values must be explicitly typed
  - No use of Any, Union, or dynamic attribute access
  - No I/O of any kind (no file access, no SQLite, no print)
  - Numpy arrays cross the boundary freely; Python scalars (int, float)
    are preferred over numpy scalars in loop bodies because mypyc generates
    tighter code for Python builtins
  - No default mutable arguments

Each function takes one or more numpy arrays plus scalar parameters and
returns a single numpy array of the same length as the input. Bars before
the indicator has enough data are filled with np.nan.

These functions know nothing about symbols, timeframes, or dates. That
context lives in the Indicator subclasses that call these kernels.
"""

import numpy as np


def sma(closes: np.ndarray, period: int) -> np.ndarray:
    """
    Simple Moving Average.

    result[i] = mean of closes[i - period + 1 : i + 1]

    The first valid value is at index period - 1. All prior values are NaN.

    Implementation uses the cumulative sum trick to avoid an O(n*period)
    loop: prepend a zero, take the cumsum, then each window sum is just
    the difference of two cumsum values — O(n) overall.

    Args:
        closes: array of closing prices, oldest-first
        period: number of bars in the averaging window

    Returns:
        Array of the same length as closes. First (period - 1) values are NaN.
    """
    n: int = len(closes)
    result: np.ndarray = np.full(n, np.nan)

    if period < 1 or period > n:
        return result

    # Prepend 0.0 so cumsum[i] = sum of closes[0 : i].
    # Window sum for bar i (i >= period-1):
    #   sum(closes[i-period+1 : i+1]) = cumsum[i+1] - cumsum[i+1-period]
    padded: np.ndarray = np.concatenate(([0.0], closes.astype(float)))
    cumsum: np.ndarray = np.cumsum(padded)
    result[period - 1:] = (cumsum[period:] - cumsum[:n - period + 1]) / period

    return result


def ema(closes: np.ndarray, period: int) -> np.ndarray:
    """
    Exponential Moving Average.

    Uses the standard smoothing factor k = 2 / (period + 1).
    Seeded with the SMA of the first `period` bars (the most common
    convention used by charting platforms including TradeStation).

    The first valid value is at index period - 1. All prior values are NaN.

    The inner loop runs in compiled C when this module is built with mypyc,
    which is the main reason this function lives in _fast/ rather than being
    expressed as a vectorized numpy operation — EMA cannot be cleanly
    vectorized because each value depends on the previous.

    Args:
        closes: array of closing prices, oldest-first
        period: EMA period (determines smoothing factor and seed window)

    Returns:
        Array of the same length as closes. First (period - 1) values are NaN.
    """
    n: int = len(closes)
    result: np.ndarray = np.full(n, np.nan)

    if period < 1 or period > n:
        return result

    k: float = 2.0 / (period + 1)
    one_minus_k: float = 1.0 - k

    # Seed: SMA of the first `period` bars.
    seed: float = float(np.mean(closes[:period]))
    result[period - 1] = seed

    # Iterate from the bar after the seed onward.
    prev: float = seed
    for i in range(period, n):
        current: float = float(closes[i]) * k + prev * one_minus_k
        result[i] = current
        prev = current

    return result
