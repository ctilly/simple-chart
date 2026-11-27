from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PyQt6.QtGui import QAction, QColor
from PyQt6.QtWidgets import QCheckBox, QColorDialog, QDialog, QMenu, QSpinBox, QWidget

import app.controller as controller
from app.controller import MainWindow
from app.extension_config import ColorButton, ExtensionConfigDialog
from data.models import Timeframe
from simplechart.api import ChartExtension, ChartExtensionRender, OHLCVSeries, get_extension
from simplechart.extensions import _registry
from test_basis_change import (
    _AdjustedSource,
    _SYMBOL,
    _YAHOO,
    _at,
    _isolated_finplot_windows,  # noqa: F401 (fixture)
    _window,
)
from test_day_based_indicators_integration import _daily, _price_bar


@pytest.fixture
def window(qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MainWindow:
    return _window(qtbot, tmp_path / "chart.db", monkeypatch, _AdjustedSource(None))


@pytest.fixture
def messages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    shown: list[str] = []

    def show_message(_parent: QWidget | None, _title: str, message: str) -> None:
        shown.append(message)

    monkeypatch.setattr(controller, "show_information", show_message)
    return shown


def _select_add_menu_item(monkeypatch: pytest.MonkeyPatch, label: str) -> None:
    def select(menu: QMenu, *_args: object) -> QAction:
        return next(action for action in menu.actions() if action.text() == label)

    monkeypatch.setattr(QMenu, "exec", select)


def _render(window: MainWindow, series: OHLCVSeries) -> None:
    window._state.symbol = series.symbol
    window._state.timeframe = series.timeframe
    window._current_series = series
    window._render(series)


def _render_daily(window: MainWindow) -> None:
    _render(window, OHLCVSeries(_SYMBOL, Timeframe.DAILY, [
        _daily(date(2025, 11, 25), 10), _daily(date(2025, 11, 26), 20),
    ]))


@pytest.mark.parametrize("name", ["sma", "ema"])
def test_main_window_rejects_duplicate_add_even_with_smoothing_enabled(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch, messages: list[str], name: str,
) -> None:
    _render_daily(window)
    _select_add_menu_item(monkeypatch, get_extension(name).label())

    def accept(dialog: ExtensionConfigDialog) -> QDialog.DialogCode:
        assert list(dialog.result_params())[-2:] == ["line_style", "smooth_intraday"]
        checkbox = dialog.findChild(QCheckBox)
        assert checkbox is not None and not checkbox.isChecked()
        checkbox.setChecked(bool(window._state.extensions))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ExtensionConfigDialog, "exec", accept)
    window._on_add_extension()
    (existing,) = window._state.extensions
    key = f"{name}_{existing.params['days']}"
    label = f"{name.upper()} {existing.params['days']}"
    legend_entry = window._chart.legend._labels[key]
    plot = window._chart.plot_manager._plots[key]
    params = existing.params.copy()

    window._on_add_extension()

    assert window._state.extensions == [existing]
    assert existing.params == params
    assert window._chart.legend._labels == {key: legend_entry}
    assert window._chart.plot_manager._plots == {key: plot}
    assert messages == [
        f"{label} is already on this chart. Use Configure on the existing line "
        "to change its color or style.",
    ]


def test_main_window_rejects_configure_into_another_lines_key_and_accepts_color_edit(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch, messages: list[str],
) -> None:
    _render_daily(window)
    _select_add_menu_item(monkeypatch, get_extension("sma").label())
    days = 50

    def change_days(dialog: ExtensionConfigDialog) -> QDialog.DialogCode:
        field = dialog.findChild(QSpinBox)
        assert field is not None
        field.setValue(days)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ExtensionConfigDialog, "exec", change_days)
    window._on_add_extension()
    days = 200
    window._on_add_extension()
    first, second = window._state.extensions
    first_params, second_params = first.params.copy(), second.params.copy()
    labels = window._chart.legend._labels.copy()

    window._on_extension_configure("sma_50")

    assert window._state.extensions == [first, second]
    assert first.params == first_params and second.params == second_params
    assert window._chart.legend._labels == labels
    assert len(messages) == 1 and messages[0].startswith("SMA 200 is already on this chart.")

    def pick_color(*_args: object) -> QColor:
        return QColor("#123456")

    def change_color(dialog: ExtensionConfigDialog) -> QDialog.DialogCode:
        field = dialog.findChild(ColorButton)
        assert field is not None
        field.click()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QColorDialog, "getColor", pick_color)
    monkeypatch.setattr(ExtensionConfigDialog, "exec", change_color)
    window._on_extension_configure("sma_50")

    assert first.params == first_params | {"color": "#123456"}
    assert second.params == second_params
    assert window._chart.legend._labels == labels
    assert len(messages) == 1


class DailyReferenceIndicator(ChartExtension):
    def name(self) -> str:
        return "daily_reference_fixture"

    def label(self) -> str:
        return "Daily Reference Fixture"

    def default_params(self) -> dict[str, Any]:
        return {"variant": 1}

    def compute(self, series: OHLCVSeries, params: dict[str, Any]) -> dict[str, np.ndarray]:
        values = np.full(len(series.bars), 10.0)
        result = {f"variant_{params['variant']}": values}
        if params.get("_daily_bars"):
            result["daily_reference_fixture_line"] = values
        return result

    def render(self, series: OHLCVSeries, params: dict[str, Any]) -> ChartExtensionRender:
        render = super().render(series, params)
        for line in render.series:
            if line.key == "daily_reference_fixture_line":
                line.label = "Prior Session Reference"
        return render


def test_main_window_checks_every_computed_key_with_runtime_inputs(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch, messages: list[str],
) -> None:
    fixture = DailyReferenceIndicator()
    monkeypatch.setitem(_registry._registry, fixture.name(), DailyReferenceIndicator)
    window._cache.put_bars(_YAHOO, _SYMBOL, Timeframe.DAILY, [_daily(date(2025, 11, 25), 10)])
    _render(window, OHLCVSeries(_SYMBOL, Timeframe.MIN30, [
        _price_bar(_at(date(2025, 11, 26)), 20),
    ]))
    _select_add_menu_item(monkeypatch, fixture.label())
    variant = 1

    def accept(dialog: ExtensionConfigDialog) -> QDialog.DialogCode:
        field = dialog.findChild(QSpinBox)
        assert field is not None
        field.setValue(variant)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ExtensionConfigDialog, "exec", accept)
    window._on_add_extension()
    variant = 2
    window._on_add_extension()

    (existing,) = window._state.extensions
    assert existing.params == {"variant": 1}
    assert set(window._chart.legend._labels) == {"variant_1", "daily_reference_fixture_line"}
    assert len(messages) == 1 and messages[0].startswith(
        "Prior Session Reference is already on this chart.",
    )


def test_main_window_allows_add_with_no_series(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch, messages: list[str],
) -> None:
    _select_add_menu_item(monkeypatch, get_extension("sma").label())

    def accept(_dialog: ExtensionConfigDialog) -> QDialog.DialogCode:
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ExtensionConfigDialog, "exec", accept)
    assert window._current_series is None
    window._on_add_extension()

    (added,) = window._state.extensions
    assert added.name == "sma"
    assert messages == []
