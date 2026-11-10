"""Plugin isolation through MainWindow, where the host call sites live.

Fault plugins are real files in a temporary plugin directory, loaded through
the real loader before the window is built (as at startup). Each case drives
the production entry point that reaches one guarded call family, then checks:
one notice, no further calls to the failed plugin, cleared interaction state,
removed render output, other extensions still rendering, re-adding and
symbol switching blocked, and no secret or exception text anywhere.
"""

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import finplot as fplt
import pytest
from PyQt6.QtWidgets import QLabel, QMenu

import app.controller as controller_module
from app.controller import MainWindow
from app.dialogs import ApplicationMessageDialog
from app.extension_config import ExtensionConfigDialog
from app.state import ChartExtensionState
from data.models import Bar, OHLCVSeries, Timeframe
from data.provider.credentials import CredentialStoreAccess, UnavailableCredentialStore
from plugin_faults import (
    arm,
    fault_plugin_source,
    isolated_plugin_state,
    plugin_module,
    write_plugin,
)
from realistic_errors import assert_secret_absent
from simplechart.extensions._guard import is_disabled
from simplechart.plugins import load_plugin_directory
from ui_text import widget_text


_FIRST = datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
_FAULT = "iso_fault"
_FAULT_LABEL = "Fault Plugin"
_FAULT_KEY = f"{_FAULT}_line"
_HEALTHY = "iso_healthy"
_HEALTHY_LABEL = "Healthy Plugin"
_HEALTHY_KEY = f"{_HEALTHY}_line"
_NOTICE_TITLE = "Extension Problem"
_FAULT_NOTICE = f"'{_FAULT_LABEL}' failed and was turned off. Details were logged."


@pytest.fixture(autouse=True)
def _isolated_finplot_windows() -> Iterator[None]:
    fplt.windows.clear()
    yield
    fplt.windows.clear()


@pytest.fixture(autouse=True)
def _isolated_plugins() -> Iterator[None]:
    with isolated_plugin_state():
        yield


