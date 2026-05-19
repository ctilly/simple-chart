import pandas as pd

from chart.plot_manager import _x_value_for_index


def test_x_value_for_index_keeps_index_coordinates_in_x_indexed_mode() -> None:
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-01-02T14:30:00Z"),
            pd.Timestamp("2026-01-02T14:45:00Z"),
        ]
    )

    assert _x_value_for_index(1.25, index, True) == 1.25


def test_x_value_for_index_uses_finplot_nanosecond_time_coordinates() -> None:
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-01-02T14:30:00Z"),
            pd.Timestamp("2026-01-02T14:45:00Z"),
            pd.Timestamp("2026-01-02T15:00:00Z"),
        ]
    )

    assert _x_value_for_index(1.0, index, False) == float(index[1].value)
    assert _x_value_for_index(1.5, index, False) == (
        float(index[1].value) + ((float(index[2].value) - float(index[1].value)) * 0.5)
    )
    assert _x_value_for_index(3.0, index, False) == (
        float(index[2].value) + (float(index[2].value) - float(index[1].value))
    )
