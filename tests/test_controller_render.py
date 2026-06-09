from typing import Any, cast

from app.controller import MainWindow
from app.state import ChartExtensionState
from simplechart.api import VerticalLineRender


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
