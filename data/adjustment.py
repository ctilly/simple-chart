"""
data/adjustment.py

Detects when a provider has moved cached history to a new adjustment basis
(a split or a dividend applied to older bars).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from statistics import median

from data.models import Bar, Timeframe


@dataclass(frozen=True)
class BasisChange:
    # New price over cached price for bars on the old basis.
    ratio: float
    # New volume over cached volume: 1 / ratio for a split, 1 for a dividend.
    volume_factor: float
    # Timestamp (UTC ms) of the first compared cached bar already on the new
    # basis, when the provider adjusted older bars after newer ones were
    # cached; None when every compared bar was on the old basis.
    new_basis_from_ms: int | None


class BasisChangeChoice(str, Enum):
    """What to do with cached history older than a re-downloaded window."""

    RESCALE = "rescale"
    DISCARD = "discard"


@dataclass(frozen=True)
class BasisChangeRecord:
    """Cached bars in [old_coverage_start_ms, boundary_ms) on an old basis."""

    cache_namespace: str
    symbol: str
    timeframe: Timeframe
    old_coverage_start_ms: int
    boundary_ms: int
    ratio: float
    volume_factor: float


# Per-price tolerance: relative, but never tighter than the noise from
# rounding adjusted prices to the cent (about 0.01 / price, with margin).
_MIN_RELATIVE_TOLERANCE = 1e-4
_PRICE_ROUNDING_TOLERANCE = 0.015
_MIN_BASIS_SHIFT = 2e-4
# A block of bars scaled by this much or more is a unit or data error, not
# a corporate action.
_MAX_BASIS_RATIO = 50.0
_MIN_CHANGED_BARS = 2


def detect_basis_change(
    cached: Sequence[Bar],
    fetched: Sequence[Bar],
) -> BasisChange | None:
    """
    Compare settled cached bars with the provider's current bars for the
    same timestamps, oldest first, on open and close.

    A basis change is a run of the oldest bars all scaled by one ratio,
    optionally followed by bars that are unchanged (the provider adjusted
    older history after newer bars were cached). Anything else, such as a
    single revised bar or differing ratios, is a revision and returns None.
    """
    fetched_by_ms = {_ms(bar): bar for bar in fetched}
    changed: list[tuple[Bar, Bar]] = []
    new_basis_from_ms: int | None = None
    for cached_bar in cached:
        fetched_bar = fetched_by_ms.get(_ms(cached_bar))
        if fetched_bar is None or not _has_positive_prices(cached_bar, fetched_bar):
            continue
        open_changed = _differs(cached_bar.open, fetched_bar.open, 1.0)
        close_changed = _differs(cached_bar.close, fetched_bar.close, 1.0)
        if open_changed != close_changed:
            return None
        if not open_changed:
            if new_basis_from_ms is None:
                new_basis_from_ms = _ms(cached_bar)
            continue
        if new_basis_from_ms is not None:
            return None
        changed.append((cached_bar, fetched_bar))
    if len(changed) < _MIN_CHANGED_BARS:
        return None

    ratio = median(
        value
        for cached_bar, fetched_bar in changed
        for value in (
            fetched_bar.open / cached_bar.open,
            fetched_bar.close / cached_bar.close,
        )
    )
    if (
        abs(ratio - 1.0) < _MIN_BASIS_SHIFT
        or not 1.0 / _MAX_BASIS_RATIO < ratio < _MAX_BASIS_RATIO
    ):
        return None
    for cached_bar, fetched_bar in changed:
        if _differs(cached_bar.open, fetched_bar.open, ratio) or _differs(
            cached_bar.close,
            fetched_bar.close,
            ratio,
        ):
            return None
    return BasisChange(ratio, _volume_factor(changed, ratio), new_basis_from_ms)


def _differs(cached: float, fetched: float, ratio: float) -> bool:
    tolerance = max(
        _MIN_RELATIVE_TOLERANCE,
        _PRICE_ROUNDING_TOLERANCE / min(cached, fetched),
    )
    return abs(fetched / (cached * ratio) - 1.0) > tolerance


def _volume_factor(changed: list[tuple[Bar, Bar]], ratio: float) -> float:
    """
    1 / ratio when volumes moved inversely to prices (a split), else 1 (a
    dividend leaves volume alone).
    """
    volume_ratios = [
        fetched_bar.volume / cached_bar.volume
        for cached_bar, fetched_bar in changed
        if cached_bar.volume > 0 and fetched_bar.volume > 0
    ]
    if not volume_ratios:
        return 1.0
    volume_ratio = median(volume_ratios)
    if abs(volume_ratio * ratio - 1.0) < abs(volume_ratio - 1.0):
        return 1.0 / ratio
    return 1.0


def _has_positive_prices(cached: Bar, fetched: Bar) -> bool:
    return min(cached.open, cached.close, fetched.open, fetched.close) > 0


def _ms(bar: Bar) -> int:
    return int(bar.timestamp.timestamp() * 1000)