def _window(qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MainWindow:
    # Never touch the host OS keyring from these tests.
    monkeypatch.setattr(
        controller_module,
        "initialize_keyring_credential_store",
        lambda: CredentialStoreAccess(
            UnavailableCredentialStore("test"), False, "test",
        ),
    )
    monkeypatch.setattr(MainWindow, "_load", lambda window: None)
    monkeypatch.setattr(MainWindow, "_refresh_watchlist_snapshots", lambda window: None)
    monkeypatch.setattr(MainWindow, "_refresh_level1", lambda window: None)
    window = MainWindow(str(tmp_path / "test.db"))
    qtbot.addWidget(window)
    return window


def _series(symbol: str, count: int = 300) -> OHLCVSeries:
    return OHLCVSeries(
        symbol,
        Timeframe.MIN5,
        [
            Bar(_FIRST + timedelta(minutes=5 * i), 100.0, 101.0, 99.0, 100.5, 1_000)
            for i in range(count)
        ],
    )


def _notice_dialogs(window: MainWindow) -> list[ApplicationMessageDialog]:
    return [
        dialog
        for dialog in window.findChildren(ApplicationMessageDialog)
        if dialog.windowTitle() == _NOTICE_TITLE and dialog.isVisible()
    ]


def _dialog_text(dialog: ApplicationMessageDialog) -> str:
    label = dialog.findChild(QLabel, "applicationMessageText")
    assert label is not None
    return label.text()


def _menu_picks(monkeypatch: pytest.MonkeyPatch, text: str | None) -> list[str]:
    # Every QMenu "shown" records its entries and selects the one named
    # `text` (or nothing).
    offered: list[str] = []

    def choose(menu: QMenu, *args: object) -> object:
        actions = menu.actions()
        offered.extend(action.text() for action in actions)
        return next((action for action in actions if action.text() == text), None)

    monkeypatch.setattr(QMenu, "exec", choose)
    return offered


def _config_dialog_answers(
    monkeypatch: pytest.MonkeyPatch,
    code: ExtensionConfigDialog.DialogCode,
) -> None:
    monkeypatch.setattr(ExtensionConfigDialog, "exec", lambda dialog: code)


@dataclass
class _Scene:
    window: MainWindow
    fault: Any
    healthy: Any
    monkeypatch: pytest.MonkeyPatch


def _scene(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_in: str = "",
    store_fail_in: str = "",
    add_mode: str = "dialog",
    hits: bool = False,
    render_target: str = "chart",
    arm_before_window: bool = False,
) -> _Scene:
    plugins = tmp_path / "plugins"
    write_plugin(
        plugins,
        "a_fault",
        fault_plugin_source(
            name=_FAULT,
            label=_FAULT_LABEL,
            fail_in=fail_in,
            store_fail_in=store_fail_in,
            add_mode=add_mode,
            hits=hits,
            render_target=render_target,
        ),
    )
    write_plugin(
        plugins,
        "b_healthy",
        fault_plugin_source(
            name=_HEALTHY,
            label=_HEALTHY_LABEL,
            add_mode="toolbar",
            clear_on_start=True,
        ),
    )
    load_plugin_directory(plugins)
    fault = plugin_module("a_fault")
    healthy = plugin_module("b_healthy")
    if arm_before_window:
        arm(fault)
    window = _window(qtbot, tmp_path, monkeypatch)
    window._state.symbol = "XBI"
    window._state.timeframe = Timeframe.MIN5
    window._on_fetch_done(_series("XBI"))
    window._state.extensions += [
        ChartExtensionState(name=_FAULT, params={"color": "#ff0000"}),
        ChartExtensionState(name=_HEALTHY, params={"color": "#00ff00"}),
    ]
    window._reload_extensions(draw_bars=False)
    if not arm_before_window:
        assert _FAULT_KEY in window._chart.plot_manager.active_series_keys()
    assert _HEALTHY_KEY in window._chart.plot_manager.active_series_keys()
    return _Scene(window, fault, healthy, monkeypatch)


# ------------------------------------------------------------------
# Production entry points, one per call family
# ------------------------------------------------------------------


def _reload(scene: _Scene) -> None:
    scene.window._reload_extensions(draw_bars=False)


def _interim_render(scene: _Scene) -> None:
    scene.window._on_fetch_done(_series("XBI"), history_complete=False)


def _chart_load_request(scene: _Scene) -> None:
    scene.window._make_chart_load_request(
        "XBI", Timeframe.MIN5, scene.window._selected_route.connection,
    )


def _switch_symbol(scene: _Scene) -> None:
    scene.window._state.symbol = "QQQ"
    scene.window._on_fetch_done(_series("QQQ"))


def _add_menu(scene: _Scene) -> None:
    _menu_picks(scene.monkeypatch, _FAULT_LABEL)
    _config_dialog_answers(scene.monkeypatch, ExtensionConfigDialog.DialogCode.Rejected)
    scene.window._on_add_extension()


def _toolbar_entries(scene: _Scene) -> None:
    scene.window._drawing_tool_entries()


def _right_click(scene: _Scene, pick: str | None) -> None:
    _menu_picks(scene.monkeypatch, pick)
    scene.window._on_bar_right_clicked(5.0, 100.0, 1.0, 1.0)


def _context_menu(scene: _Scene) -> None:
    _right_click(scene, None)


def _fault_context_action(scene: _Scene) -> None:
    _right_click(scene, f"{_FAULT_LABEL} action")


def _healthy_context_action(scene: _Scene) -> None:
    _right_click(scene, f"{_HEALTHY_LABEL} action")


def _drag_start(scene: _Scene) -> None:
    scene.window._on_drag_start(5.0, 100.0, 1.0, 1.0)


def _drag_move(scene: _Scene) -> None:
    _drag_start(scene)
    scene.window._on_drag_move(6.0, 100.0, 1.0, 1.0)


def _drag_finish(scene: _Scene) -> None:
    _drag_move(scene)
    scene.window._on_drag_finish(7.0, 100.0, 1.0, 1.0)


def _drag_cancel(scene: _Scene) -> None:
    _drag_move(scene)
    scene.window._on_drag_cancel()


def _draw_start(scene: _Scene) -> None:
    scene.window._on_drawing_tool_selected(_FAULT)
    scene.window._on_bar_clicked(5.0, 100.0, 1.0, 1.0)


def _draw_preview(scene: _Scene) -> None:
    _draw_start(scene)
    scene.window._on_mouse_move(6.0, 100.0, 1.0, 1.0)
    scene.window._flush_drawing_preview()


def _draw_advance(scene: _Scene) -> None:
    _draw_preview(scene)
    scene.window._on_bar_clicked(7.0, 100.0, 1.0, 1.0)


def _draw_commit(scene: _Scene) -> None:
    _draw_preview(scene)
    scene.window._on_commit_shortcut()


def _draw_cancel(scene: _Scene) -> None:
    _draw_preview(scene)
    scene.window._on_cancel_shortcut()


def _healthy_draw(scene: _Scene) -> None:
    scene.window._on_drawing_tool_selected(_HEALTHY)
    scene.window._on_bar_clicked(5.0, 100.0, 1.0, 1.0)


def _toggle(scene: _Scene) -> None:
    scene.window._on_extension_toggled(_FAULT_KEY)


def _configure(scene: _Scene) -> None:
    _config_dialog_answers(scene.monkeypatch, ExtensionConfigDialog.DialogCode.Accepted)
    scene.window._on_extension_configure(_FAULT_KEY)


def _remove(scene: _Scene) -> None:
    scene.window._on_extension_remove(_FAULT_KEY)


@dataclass(frozen=True)
class _Case:
    hook: str
    trigger: Callable[[_Scene], None]
    add_mode: str = "dialog"
    hits: bool = False


_EXTENSION_CASES: list[_Case] = [
    _Case("__init__", _reload),
    _Case("render", _reload),
    _Case("render_target", _reload),
    _Case("history_lookback_days", _interim_render),
    _Case("history_lookback_days", _chart_load_request),
    _Case("preserve_ui_state_per_symbol", _switch_symbol),
    _Case("add_mode", _add_menu),
    _Case("label", _add_menu),
    _Case("default_params", _add_menu),
    _Case("add_mode", _toolbar_entries, add_mode="toolbar"),
    _Case("label", _toolbar_entries, add_mode="toolbar"),
    _Case("toolbar_icon", _toolbar_entries, add_mode="toolbar"),
    _Case("context_actions", _context_menu),
    _Case("apply_action", _fault_context_action),
    _Case("hit_test", _drag_start, hits=True),
    _Case("begin_drag", _drag_start, hits=True),
    _Case("drag_to", _drag_move, hits=True),
    _Case("finish_drag", _drag_finish, hits=True),
    _Case("cancel_drag", _drag_cancel, hits=True),
    _Case("start_drawing", _draw_start, add_mode="toolbar"),
    _Case("preview_drawing", _draw_preview, add_mode="toolbar"),
    _Case("advance_drawing", _draw_advance, add_mode="toolbar"),
    _Case("commit_drawing", _draw_commit, add_mode="toolbar"),
    _Case("cancel_drawing", _draw_cancel, add_mode="toolbar"),
    _Case("add_mode", _context_menu, add_mode="toolbar", hits=True),
    _Case("hit_test", _context_menu, add_mode="toolbar", hits=True),
    _Case("config_for_series", _context_menu, add_mode="toolbar", hits=True),
    _Case("toggles_series_independently", _toggle),
    _Case("config_for_series", _configure),
    _Case("label", _configure),
    _Case("apply_config_to_series", _configure),
    _Case("remove_series", _remove),
    _Case("render_target", _remove),
]

_STORE_CASES: list[_Case] = [
    _Case("load_for_symbol", _switch_symbol),
    _Case("prepare_active_extensions", _reload),
    _Case("params_for", _reload),
    _Case("apply", _healthy_context_action),
    _Case("clear_transient", _healthy_draw),
]


def _case_id(case: _Case) -> str:
    return f"{case.hook}-{case.trigger.__name__.lstrip('_')}"


# ------------------------------------------------------------------
# Checks
# ------------------------------------------------------------------


def _assert_turned_off_with_one_notice(scene: _Scene, qtbot: Any) -> None:
    window = scene.window
    qtbot.waitUntil(lambda: bool(_notice_dialogs(window)), timeout=3000)
    qtbot.wait(20)
    dialogs = _notice_dialogs(window)
    assert [_dialog_text(dialog) for dialog in dialogs] == [_FAULT_NOTICE]
    assert is_disabled(_FAULT)
    assert not is_disabled(_HEALTHY)


def _assert_cleared(scene: _Scene) -> None:
    window = scene.window
    pm = window._chart.plot_manager
    assert window._drawing_session is None
    assert window._active_drawing_tool != _FAULT
    assert not window._extension_runtime.drag_active
    assert not window._preview_keys
    assert not any(key.startswith(_FAULT) for key in pm.active_series_keys())
    assert not any(key.startswith(_FAULT) for key in pm.active_marker_keys())
    assert not any(key.startswith(_FAULT) for key in window._chart.legend._labels)


def _assert_others_keep_rendering(scene: _Scene) -> None:
    window = scene.window
    if window._state.get_extension(_HEALTHY) is None:
        window._state.extensions.append(
            ChartExtensionState(name=_HEALTHY, params={"color": "#00ff00"})
        )
    renders = scene.healthy.CALLS.count("render")
    window._reload_extensions(draw_bars=False)
    keys = window._chart.plot_manager.active_series_keys()
    assert _HEALTHY_KEY in keys
    # Built-in SMAs wait for full history while an interim window shows.
    if window._displayed_history_complete:
        assert any(key.startswith("sma_") for key in keys)
    assert scene.healthy.CALLS.count("render") == renders + 1


def _follow_ups(scene: _Scene) -> list[str]:
    # Everything that would reach the failed plugin again if it were not
    # filtered: redraws, symbol switches both ways, a restored state entry,
    # every menu, the toolbar and every interaction. Returns what the
    # add-extension menu offered.
    window = scene.window
    window._reload_extensions(draw_bars=False)
    window._on_fetch_done(_series(window._state.symbol or "XBI"), history_complete=False)
    window._make_chart_load_request("XBI", Timeframe.MIN5, window._selected_route.connection)
    window._state.symbol = "QQQ"
    window._on_fetch_done(_series("QQQ"))
    window._state.symbol = "XBI"
    window._on_fetch_done(_series("XBI"))
    window._state.extensions.append(
        ChartExtensionState(name=_FAULT, params={"color": "#ff0000"})
    )
    window._reload_extensions(draw_bars=False)
    offered = _menu_picks(scene.monkeypatch, _FAULT_LABEL)
    _config_dialog_answers(scene.monkeypatch, ExtensionConfigDialog.DialogCode.Accepted)
    window._on_add_extension()
    window._drawing_tool_entries()
    window._on_drawing_tool_selected(_FAULT)
    assert window._active_drawing_tool is None
    window._on_bar_clicked(5.0, 100.0, 1.0, 1.0)
    window._on_bar_right_clicked(5.0, 100.0, 1.0, 1.0)
    window._on_drag_start(5.0, 100.0, 1.0, 1.0)
    window._on_drag_move(6.0, 100.0, 1.0, 1.0)
    window._on_drag_finish(6.0, 100.0, 1.0, 1.0)
    window._on_extension_toggled(_FAULT_KEY)
    window._on_extension_configure(_FAULT_KEY)
    window._on_extension_remove(_FAULT_KEY)
    return offered


def _assert_isolated(
    scene: _Scene,
    qtbot: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _assert_turned_off_with_one_notice(scene, qtbot)
    _assert_cleared(scene)
    calls = list(scene.fault.CALLS)
    store_calls = list(scene.fault.STORE_CALLS)
    _assert_others_keep_rendering(scene)
    offered = _follow_ups(scene)
    qtbot.wait(20)

    assert scene.fault.CALLS == calls
    assert scene.fault.STORE_CALLS == store_calls
    assert offered
    assert _FAULT_LABEL not in offered
    assert [_dialog_text(dialog) for dialog in _notice_dialogs(scene.window)] == [
        _FAULT_NOTICE
    ]
    assert scene.window._extension_runtime.rendered_target(_HEALTHY) == "chart"
    _assert_cleared(scene)
    assert "RuntimeError" in caplog.text
    for text in (
        widget_text(scene.window),
        *(_dialog_text(dialog) for dialog in _notice_dialogs(scene.window)),
        caplog.text,
    ):
        assert_secret_absent(text)
        assert "exploded" not in text


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


@pytest.mark.parametrize("case", _EXTENSION_CASES, ids=_case_id)
def test_extension_failing_in_each_call_family_is_isolated(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: _Case,
) -> None:
    scene = _scene(
        qtbot,
        tmp_path,
        monkeypatch,
        fail_in=case.hook,
        add_mode=case.add_mode,
        hits=case.hits,
    )
    arm(scene.fault, scene.healthy)
    caplog.clear()
    caplog.set_level(logging.WARNING)

    case.trigger(scene)

    assert scene.fault.CALLS[-1] == case.hook
    _assert_isolated(scene, qtbot, caplog)


@pytest.mark.parametrize("case", _STORE_CASES, ids=_case_id)
def test_store_handler_failing_in_each_call_disables_only_its_owner(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: _Case,
) -> None:
    scene = _scene(qtbot, tmp_path, monkeypatch, store_fail_in=case.hook)
    arm(scene.fault, scene.healthy)
    caplog.clear()
    caplog.set_level(logging.WARNING)

    case.trigger(scene)

    assert scene.fault.STORE_CALLS[-1] == case.hook
    _assert_isolated(scene, qtbot, caplog)


def test_store_factory_failing_at_window_start_is_isolated(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    scene = _scene(
        qtbot,
        tmp_path,
        monkeypatch,
        store_fail_in="factory",
        arm_before_window=True,
    )

    assert scene.fault.STORE_CALLS == ["factory"]
    _assert_isolated(scene, qtbot, caplog)


def test_failed_panel_extension_releases_its_panel(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scene = _scene(
        qtbot,
        tmp_path,
        monkeypatch,
        fail_in="render",
        render_target="iso_panel",
    )
    slots = scene.window._chart._extension_slots
    assert "iso_panel" in [slot.name for slot in slots]
    arm(scene.fault)
    caplog.clear()
    caplog.set_level(logging.WARNING)

    _reload(scene)

    _assert_isolated(scene, qtbot, caplog)
    assert "iso_panel" not in [slot.name for slot in slots]


def test_reselecting_a_tool_whose_cancel_fails_leaves_no_tool_selected(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scene = _scene(
        qtbot,
        tmp_path,
        monkeypatch,
        fail_in="cancel_drawing",
        add_mode="toolbar",
    )
    window = scene.window
    window._on_drawing_tool_selected(_FAULT)
    window._on_bar_clicked(5.0, 100.0, 1.0, 1.0)
    assert window._drawing_session is not None
    arm(scene.fault)
    caplog.clear()
    caplog.set_level(logging.WARNING)

    window._on_drawing_tool_selected(_FAULT)

    assert scene.fault.CALLS[-1] == "cancel_drawing"
    _assert_turned_off_with_one_notice(scene, qtbot)
    assert window._active_drawing_tool is None
    buttons = window._chart.legend._tool_palette._tool_buttons
    assert not buttons[_FAULT].isChecked()
    _assert_isolated(scene, qtbot, caplog)
    assert not buttons[_FAULT].isChecked()


def test_failure_during_a_drawing_session_of_another_tool_keeps_that_session(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scene = _scene(qtbot, tmp_path, monkeypatch, fail_in="render")
    window = scene.window
    scene.healthy.CLEAR_ON_START = False
    window._on_drawing_tool_selected(_HEALTHY)
    window._on_bar_clicked(5.0, 100.0, 1.0, 1.0)
    session = window._drawing_session
    assert session is not None and session.extension_name == _HEALTHY
    arm(scene.fault)

    _reload(scene)
    _assert_turned_off_with_one_notice(scene, qtbot)

    assert window._drawing_session is session
    assert window._active_drawing_tool == _HEALTHY


def test_plugin_failing_at_import_is_skipped_and_the_app_starts(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    plugins = tmp_path / "plugins"
    write_plugin(
        plugins,
        "a_broken",
        fault_plugin_source(name="iso_broken", label="Broken Plugin")
        + "\nraise RuntimeError('import exploded: ' + SECRET)\n",
    )
    write_plugin(
        plugins,
        "b_healthy",
        fault_plugin_source(name=_HEALTHY, label=_HEALTHY_LABEL, add_mode="toolbar"),
    )

    load_plugin_directory(plugins)
    window = _window(qtbot, tmp_path, monkeypatch)
    qtbot.waitUntil(lambda: bool(_notice_dialogs(window)), timeout=3000)
    qtbot.wait(20)

    assert [_dialog_text(dialog) for dialog in _notice_dialogs(window)] == [
        "Plugin 'a_broken.py' could not be loaded and was skipped."
    ]
    assert [entry[0] for entry in window._drawing_tool_entries()].count(_HEALTHY) == 1
    assert "iso_broken" not in [entry[0] for entry in window._drawing_tool_entries()]
    window._state.symbol = "XBI"
    window._state.timeframe = Timeframe.MIN5
    window._on_fetch_done(_series("XBI"))
    window._state.extensions.append(ChartExtensionState(name=_HEALTHY, params={}))
    window._reload_extensions(draw_bars=False)
    assert _HEALTHY_KEY in window._chart.plot_manager.active_series_keys()
    for text in (widget_text(window), caplog.text):
        assert_secret_absent(text)
        assert "exploded" not in text
