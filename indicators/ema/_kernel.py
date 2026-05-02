"""
indicators/ema/_kernel.py

Compiled EMA kernel.

This module is eligible for mypyc compilation. Rules that must be followed
to keep it compilable:

  - All function parameters and return values must be explicitly typed
  - No use of Any, Union, or dynamic attribute access
  - No I/O of any kind (no file access, no SQLite, no print)
  - Python scalars (int, float) preferred over numpy scalars in loop bodies
  - No default mutable arguments
"""

import numpy as np


def ema(closes: np.ndarray, period: int) -> np.ndarray:
    """
    Exponential Moving Average.

    Uses the standard smoothing factor k = 2 / (period + 1).
    Seeded with the SMA of the first `period` bars (the most common
    convention used by charting platforms including TradeStation).

    The first valid value is at index period - 1. All prior values are NaN.

    The inner loop runs in compiled C when this module is built with mypyc,
    which is the main reason this lives in _kernel.py rather than being
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
