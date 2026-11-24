from typing import Any
from unittest.mock import Mock

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QToolButton

from app.data_source_status import DataSourceStatus
from app.symbol_bar import SymbolBar
from data.aggregator import Aggregator


def test_settings_button_is_aligned_with_timeframe_buttons(qtbot: Any) -> None:
    opened: list[bool] = []
    symbol_bar = SymbolBar(Mock(spec=Aggregator))
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

    symbol_bar.hide()
    QApplication.processEvents()


def test_data_source_indicator_reports_request_health(qtbot: Any) -> None:
    source = DataSourceStatus()
    qtbot.addWidget(source)
    label = source.findChild(QLabel, "dataSourceStatusLabel")
    dot = source.findChild(QLabel, "dataSourceStatusDot")

    assert label is not None
    assert dot is not None

    source.set_data_source("Alpaca Paper / IEX", "connected")

    assert label.text() == "Alpaca Paper / IEX"
    assert "Last data request succeeded" in label.toolTip()
    assert "#198754" in dot.styleSheet()

    source.set_data_source("Yahoo Finance", "error")

    assert label.text() == "Yahoo Finance"
    assert "Last data request failed" in label.toolTip()
    assert "#c43d3d" in dot.styleSheet()
