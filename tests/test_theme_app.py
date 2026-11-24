"""
Theme wiring: the saved theme reaches the application stylesheet, palette and
chart at MainWindow startup, the Settings dropdown saves the choice, and a
broken user theme is announced instead of crashing (docs/agentic-projects/
theming.md). Uses the live-refresh window harness.
"""

from pathlib import Path
from typing import Any

import finplot as fplt
import pytest
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QComboBox

import app.controller as controller_module
from app.application_settings import ApplicationSettingsDialog
from app.controller import MainWindow
from app.theme import active_colors, app_stylesheet, load_themes
from data.cache import Cache
from data.models import Timeframe
from data.provider import ProviderAvailability
from data.provider.credentials import UnavailableCredentialStore
from simplechart.extensions._guard import take_notices
from test_basis_change import (  # noqa: F401  (fixture)
    _isolated_finplot_windows,
    _show,
    _window,
)
from test_live_refresh import _LiveSource
from provider_support import startup_registry

_BROKEN_THEME = 'name = "Broken"\n[colors]\nnope = "#000000"\n'
_MINE = 'name = "Mine"\ninherits = "dark"\n[colors]\nbackground = "#ff00ff"\n'


def _window_with_theme(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    saved: str | None,
) -> MainWindow:
    db_path = tmp_path / "test.db"
    if saved is not None:
        with Cache(str(db_path), registry=startup_registry()) as cache:
            cache.set_theme(saved)
    return _window(qtbot, db_path, monkeypatch, _LiveSource())


def test_theme_setting_defaults_to_light_and_round_trips(tmp_path: Path) -> None:
    with Cache(str(tmp_path / "test.db"), registry=startup_registry()) as cache:
        assert cache.get_theme() == "Light"
        cache.set_theme("Dark")
        assert cache.get_theme() == "Dark"


def test_both_builtin_themes_render_the_app_stylesheet() -> None:
    themes, notices = load_themes(Path("/nonexistent-themes"))
    assert notices == []
    assert set(themes) == {"Light", "Dark"}
    for theme in themes.values():
        assert "$" not in app_stylesheet(theme)


