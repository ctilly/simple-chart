from datetime import UTC, datetime

import pandas as pd
import pytest

from data.provider.yfinance_provider import _rows_to_bars


def _frame(rows: list[dict[str, float | int | None]]) -> pd.DataFrame:
    index = pd.DatetimeIndex(
        [
            datetime(2026, 4, 8, 14, 30 + offset, tzinfo=UTC)
            for offset in range(len(rows))
        ]
    )
    return pd.DataFrame(rows, index=index)


def test_rows_to_bars_drops_invalid_trailing_row() -> None:
    df = _frame(
        [
            {"Open": 100.0, "High": 101.0, "Low": 99.5, "Close": 100.5, "Volume": 1000},
            {"Open": 100.5, "High": 102.0, "Low": 100.0, "Close": 101.5, "Volume": 1200},
            {"Open": None, "High": None, "Low": None, "Close": None, "Volume": None},
        ]
    )

    bars = _rows_to_bars(df)

    assert len(bars) == 2
    assert bars[-1].close == pytest.approx(101.5)


def test_rows_to_bars_raises_on_invalid_interior_row() -> None:
    df = _frame(
        [
            {"Open": 100.0, "High": 101.0, "Low": 99.5, "Close": 100.5, "Volume": 1000},
            {"Open": None, "High": 102.0, "Low": 100.0, "Close": 101.5, "Volume": 1200},
            {"Open": 101.5, "High": 103.0, "Low": 101.0, "Close": 102.5, "Volume": 1100},
        ]
    )

    with pytest.raises(ValueError, match="invalid interior bar"):
        _rows_to_bars(df)
