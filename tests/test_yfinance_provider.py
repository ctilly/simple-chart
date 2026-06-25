from datetime import UTC, datetime

import pandas as pd
import pytest

from data.provider.yfinance_provider import (
    _level1_from_info,
    _rows_to_bars,
    _snapshot_from_daily_history,
    _snapshots_from_downloaded_history,
    _snapshot_from_fast_info,
    _snapshot_from_price_fields,
    _snapshot_from_pricing_data,
)


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


def test_rows_to_bars_drops_flat_zero_volume_placeholders_when_requested() -> None:
    df = _frame(
        [
            {"Open": 135.0, "High": 135.0, "Low": 135.0, "Close": 135.0, "Volume": 0},
            {"Open": 150.0, "High": 176.52, "Low": 149.34, "Close": 160.95, "Volume": 519_234_800},
        ]
    )

    bars = _rows_to_bars(df, drop_flat_zero_volume_placeholders=True)

    assert len(bars) == 1
    assert bars[0].open == pytest.approx(150.0)
    assert bars[0].close == pytest.approx(160.95)


def test_rows_to_bars_keeps_flat_zero_volume_rows_by_default() -> None:
    df = _frame(
        [
            {"Open": 135.0, "High": 135.0, "Low": 135.0, "Close": 135.0, "Volume": 0},
        ]
    )

    bars = _rows_to_bars(df)

    assert len(bars) == 1
    assert bars[0].volume == 0


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


def test_snapshot_from_pricing_data_uses_change_percent() -> None:
    snapshot = _snapshot_from_pricing_data(
        {
            "id": "spy",
            "price": 523.4,
            "change": -1.25,
            "change_percent": -0.238,
            "previous_close": 524.65,
            "time": 1_777_650_000_000,
        }
    )

    assert snapshot is not None
    assert snapshot.symbol == "SPY"
    assert snapshot.last_price == pytest.approx(523.4)
    assert snapshot.change == pytest.approx(-1.25)
    assert snapshot.change_percent == pytest.approx(-0.238)
    assert snapshot.previous_close == pytest.approx(524.65)
    assert snapshot.timestamp == datetime(2026, 5, 1, 15, 40, tzinfo=UTC)


def test_snapshot_from_pricing_data_ignores_invalid_numeric_fields() -> None:
    snapshot = _snapshot_from_pricing_data(
        {
            "id": "QQQ",
            "price": "not-a-number",
            "change_percent": float("nan"),
            "previous_close": None,
            "time": "not-a-time",
        }
    )

    assert snapshot is not None
    assert snapshot.symbol == "QQQ"
    assert snapshot.last_price is None
    assert snapshot.change_percent is None
    assert snapshot.previous_close is None
    assert snapshot.timestamp is None


def test_snapshot_from_fast_info_computes_change_percent() -> None:
    snapshot = _snapshot_from_fast_info(
        "spy",
        {
            "lastPrice": 105.0,
            "regularMarketPreviousClose": 100.0,
        },
    )

    assert snapshot is not None
    assert snapshot.symbol == "SPY"
    assert snapshot.last_price == pytest.approx(105.0)
    assert snapshot.previous_close == pytest.approx(100.0)
    assert snapshot.change == pytest.approx(5.0)
    assert snapshot.change_percent == pytest.approx(5.0)


def test_snapshot_from_fast_info_falls_back_to_previous_close() -> None:
    snapshot = _snapshot_from_fast_info(
        "QQQ",
        {
            "lastPrice": 98.0,
            "regularMarketPreviousClose": None,
            "previousClose": 100.0,
        },
    )

    assert snapshot is not None
    assert snapshot.change == pytest.approx(-2.0)
    assert snapshot.change_percent == pytest.approx(-2.0)


def test_snapshot_from_price_fields_avoids_zero_previous_close() -> None:
    snapshot = _snapshot_from_price_fields("IWM", 10.0, 0.0, None)

    assert snapshot is not None
    assert snapshot.change == pytest.approx(10.0)
    assert snapshot.change_percent is None


