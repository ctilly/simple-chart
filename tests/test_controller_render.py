from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import finplot as fplt
import pytest
from pytestqt.qtbot import QtBot

from app.controller import MainWindow
from app.state import ChartExtensionState
from data.models import Bar, OHLCVSeries, Timeframe
from data.provider.registry import ProviderRegistry
from simplechart.api import VerticalLineRender


def test_main_window_renders_hollow_red_candle(
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_registry: ProviderRegistry,
    isolated_credentials: None,
) -> None:
    def skip_startup_request(_window: MainWindow) -> None:
        pass

    monkeypatch.setattr(fplt, "windows", [])
    monkeypatch.setattr(fplt, "master_data", {})
    monkeypatch.setattr(fplt, "last_ax", None)
    monkeypatch.setattr(MainWindow, "_load", skip_startup_request)
    monkeypatch.setattr(MainWindow, "_refresh_watchlist_snapshots", skip_startup_request)
    window = MainWindow(str(tmp_path / "test.db"), provider_registry=provider_registry)
    qtbot.addWidget(window)

    window._render(OHLCVSeries("TSLA", Timeframe.DAILY, [
        Bar(datetime(2026, 11, 24, tzinfo=timezone.utc), 380.0, 386.0, 378.0, 384.96, 1_000),
        Bar(datetime(2026, 11, 25, tzinfo=timezone.utc), 370.51, 382.0, 369.0, 380.20, 1_000),
        Bar(datetime(2026, 11, 27, tzinfo=timezone.utc), 386.0, 388.0, 381.0, 383.0, 1_000),
    ]))

    manager = window._chart.plot_manager
    item = manager._candle_plot
    assert isinstance(item, fplt.CandlestickItem)
    source = item.datasrc
    rows, _ = source.rows(5, 1, 2, item.ax.vb.yscale, resamp=item.resamp)
    color_groups = list(item.colorfunc(item, source, rows))
    assert [
        (frame, body)
        for _, frame, body, candles in color_groups
        if 1 in candles.index
    ] == [(manager._colors.candle_down, manager._colors.hollow_body)]
    assert [
        (frame, body)
        for _, frame, body, candles in color_groups
        if 2 in candles.index
    ] == [(manager._colors.candle_up, manager._colors.candle_up)]


def test_controller_adds_vertical_line_render_to_legend() -> None:
    plot_manager = _RecordingPlotManager()
    legend = _RecordingLegend()
    controller = _ControllerStub(plot_manager, legend)
    line = VerticalLineRender(
        key="example_vertical_line",
        x_index=3.0,
        label="Example Vertical Line",
        color="#800080",
        line_width=1.0,
        line_style="dash",
    )

    draw_vertical_line = cast(Any, MainWindow._draw_vertical_line)
    draw_vertical_line(
        controller,
        line,
        ChartExtensionState(name="example", params={}),
    )

    assert plot_manager.lines == [line]
    assert legend.added == [("example_vertical_line", "Example Vertical Line", "#800080")]
    assert legend.colors == [("example_vertical_line", "#800080")]
    assert legend.visibility == [("example_vertical_line", True)]


def test_controller_skips_vertical_line_legend_during_preview() -> None:
    plot_manager = _RecordingPlotManager()
    legend = _RecordingLegend()
    controller = _ControllerStub(plot_manager, legend)
    line = VerticalLineRender(
        key="example_vertical_line",
        x_index=3.0,
        label="Example Vertical Line",
        color="#800080",
        line_width=1.0,
        line_style="dash",
    )

    draw_vertical_line = cast(Any, MainWindow._draw_vertical_line)
    draw_vertical_line(
        controller,
        line,
        ChartExtensionState(name="example", params={}),
        False,
    )

    assert plot_manager.lines == [line]
    assert legend.added == []
    assert legend.colors == []
    assert legend.visibility == []


class _ControllerStub:

    def __init__(
        self,
        plot_manager: "_RecordingPlotManager",
        legend: "_RecordingLegend",
    ) -> None:
        self._chart = _ChartStub(plot_manager, legend)


class _ChartStub:

    def __init__(
        self,
        plot_manager: "_RecordingPlotManager",
        legend: "_RecordingLegend",
    ) -> None:
        self.plot_manager = plot_manager
        self.legend = legend


class _RecordingPlotManager:

    def __init__(self) -> None:
        self.lines: list[VerticalLineRender] = []

    def update_vertical_line(self, line: VerticalLineRender) -> None:
        self.lines.append(line)


class _RecordingLegend:

    def __init__(self) -> None:
        self.added: list[tuple[str, str, str]] = []
        self.colors: list[tuple[str, str]] = []
        self.visibility: list[tuple[str, bool]] = []

    def add_extension(self, key: str, label: str, color: str) -> None:
        self.added.append((key, label, color))

    def update_color(self, key: str, color: str) -> None:
        self.colors.append((key, color))

    def set_extension_visible(self, key: str, visible: bool) -> None:
        self.visibility.append((key, visible))
