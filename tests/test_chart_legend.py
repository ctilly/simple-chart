from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QFrame, QLabel, QToolButton, QWidget

from chart.legend import ChartLegend


def test_chart_legend_toggles_drawing_tool_palette(qtbot) -> None:  # type: ignore[no-untyped-def]
    selected: list[str] = []
    legend = ChartLegend(
        on_toggle=lambda _: None,
        on_configure=lambda _: None,
        on_remove=lambda _: None,
        on_add=lambda: None,
        on_drawing_tool=selected.append,
        drawing_tools=[("trendline", "Trend Line", None)],
    )
    qtbot.addWidget(legend)
    legend.show()
    qtbot.waitExposed(legend)

    tool_button = next(
        button for button in legend.findChildren(QToolButton)
        if button.text() == "Tools"
    )
    palette = legend.findChild(QWidget, "drawingToolPalette")
    assert palette is not None
    palette_button = palette.findChild(QToolButton, "drawingToolButton_trendline")
    assert palette_button is not None
    close_button = palette.findChild(QToolButton, "drawingToolPaletteClose")
    assert close_button is not None
    title = palette.findChild(QLabel, "drawingToolPaletteTitle")
    assert title is not None
    title_bar = palette.findChild(QFrame, "drawingToolPaletteTitleBar")
    assert title_bar is not None
    tool_row = palette.findChild(QFrame, "drawingToolPaletteToolRow")
    assert tool_row is not None

    assert not palette_button.isVisible()

    qtbot.mouseClick(tool_button, Qt.MouseButton.LeftButton)
    assert palette_button.isVisible()
    assert not palette.isWindow()
    assert palette_button.toolTip() == "Trend Line"
    assert palette.width() > palette.height()
    assert title_bar.y() < tool_row.y()
    assert close_button.mapTo(palette, QPoint(0, 0)).y() < palette_button.mapTo(
        palette,
        QPoint(0, 0),
    ).y()
    assert palette.pos() == _expected_palette_position(legend, tool_button, palette)

    qtbot.mouseClick(palette_button, Qt.MouseButton.LeftButton)
    assert selected == ["trendline"]

    original_pos = palette.pos()
    qtbot.mousePress(title_bar, Qt.MouseButton.LeftButton, pos=QPoint(8, 8))
    qtbot.mouseMove(title_bar, pos=QPoint(36, 18))
    qtbot.mouseRelease(title_bar, Qt.MouseButton.LeftButton, pos=QPoint(36, 18))
    assert palette.pos() != original_pos

    qtbot.mouseClick(tool_button, Qt.MouseButton.LeftButton)
    assert not palette_button.isVisible()

    palette.move(0, 0)
    qtbot.mouseClick(tool_button, Qt.MouseButton.LeftButton)
    assert palette.pos() == _expected_palette_position(legend, tool_button, palette)

    qtbot.mouseClick(close_button, Qt.MouseButton.LeftButton)
    assert not palette_button.isVisible()


def _expected_palette_position(
    legend: ChartLegend,
    tool_button: QToolButton,
    palette: QWidget,
) -> QPoint:
    global_x = tool_button.mapToGlobal(QPoint(-palette.width() - 12, 0)).x()
    global_y = legend.mapToGlobal(QPoint(0, legend.height())).y() - (palette.height() // 4)
    parent = palette.parentWidget()
    assert parent is not None
    return parent.mapFromGlobal(QPoint(global_x, global_y))
