from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QToolButton, QWidget

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

    symbol_bar.hide()
    QApplication.processEvents()


def test_data_source_indicator_reports_request_health(qtbot: Any) -> None:
    symbol_bar = SymbolBar()
    qtbot.addWidget(symbol_bar)
    label = symbol_bar.findChild(QLabel, "dataSourceStatusLabel")
    dot = symbol_bar.findChild(QLabel, "dataSourceStatusDot")

    assert label is not None
    assert dot is not None

    symbol_bar.set_data_source("Alpaca Paper / IEX", "connected")

    assert label.text() == "Alpaca Paper / IEX"
    assert "Last data request succeeded" in label.toolTip()
    assert "#198754" in dot.styleSheet()

    symbol_bar.set_data_source("Yahoo Finance", "error")

    assert label.text() == "Yahoo Finance"
    assert "Last data request failed" in label.toolTip()
    assert "#c43d3d" in dot.styleSheet()


def test_data_source_cluster_keeps_label_visible_and_status_adjacent(
    qtbot: Any,
) -> None:
    symbol_bar = SymbolBar()
    symbol_bar.resize(800, 32)
    qtbot.addWidget(symbol_bar)
    symbol_bar.show()
    qtbot.waitExposed(symbol_bar)

    cluster = symbol_bar.findChild(QWidget, "dataSourceStatus")
    label = symbol_bar.findChild(QLabel, "dataSourceStatusLabel")
    dot = symbol_bar.findChild(QLabel, "dataSourceStatusDot")
    settings = symbol_bar.findChild(QToolButton, "applicationSettingsButton")

    assert cluster is not None
    assert label is not None
    assert dot is not None
    assert settings is not None

    for source_name in (
        "Alpaca Paper / SIP (15-minute delayed)",
        "Yahoo Finance",
    ):
        for bar_width in (800, 1200):
            symbol_bar.resize(bar_width, 32)
            symbol_bar.set_data_source(source_name, "connected")
            QApplication.processEvents()

            dot_right = dot.mapTo(symbol_bar, dot.rect().topRight()).x()
            label_left = label.mapTo(symbol_bar, label.rect().topLeft()).x()
            label_right = label.mapTo(symbol_bar, label.rect().topRight()).x()

            assert label.text() == source_name
            assert label.width() >= label.sizeHint().width()
            assert label_left - dot_right - 1 == 4
            assert settings.geometry().left() - label_right - 1 >= 12
            assert settings.geometry().right() < symbol_bar.width()

    symbol_bar.hide()
    QApplication.processEvents()
