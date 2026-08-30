from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QPushButton, QToolButton

from app.symbol_bar import SymbolBar


def test_settings_button_is_aligned_with_timeframe_buttons(qtbot: Any) -> None:
    opened: list[bool] = []
    symbol_bar = SymbolBar()
    symbol_bar.settings_requested.connect(lambda: opened.append(True))
    qtbot.addWidget(symbol_bar)
    symbol_bar.show()
    qtbot.waitExposed(symbol_bar)

    settings = symbol_bar.findChild(QToolButton, "applicationSettingsButton")
    daily = next(
        button
        for button in symbol_bar.findChildren(QPushButton)
        if button.text() == "D"
    )

    assert settings is not None
    assert abs(settings.geometry().center().y() - daily.geometry().center().y()) <= 1
    qtbot.mouseClick(settings, Qt.MouseButton.LeftButton)
    assert opened == [True]


def test_data_source_indicator_reports_request_health(qtbot: Any) -> None:
    symbol_bar = SymbolBar()
    qtbot.addWidget(symbol_bar)
    label = symbol_bar.findChild(QLabel, "dataSourceStatusLabel")
    dot = symbol_bar.findChild(QLabel, "dataSourceStatusDot")

    assert label is not None
    assert dot is not None
    reserved_width = label.width()

    symbol_bar.set_data_source("Alpaca Paper / IEX", "connected")

    assert label.text() == "Alpaca Paper / IEX"
    assert label.width() == reserved_width
    assert "Last data request succeeded" in label.toolTip()
    assert "#198754" in dot.styleSheet()

    symbol_bar.set_data_source("Yahoo Finance", "error")

    assert label.text() == "Yahoo Finance"
    assert "Last data request failed" in label.toolTip()
    assert "#c43d3d" in dot.styleSheet()
