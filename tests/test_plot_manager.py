from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import inf, nextafter
from types import SimpleNamespace

import finplot as fplt
import pandas as pd
import pytest
from pytestqt.qtbot import QtBot

from chart.plot_manager import _bar_datetime_index, _hollow_candle_colorfilter, _x_value_for_index
from chart.window import ChartWidget
from data.models import Bar, OHLCVSeries, Timeframe
from simplechart.api import MarkerRender


@pytest.fixture
def candle_source() -> fplt.PandasDataSource:
    return _candle_source([
        (391.0, 384.96),
        (370.51, 380.20),  # TSLA 11/25
        (395.0, 390.0),
        (389.0, 391.0),
        (392.0, 391.0),
        (384.0, 384.0),
    ])


def _candle_source(prices: list[tuple[float, float]]) -> fplt.PandasDataSource:
    df = pd.DataFrame({
        "open": [op for op, _ in prices],
        "close": [close for _, close in prices],
        "high": [max(op, close) for op, close in prices],
        "low": [min(op, close) for op, close in prices],
    }, index=pd.date_range("2026-11-01", periods=len(prices), tz="UTC"))
    return fplt.PandasDataSource(df)


def _group_colors(group: str) -> tuple[str, str, str]:
    return group, group, group


def _groups_by_label(datasrc: fplt.PandasDataSource, rows: pd.DataFrame) -> dict[int, str]:
    item = SimpleNamespace(rowcolors=_group_colors)
    return {
        label: group
        for _, group, _, candles in _hollow_candle_colorfilter(item, datasrc, rows)
        for label in candles.index
    }


def test_hollow_candle_groups_include_ties_and_first_loaded_bar(
    candle_source: fplt.PandasDataSource,
) -> None:
    assert _groups_by_label(candle_source, candle_source.df) == {
        0: "bear",       # First loaded bar falls back to open.
        1: "weak_bear",  # Hollow red.
        2: "weak_bull",  # Filled green.
        3: "bull",       # Hollow green.
        4: "weak_bull",  # Tying the previous close is green.
        5: "weak_bear",  # Tying the open is hollow.
    }


def test_hollow_candles_use_close_before_visible_slice(candle_source: fplt.PandasDataSource) -> None:
    rows = candle_source.df.loc[2:3]

    assert _groups_by_label(candle_source, rows) == {2: "weak_bull", 3: "bull"}


def test_hollow_candles_classify_from_raw_prices_on_log_scale() -> None:
    source = _candle_source([(98.0, 99.0), (nextafter(100.0, inf), 100.0)])
    rows, _ = source.rows(5, 1, 1, fplt.YScale("log", 1), resamp="hilo")

    assert rows.loc[1, "open"] == rows.loc[1, "close"]
    assert _groups_by_label(source, rows) == {1: "weak_bull"}


def test_hollow_candles_classify_from_aggregates_at_lod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fplt, "lod_candles", 11)
    source = _candle_source(
        [(30.0, 30.0)] * 2
        + [(10.0, 9.0)] * 4 + [(10.0, 11.0)]
        + [(9.0, 8.0)] * 4 + [(9.0, 10.0)]
    )
    rows, _ = source.rows(5, 0, 11, fplt.YScale("linear", 1), resamp="hilo")

    assert _groups_by_label(source, rows) == {2: "bull", 7: "weak_bear"}


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
    assert marker_items[0].pos().x() == 2.0
    assert marker_items[0].pos().y() == 101.0
    assert plot_manager._markers["avwap_anchor_1"] is marker_items[0]


def _minute_series(count: int, first: datetime) -> OHLCVSeries:
    bars = [
        Bar(
            timestamp=first + timedelta(minutes=5 * index),
            open=100.0 + index % 7,
            high=102.0 + index % 7,
            low=99.0 + index % 7,
            close=101.0 + index % 7,
            volume=1_000 + index,
        )
        for index in range(count)
    ]
    return OHLCVSeries(symbol="XBI", timeframe=Timeframe.MIN5, bars=bars)


def test_updating_the_previous_bar_recolors_the_next_candle(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fplt, "windows", [])
    monkeypatch.setattr(fplt, "master_data", {})
    monkeypatch.setattr(fplt, "last_ax", None)
    widget = ChartWidget(
        on_toggle=lambda _key: None,
        on_configure=lambda _key: None,
        on_remove=lambda _key: None,
        on_add=lambda: None,
        on_drawing_tool=lambda _name: None,
        drawing_tools=[],
    )
    qtbot.addWidget(widget)
    manager = widget.plot_manager
    series = _minute_series(3, datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc))
    manager.draw_candles(series)
    item = manager._candle_plot
    assert isinstance(item, fplt.CandlestickItem)

    def next_candle_frames() -> list[str]:
        source = item.datasrc
        rows, _ = source.rows(5, 1, 1, item.ax.vb.yscale, resamp=item.resamp)
        return [frame for _, frame, _, candles in item.colorfunc(item, source, rows) if 1 in candles.index]

    assert next_candle_frames() == [manager._colors.candle_up]

    revised = replace(series.bars[0], high=104.0, close=103.0)
    manager.draw_candles(replace(series, bars=[revised, *series.bars[1:]]))

    assert manager._candle_plot is item
    assert next_candle_frames() == [manager._colors.candle_down]


def test_preserving_viewport_shift_keeps_view_on_same_bars_after_prepend(
    qtbot: object,
) -> None:
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
    first = datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc)
    recent = _minute_series(300, first)
    plot_manager.draw_candles(recent)
    plot_manager.draw_volume(recent)
    plot_manager.refresh()
    for axis in plot_manager._viewport_axes():
        axis.vb.set_range(250.0, 90.0, 290.0, 120.0)  # type: ignore[attr-defined]
    before = [
        axis.vb.targetRect()  # type: ignore[attr-defined]
        for axis in plot_manager._viewport_axes()
    ]

    full = _minute_series(400, first - timedelta(minutes=5 * 100))
    with plot_manager.preserving_viewport(x_shift=100.0):
        plot_manager.draw_candles(full)
        plot_manager.draw_volume(full)

    after = [
        axis.vb.targetRect()  # type: ignore[attr-defined]
        for axis in plot_manager._viewport_axes()
    ]
    for old, new in zip(before, after):
        assert new.left() == old.left() + 100.0
        assert new.right() == old.right() + 100.0