def test_snapshot_from_daily_history_uses_latest_close_vs_previous_close() -> None:
    df = pd.DataFrame(
        [
            {"Close": 100.0},
            {"Close": 110.0},
        ],
        index=pd.DatetimeIndex(
            [
                datetime(2026, 5, 28, tzinfo=UTC),
                datetime(2026, 5, 29, tzinfo=UTC),
            ]
        ),
    )

    snapshot = _snapshot_from_daily_history("SPY", df)

    assert snapshot is not None
    assert snapshot.last_price == pytest.approx(110.0)
    assert snapshot.previous_close == pytest.approx(100.0)
    assert snapshot.change == pytest.approx(10.0)
    assert snapshot.change_percent == pytest.approx(10.0)
    assert snapshot.timestamp == datetime(2026, 5, 29, tzinfo=UTC)


def test_snapshot_from_daily_history_omits_snapshot_when_latest_row_is_nan() -> None:
    df = pd.DataFrame(
        [
            {"Close": 100.0},
            {"Close": 110.0},
            {"Close": float("nan")},
        ],
        index=pd.DatetimeIndex(
            [
                datetime(2026, 6, 5, tzinfo=UTC),
                datetime(2026, 6, 8, tzinfo=UTC),
                datetime(2026, 6, 9, tzinfo=UTC),
            ]
        ),
    )

    assert _snapshot_from_daily_history("SPY", df) is None


def test_snapshots_from_downloaded_history_handles_multi_symbol_frame() -> None:
    columns = pd.MultiIndex.from_product(
        [["SPY", "QQQ"], ["Open", "High", "Low", "Close", "Volume"]]
    )
    df = pd.DataFrame(
        [
            [99.0, 101.0, 98.0, 100.0, 1_000, 198.0, 201.0, 197.0, 200.0, 2_000],
            [109.0, 111.0, 108.0, 110.0, 1_100, 189.0, 191.0, 188.0, 190.0, 2_100],
        ],
        index=pd.DatetimeIndex(
            [
                datetime(2026, 5, 28, tzinfo=UTC),
                datetime(2026, 5, 29, tzinfo=UTC),
            ]
        ),
        columns=columns,
    )

    snapshots = _snapshots_from_downloaded_history(["SPY", "QQQ"], df)

    assert snapshots["SPY"].change_percent == pytest.approx(10.0)
    assert snapshots["QQQ"].change_percent == pytest.approx(-5.0)


def test_level1_from_info_maps_quote_fields() -> None:
    quote = _level1_from_info(
        "NVDA",
        {
            "longName": "NVIDIA Corporation",
            "regularMarketPrice": 208.19,
            "regularMarketPreviousClose": 208.64,
            "regularMarketChange": -0.45,
            "regularMarketChangePercent": -0.2157,
            "bid": 207.21,
            "bidSize": 20,
            "ask": 208.75,
            "askSize": 18,
            "regularMarketOpen": 210.615,
            "regularMarketDayHigh": 211.39,
            "regularMarketDayLow": 199.34,
            "regularMarketVolume": 179_602_708,
        },
    )

    assert quote is not None
    assert quote.company_name == "NVIDIA Corporation"
    assert quote.last_price == pytest.approx(208.19)
    assert quote.change == pytest.approx(-0.45)
    assert quote.change_percent == pytest.approx(-0.2157)
    assert quote.bid == pytest.approx(207.21)
    assert quote.bid_size == 20
    assert quote.ask_size == 18
    assert quote.high == pytest.approx(211.39)
    assert quote.volume == 179_602_708


def test_level1_from_info_computes_change_when_absent() -> None:
    quote = _level1_from_info(
        "SPY",
        {"regularMarketPrice": 110.0, "previousClose": 100.0},
    )

    assert quote is not None
    assert quote.change == pytest.approx(10.0)
    assert quote.change_percent == pytest.approx(10.0)
    assert quote.bid is None
    assert quote.company_name is None


def test_level1_from_info_requires_some_price_data() -> None:
    assert _level1_from_info("SPY", {"longName": "SPDR S&P 500"}) is None
