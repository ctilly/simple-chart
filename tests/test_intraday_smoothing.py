from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Self

import numpy as np
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QCheckBox, QDialog, QFormLayout, QLabel, QMenu, QSpinBox

import indicators._day_based as day_based
from app.extension_config import ExtensionConfigDialog
from app.state import ChartExtensionState
from data.models import Timeframe
from simplechart.api import OHLCVSeries, get_extension
from test_basis_change import (
    _AdjustedSource,
    _FixedDateTime,
    _SYMBOL,
    _YAHOO,
    _at,
    _isolated_finplot_windows,  # noqa: F401 (fixture)
    _window,
)
from test_day_based_indicators_integration import (
    _capture_plots,
    _daily,
    _price_bar,
)

_FULL_DAY = date(2025, 11, 26)
_HALF_DAY = date(2025, 11, 28)
_NEXT_DAY = date(2025, 12, 1)


@pytest.mark.parametrize(("session_date", "minutes"), [
    (date(2026, 11, 27), 210),
    (date(2026, 12, 24), 210),
    (date(2025, 7, 3), 210),
    (date(2026, 7, 3), 390),
    (_FULL_DAY, 390),
])
def test_regular_session_minutes(session_date: date, minutes: int) -> None:
    assert day_based._regular_session_minutes(session_date) == minutes


