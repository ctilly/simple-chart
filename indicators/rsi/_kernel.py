"""
indicators/rsi/_kernel.py

Compiled RSI kernel.

This module is eligible for mypyc compilation. It keeps the recurrence loop
outside the indicator glue so RSI follows the same boundary as EMA.
"""

import numpy as np


def rsi(closes: np.ndarray, period: int) -> np.ndarray:
    """
    Compute RSI using Wilder's smoothing method.

    Returns an array of the same length as closes. The first `period` values
    are NaN because RSI needs `period` changes to seed its averages.
    """
    n: int = len(closes)
    result: np.ndarray = np.full(n, np.nan)

    if period < 1 or n < period + 1:
        return result

    avg_gain: float = 0.0
    avg_loss: float = 0.0
    for i in range(1, period + 1):
        change: float = float(closes[i]) - float(closes[i - 1])
        if change > 0.0:
            avg_gain += change
        else:
            avg_loss -= change

    avg_gain /= period
    avg_loss /= period
    result[period] = _rsi_value(avg_gain, avg_loss)

    for i in range(period + 1, n):
        change = float(closes[i]) - float(closes[i - 1])
        gain: float = change if change > 0.0 else 0.0
        loss: float = -change if change < 0.0 else 0.0

        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        result[i] = _rsi_value(avg_gain, avg_loss)

    return result


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0
    rs: float = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