def test_saved_dark_theme_reaches_the_stylesheet_palette_and_chart(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    app.setStyleSheet("")  # a stylesheet hides the base style's name
    original_style = app.style().objectName()
    app.setStyle("Windows")  # offscreen already defaults to fusion
    try:
        window = _window_with_theme(qtbot, tmp_path, monkeypatch, "Dark")
        _show(qtbot, window, Timeframe.DAILY)
        assert active_colors()["background"] == "#16181d"
        assert active_colors()["success"] in window._data_source_status._source_dot.styleSheet()
        assert "#16181d" in app.styleSheet()
        app.setStyleSheet("")  # a stylesheet hides the base style's name
        assert app.style().objectName() == "fusion"
    finally:
        app.setStyleSheet("")
        app.setStyle(original_style)
    app.setStyleSheet("")  # a stylesheet hides the base style's name
    assert app.style().objectName() == "fusion"
    assert app.palette().color(QPalette.ColorRole.Window) == QColor("#16181d")
    assert app.palette().color(QPalette.ColorRole.WindowText) == QColor("#d1d4dc")
    assert app.palette().color(QPalette.ColorRole.Mid) == QColor("#3a3f4b")
    assert app.palette().color(QPalette.ColorRole.Link) == QColor(
        active_colors()["primary_lighten_2"]
    )
    chart: Any = window._chart
    assert chart.plot_manager._colors.background == "#16181d"
    assert fplt.background == "#16181d"
    assert chart.plot_manager._last_price_line.opts["pen"].color().name() == "#2962ff"
    assert chart.plot_manager._candle_plot.colors["bear_body"] == "#f23645"
    assert chart.plot_manager._candle_plot.colors["bull_body"] == "#16181d"
    assert chart.plot_manager._volume_plot.colors["bull_body"] == "#0bb37a66"


def test_light_keeps_the_platform_style_and_palette(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    app.setStyleSheet("")  # a stylesheet hides the base style's name
    style, palette = app.style().objectName(), app.palette()

    window = _window_with_theme(qtbot, tmp_path, monkeypatch, None)

    assert "#ffffff" in app.styleSheet()
    app.setStyleSheet("")
    assert app.style().objectName() == style
    assert app.palette() == palette
    assert window._chart.plot_manager._colors.background == "#ffffff"


def test_a_missing_saved_theme_falls_back_to_light_with_a_notice(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window_with_theme(qtbot, tmp_path, monkeypatch, "Gone")

    assert window._chart.plot_manager._colors.background == "#ffffff"
    assert any('"Gone"' in notice for notice in take_notices())


def test_a_broken_user_theme_is_announced_and_does_not_stop_startup(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    themes_dir = tmp_path / "themes"
    themes_dir.mkdir()
    (themes_dir / "broken.toml").write_text(_BROKEN_THEME, encoding="utf-8")
    (themes_dir / "mine.toml").write_text(_MINE, encoding="utf-8")
    monkeypatch.setattr(controller_module, "USER_THEMES_DIR", themes_dir)

    window = _window_with_theme(qtbot, tmp_path, monkeypatch, "Mine")

    notices = take_notices()
    assert len(notices) == 1
    assert "broken.toml" in notices[0]
    assert "unknown color key" in notices[0]
    assert "#ff00ff" in QApplication.instance().styleSheet()  # type: ignore[union-attr]
    assert window._theme_names == ["Light", "Dark", "Mine"]


def _settings_dialog(cache: Cache, names: list[str]) -> ApplicationSettingsDialog:
    return ApplicationSettingsDialog(
        cache,
        UnavailableCredentialStore("test"),
        {
            "yfinance": ProviderAvailability(True, None),
            "alpaca": ProviderAvailability(True, None),
        },
        theme_names=names,
    )


def test_settings_theme_dropdown_lists_themes_and_ok_saves_the_choice(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    themes_dir = tmp_path / "themes"
    themes_dir.mkdir()
    (themes_dir / "mine.toml").write_text(_MINE, encoding="utf-8")
    names = list(load_themes(themes_dir)[0])
    with Cache(str(tmp_path / "test.db"), registry=startup_registry()) as cache:
        dialog = _settings_dialog(cache, names)
        qtbot.addWidget(dialog)
        combo = dialog.findChild(QComboBox, "theme")
        assert combo is not None
        assert [combo.itemText(i) for i in range(combo.count())] == ["Light", "Dark", "Mine"]
        assert combo.currentText() == "Light"

        combo.setCurrentText("Mine")
        dialog.accept()

        assert cache.get_theme() == "Mine"
        reopened = _settings_dialog(cache, names)
        qtbot.addWidget(reopened)
        reopened_combo = reopened.findChild(QComboBox, "theme")
        assert reopened_combo is not None
        assert reopened_combo.currentText() == "Mine"


def test_settings_cancel_does_not_save_the_theme(qtbot: Any, tmp_path: Path) -> None:
    with Cache(str(tmp_path / "test.db"), registry=startup_registry()) as cache:
        dialog = _settings_dialog(cache, ["Light", "Dark"])
        qtbot.addWidget(dialog)
        combo = dialog.findChild(QComboBox, "theme")
        assert combo is not None

        combo.setCurrentText("Dark")
        dialog.reject()

        assert cache.get_theme() == "Light"


def test_settings_ok_keeps_a_saved_theme_missing_from_the_list(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    with Cache(str(tmp_path / "test.db"), registry=startup_registry()) as cache:
        cache.set_theme("Missing")
        dialog = ApplicationSettingsDialog(
            cache,
            UnavailableCredentialStore("unavailable"),
            {"yfinance": ProviderAvailability(True, None)},
        )
        qtbot.addWidget(dialog)

        dialog.accept()

        assert cache.get_theme() == "Missing"