@pytest.mark.parametrize("name", ["sma", "ema"])
@pytest.mark.parametrize(("timeframe", "half_day_proxy"), [
    (Timeframe.MIN5, 210), (Timeframe.MIN15, 210), (Timeframe.MIN30, 210),
    (Timeframe.MIN39, 234), (Timeframe.MIN65, 260),
])
def test_smoothing_uses_session_elapsed_fraction_and_previous_daily_value(
    name: str, timeframe: Timeframe, half_day_proxy: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SessionClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(_at(_NEXT_DAY, 12).timestamp(), tz=tz)

    monkeypatch.setattr(day_based, "datetime", SessionClock)
    sessions = [date(2025, 11, 24), date(2025, 11, 25), _FULL_DAY, _HALF_DAY]
    daily = [_daily(day, close) for day, close in zip(sessions, [10, 14, 20, 32], strict=True)]
    minutes = timeframe.minutes
    assert minutes is not None
    bars = [
        _price_bar(_at(sessions[1]), 16),
        _price_bar(_at(_FULL_DAY), 34),
        _price_bar(_at(_FULL_DAY) + timedelta(minutes=390 - minutes), 20),
        _price_bar(_at(_HALF_DAY), 44),
        _price_bar(_at(_HALF_DAY) + timedelta(minutes=minutes), 38),
        _price_bar(_at(_HALF_DAY) + timedelta(minutes=half_day_proxy - minutes), 32),
        _price_bar(_at(_NEXT_DAY), 51),
        _price_bar(_at(_NEXT_DAY) + timedelta(minutes=minutes), np.nan),
    ]
    series = OHLCVSeries(_SYMBOL, timeframe, bars)
    indicator = get_extension(name)
    key = f"{name}_2"
    params = {"days": 2, "_daily_bars": daily}
    live = indicator.compute(series, params)[key]
    assert indicator.default_params().get("smooth_intraday") is False
    np.testing.assert_array_equal(
        indicator.compute(series, params | {"smooth_intraday": False})[key], live,
    )
    np.testing.assert_allclose(live, (
        [13, 24, 17, 32, 29, 26, 41.5, np.nan] if name == "sma"
        else [13, 80 / 3, 52 / 3, 316 / 9, 280 / 9, 244 / 9, 1162 / 27, np.nan]
    ))

    smooth_params = params | {"smooth_intraday": True}
    smoothed = indicator.compute(series, smooth_params)[key]
    daily_series = OHLCVSeries(_SYMBOL, Timeframe.DAILY, daily)
    daily_values = indicator.compute(daily_series, params)[key]
    anchors = daily_values[[0, 1, 1, 2, 2, 2, 3, 3]]
    fractions = np.array([
        minutes / 390, minutes / 390, 1,
        minutes / half_day_proxy, 2 * minutes / half_day_proxy, 1,
        minutes / 390, 2 * minutes / 390,
    ])
    np.testing.assert_allclose(smoothed, anchors + fractions * (live - anchors))
    np.testing.assert_allclose(smoothed[[2, 5]], daily_values[[2, 3]], rtol=1e-13)
    assert np.isnan(smoothed[[0, -1]]).all()

    # A historical half day is completed even if it is the last day loaded.
    ended_on_half_day = OHLCVSeries(_SYMBOL, timeframe, bars[:-2])
    np.testing.assert_array_equal(
        indicator.compute(ended_on_half_day, smooth_params)[key], smoothed[:-2],
    )


@pytest.mark.parametrize("name", ["sma", "ema"])
@pytest.mark.parametrize("timeframe", [Timeframe.DAILY, Timeframe.WEEKLY])
def test_smoothing_keeps_key_but_does_not_change_daily_or_weekly_values(
    name: str, timeframe: Timeframe,
) -> None:
    daily = [_daily(date(2025, 11, 17) + timedelta(days=i), 10 + i) for i in range(5)]
    series = OHLCVSeries(_SYMBOL, timeframe, daily if timeframe == Timeframe.DAILY else daily[:1])
    indicator = get_extension(name)
    params = {"days": 2, "_daily_bars": daily}
    key = f"{name}_2"
    regular = indicator.render(series, params).series
    smoothed = indicator.render(series, params | {"smooth_intraday": True}).series
    assert [(line.key, line.label) for line in regular] == [(key, f"{name.upper()} 2")]
    assert [(line.key, line.label) for line in smoothed] == [(key, f"{name.upper()} 2")]
    np.testing.assert_array_equal(smoothed[0].values, regular[0].values)


def test_main_window_smoothing_reaches_daily_value_at_current_early_close(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path / "chart.db", monkeypatch, _AdjustedSource(None))
    early_close = date(2026, 11, 27)
    monkeypatch.setattr(_FixedDateTime, "frozen", _at(early_close, 13))
    monkeypatch.setattr(day_based, "datetime", _FixedDateTime)
    window._state.extensions = [ChartExtensionState("sma", {"days": 2, "smooth_intraday": True})]
    window._state.symbol = _SYMBOL
    window._state.timeframe = Timeframe.MIN15
    daily = [_daily(date(2026, 11, 24), 10), _daily(date(2026, 11, 25), 14)]
    window._cache.put_bars(_YAHOO, _SYMBOL, Timeframe.DAILY, daily)
    series = OHLCVSeries(_SYMBOL, Timeframe.MIN15, [
        _price_bar(_at(early_close), 22), _price_bar(_at(early_close, 12, 45), 32),
    ])
    plots = _capture_plots(window, monkeypatch)
    window._render(series)

    first, last = plots["sma_2"]
    assert first == pytest.approx(12 + 6 * 15 / 210)
    daily_series = OHLCVSeries(_SYMBOL, Timeframe.DAILY, [*daily, _daily(early_close, 32)])
    assert last == get_extension("sma").compute(daily_series, {"days": 2})["sma_2"][-1]


def test_main_window_configures_smoothing_on_the_existing_sma_line(
    qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path / "chart.db", monkeypatch, _AdjustedSource(None))
    sma = ChartExtensionState("sma", {"days": 50, "color": "#123456"})
    window._state.extensions = [sma]
    window._state.symbol = _SYMBOL
    window._state.timeframe = Timeframe.MIN30
    daily = [_daily(_FULL_DAY - timedelta(days=i), 10) for i in range(50, 0, -1)]
    window._cache.put_bars(_YAHOO, _SYMBOL, Timeframe.DAILY, daily)
    series = OHLCVSeries(_SYMBOL, Timeframe.MIN30, [
        _price_bar(_at(_FULL_DAY), 30), _price_bar(_at(_FULL_DAY, 15, 30), 20),
    ])
    window._current_series = series
    plotted = _capture_plots(window, monkeypatch)
    window._render(series)

    labels = window._chart.legend._labels
    chart_plots = window._chart.plot_manager._plots
    regular_plot = chart_plots["sma_50"]
    regular_label = labels["sma_50"]
    assert list(labels) == ["sma_50"] and regular_label.text() == "SMA 50"
    np.testing.assert_allclose(plotted["sma_50"], [10.4, 10.2])

    def select_action(menu: QMenu, *args: Any) -> QAction:
        return next(action for action in menu.actions() if action.text() == "Configure...")

    def edit_config(dialog: ExtensionConfigDialog) -> QDialog.DialogCode:
        form = dialog.findChild(QFormLayout)
        assert form is not None
        for row, text in [(form.rowCount() - 2, "Line Style"), (form.rowCount() - 1, "Smooth Intraday")]:
            item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            assert item is not None
            label = item.widget()
            assert isinstance(label, QLabel) and label.text() == text
        field = form.itemAt(form.rowCount() - 1, QFormLayout.ItemRole.FieldRole)
        assert field is not None
        checkbox = field.widget()
        assert isinstance(checkbox, QCheckBox) and not checkbox.isChecked()
        checkbox.setChecked(True)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QMenu, "exec", select_action)
    monkeypatch.setattr(ExtensionConfigDialog, "exec", edit_config)
    qtbot.mouseClick(regular_label, Qt.MouseButton.RightButton)
    assert window._state.extensions == [sma]
    assert sma.params["days"] == 50 and sma.params["smooth_intraday"] is True
    assert sma.params["color"] == "#123456"
    assert labels == {"sma_50": regular_label} and regular_label.text() == "SMA 50"
    assert list(chart_plots) == ["sma_50"] and chart_plots["sma_50"] is regular_plot
    np.testing.assert_allclose(plotted["sma_50"], [10 + 0.4 * 30 / 390, 10.2])

    def edit_period(dialog: ExtensionConfigDialog) -> QDialog.DialogCode:
        day_field = dialog.findChild(QSpinBox)
        checkbox = dialog.findChild(QCheckBox)
        assert day_field is not None and checkbox is not None and checkbox.isChecked()
        day_field.setValue(49 if day_field.value() == 50 else 50)
        checkbox.setChecked(day_field.value() == 49)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ExtensionConfigDialog, "exec", edit_period)
    qtbot.mouseClick(regular_label, Qt.MouseButton.RightButton)
    assert sma.params["days"] == 49 and sma.params["smooth_intraday"] is True
    assert list(labels) == ["sma_49"] and labels["sma_49"].text() == "SMA 49"
    np.testing.assert_allclose(plotted["sma_49"], [10 + (20 / 49) * 30 / 390, 10 + 10 / 49])

    qtbot.mouseClick(labels["sma_49"], Qt.MouseButton.RightButton)
    assert sma.params["days"] == 50 and sma.params["smooth_intraday"] is False
    assert list(labels) == ["sma_50"]
    assert list(chart_plots) == ["sma_50"]
    np.testing.assert_allclose(plotted["sma_50"], [10.4, 10.2])
