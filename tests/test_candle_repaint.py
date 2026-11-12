"""
Candles disappear while panning/zooming (plan file, "Reported 2026-11-10 —
candles disappear"): finplot regenerates a candle item's cached picture only
in paint(), and Qt paints the item only while that picture overlaps the view.
A single large view move (a trackpad fling, a programmatic range, or an
older-history merge that shifts the view) left the candles blank until
Home/End. Candle pixels are counted in a grab of the chart.
"""

from typing import Any

import pytest
from PyQt6.QtGui import QColor

from app.controller import MainWindow
from chart.styles import CANDLE_DOWN, CANDLE_UP
from data.models import Timeframe
from test_basis_change import _isolated_finplot_windows, _show  # noqa: F401  (fixture)
from test_older_history import (
    _FLOOR,
    _FlooredSource,
    _alpaca_window,
    _older,
    _seed_alpaca,
)


def _candle_pixels(qtbot: Any, window: MainWindow) -> int:
    qtbot.wait(100)
    image = window._chart.grab().toImage()
    colors = {QColor(CANDLE_UP).rgb() & 0xFFFFFF, QColor(CANDLE_DOWN).rgb() & 0xFFFFFF}
    return sum(
        1
        for y in range(0, image.height(), 2)
        for x in range(0, image.width(), 2)
        if image.pixel(x, y) & 0xFFFFFF in colors
    )


def _chart(qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> MainWindow:
    db_path = tmp_path / "test.db"
    _seed_alpaca(db_path, Timeframe.MIN30)
    window = _alpaca_window(qtbot, db_path, monkeypatch, _FlooredSource(_FLOOR))
    # Only the explicit older load in the merge test; no automatic chain.
    monkeypatch.setattr(window._chart, "older_history_bars_wanted", lambda: 0)
    _show(qtbot, window, Timeframe.MIN30)
    window.resize(1400, 900)
    window.show()
    assert _candle_pixels(qtbot, window) > 0
    return window


def test_candles_stay_drawn_after_a_large_view_jump(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _chart(qtbot, tmp_path, monkeypatch)
    viewbox: Any = window._chart.plot_manager.price_viewbox()

    viewbox.translateBy(x=-800)

    assert _candle_pixels(qtbot, window) > 0


def test_candles_stay_drawn_after_jumping_to_the_oldest_bars(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _chart(qtbot, tmp_path, monkeypatch)
    viewbox: Any = window._chart.plot_manager.price_viewbox()

    viewbox.setXRange(-20, 180, padding=0)

    assert _candle_pixels(qtbot, window) > 0


def test_candles_stay_drawn_after_older_history_is_merged(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _chart(qtbot, tmp_path, monkeypatch)
    viewbox: Any = window._chart.plot_manager.price_viewbox()
    viewbox.setXRange(-20, 180, padding=0)
    qtbot.wait(100)

    _older(qtbot, window)

    assert _candle_pixels(qtbot, window) > 0
