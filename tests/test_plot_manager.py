from datetime import datetime, timezone

import pandas as pd

from chart.window import ChartWidget
from chart.plot_manager import _bar_datetime_index, _x_value_for_index
from data.models import Bar, OHLCVSeries, Timeframe
from simplechart.api import MarkerRender


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


def test_bar_datetime_index_shifts_daily_bars_to_noon_utc_for_display() -> None:
    series = OHLCVSeries(
        symbol="SPY",
        timeframe=Timeframe.DAILY,
        bars=[
            Bar(
                timestamp=datetime(2026, 5, 29, tzinfo=timezone.utc),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1_000,
            )
        ],
    )

    index = _bar_datetime_index(series)

    assert index[0] == pd.Timestamp("2026-05-29T12:00:00Z")


def test_bar_datetime_index_preserves_intraday_bar_time() -> None:
    timestamp = datetime(2026, 5, 29, 14, 30, tzinfo=timezone.utc)
    series = OHLCVSeries(
        symbol="SPY",
        timeframe=Timeframe.MIN15,
        bars=[
            Bar(
                timestamp=timestamp,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1_000,
            )
        ],
    )

    index = _bar_datetime_index(series)

    assert index[0] == pd.Timestamp("2026-05-29T14:30:00Z")


def test_clear_all_removes_marker_items_from_price_axis(qtbot: object) -> None:
    widget = ChartWidget(
        on_toggle=lambda _key: None,
        on_configure=lambda _key: None,
        on_remove=lambda _key: None,
        on_add=lambda: None,
        on_drawing_tool=lambda _name: None,
        drawing_tools=[],
    )
    qtbot.addWidget(widget)  # type: ignore[attr-defined]
    plot_manager = widget.plot_manager

    plot_manager.update_marker(
        MarkerRender(
            key="avwap_anchor_1",
            x_index=1.0,
            y_value=100.0,
            text="A",
            color="#000000",
        )
    )

    plot_manager.clear_all()

    marker_items = [
        item
        for item in plot_manager._price_panel.ax.items
        if getattr(item, "_simplechart_marker_key", None) == "avwap_anchor_1"
    ]
    assert marker_items == []


def test_clear_all_removes_untracked_marker_items_from_price_axis(qtbot: object) -> None:
    widget = ChartWidget(
        on_toggle=lambda _key: None,
        on_configure=lambda _key: None,
        on_remove=lambda _key: None,
        on_add=lambda: None,
        on_drawing_tool=lambda _name: None,
        drawing_tools=[],
    )
    qtbot.addWidget(widget)  # type: ignore[attr-defined]
    plot_manager = widget.plot_manager

    plot_manager.update_marker(
        MarkerRender(
            key="avwap_anchor_1",
            x_index=1.0,
            y_value=100.0,
            text="A",
            color="#000000",
        )
    )
    plot_manager._markers.clear()

    plot_manager.clear_all()

    marker_items = [
        item
        for item in plot_manager._price_panel.ax.items
        if getattr(item, "_simplechart_marker_key", None) == "avwap_anchor_1"
    ]
    assert marker_items == []


def test_update_marker_removes_same_key_orphan_items(qtbot: object) -> None:
    widget = ChartWidget(
        on_toggle=lambda _key: None,
        on_configure=lambda _key: None,
        on_remove=lambda _key: None,
        on_add=lambda: None,
        on_drawing_tool=lambda _name: None,
        drawing_tools=[],
    )
    qtbot.addWidget(widget)  # type: ignore[attr-defined]
    plot_manager = widget.plot_manager

    plot_manager.update_marker(
        MarkerRender(
            key="avwap_anchor_1",
            x_index=1.0,
            y_value=100.0,
            text="A",
            color="#000000",
        )
    )
    plot_manager._markers.clear()
    plot_manager.update_marker(
        MarkerRender(
            key="avwap_anchor_1",
            x_index=2.0,
            y_value=101.0,
            text="A",
            color="#000000",
        )
    )

    marker_items = [
        item
        for item in plot_manager._price_panel.ax.items
        if getattr(item, "_simplechart_marker_key", None) == "avwap_anchor_1"
    ]
    assert len(marker_items) == 1
