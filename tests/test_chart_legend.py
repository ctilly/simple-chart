from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QPushButton, QToolButton

from chart.legend import ChartLegend


def test_chart_legend_toggles_drawing_tool_palette(qtbot) -> None:  # type: ignore[no-untyped-def]
    selected: list[str] = []
    legend = ChartLegend(
        on_toggle=lambda _: None,
        on_configure=lambda _: None,
        on_remove=lambda _: None,
        on_add=lambda: None,
        on_drawing_tool=selected.append,
        drawing_tools=[("trendline", "Trend Line")],
    )
    qtbot.addWidget(legend)
    legend.show()
    qtbot.waitExposed(legend)

    tool_button = next(
        button for button in legend.findChildren(QToolButton)
        if button.text() == "Tools"
    )
    palette_button = next(
        button for button in legend.findChildren(QPushButton)
        if button.text() == "Trend Line"
    )

    assert not palette_button.isVisible()

    qtbot.mouseClick(tool_button, Qt.MouseButton.LeftButton)
    assert palette_button.isVisible()

    qtbot.mouseClick(palette_button, Qt.MouseButton.LeftButton)
    assert selected == ["trendline"]

    qtbot.mouseClick(tool_button, Qt.MouseButton.LeftButton)
    assert not palette_button.isVisible()
