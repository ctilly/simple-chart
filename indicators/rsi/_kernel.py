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
    avg_gains, avg_losses = wilder_averages(closes, period)
    return rsi_from_averages(avg_gains, avg_losses)


def wilder_averages(closes: np.ndarray, period: int) -> list[np.ndarray]:
    """Return average gain and loss through each close, with NaN before the seed."""
    n: int = len(closes)
    avg_gains: np.ndarray = np.full(n, np.nan)
    avg_losses: np.ndarray = np.full(n, np.nan)

    if period < 1 or n < period + 1:
        return [avg_gains, avg_losses]

    seed_sums = wilder_seed_sums(closes, period)
    avg_gain: float = float(seed_sums[0]) / period
    avg_loss: float = float(seed_sums[1]) / period
    avg_gains[period] = avg_gain
    avg_losses[period] = avg_loss

    for i in range(period + 1, n):
        change: float = float(closes[i]) - float(closes[i - 1])
        gain: float = change if change > 0.0 else 0.0
        loss: float = -change if change < 0.0 else 0.0

        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        avg_gains[i] = avg_gain
        avg_losses[i] = avg_loss

    return [avg_gains, avg_losses]


def wilder_seed_sums(closes: np.ndarray, changes: int) -> np.ndarray:
    """Return total gain and loss over the first `changes` price changes."""
    gain_sum: float = 0.0
    loss_sum: float = 0.0
    for i in range(1, changes + 1):
        change: float = float(closes[i]) - float(closes[i - 1])
        if change > 0.0:
            gain_sum += change
        else:
            loss_sum -= change
    return np.array([gain_sum, loss_sum])


def rsi_from_averages(avg_gains: np.ndarray, avg_losses: np.ndarray) -> np.ndarray:
    has_loss = avg_losses != 0.0
    strength = np.zeros_like(avg_gains)
    np.divide(avg_gains, avg_losses, out=strength, where=has_loss)
    return np.where(has_loss, 100.0 - 100.0 / (1.0 + strength), 100.0)
