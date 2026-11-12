from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import finplot as fplt
import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMenu

import app.controller as controller_module
import tools.trend_line  # noqa: F401
from app.controller import MainWindow
from app.state import ChartExtensionState
from data.cache import Cache
from data.models import Bar, OHLCVSeries, Timeframe
from data.provider.credentials import CredentialStoreAccess, UnavailableCredentialStore
from simplechart.extensions import _registry
from simplechart.api import (
    ChartEvent,
    ChartExtension,
    ChartExtensionAction,
    ChartExtensionAddMode,
    ChartExtensionMutation,
    ChartExtensionRender,
    DragSession,
    HitTestResult,
    HorizontalLineRender,
    register_extension,
)

_FIRST = datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
_EVENTS: list[tuple[str, str]] = []
# For each applied action: did the event's bar index and timestamp name the
# same bar of the series the action received?
_ACTION_EVENT_MATCHES_SERIES: list[bool] = []


class _CrossLoadTool(ChartExtension):
    """Draggable line with one context action; records which series each
    host call resolved against."""

    def name(self) -> str:
        return "test_cross_load_tool"

    def label(self) -> str:
        return "Test Cross Load Tool"

    def default_params(self) -> dict[str, Any]:
        return {}

    def add_mode(self) -> ChartExtensionAddMode:
        return ChartExtensionAddMode.TOOLBAR

    def history_lookback_days(self, params: dict[str, Any]) -> int | None:
        return 0

    def compute(self, series: OHLCVSeries, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def render(self, series: OHLCVSeries, params: dict[str, Any]) -> ChartExtensionRender:
        return ChartExtensionRender(
            horizontal_lines=[
                HorizontalLineRender(
                    key="test_cross_load_line", y_value=100.0, label="Test",
                    color="#ffffff", line_width=1.0,
                )
            ]
        )

    def hit_test(
        self, series: OHLCVSeries, params: dict[str, Any], event: ChartEvent,
        visible_keys: set[str],
    ) -> HitTestResult | None:
        if event.button == "right" or "test_cross_load_line" not in visible_keys:
            return None
        return HitTestResult(self.name(), "test_cross_load_line")

    def finish_drag(
        self, series: OHLCVSeries, session: DragSession, event: ChartEvent,
    ) -> ChartExtensionMutation | None:
        _EVENTS.append(("finish_drag", series.symbol))
        return None

    def cancel_drag(self, session: DragSession) -> ChartExtensionMutation | None:
        _EVENTS.append(("cancel_drag", ""))
        return None

    def context_actions(
        self, series: OHLCVSeries, params: dict[str, Any], event: ChartEvent,
    ) -> list[ChartExtensionAction]:
        return [ChartExtensionAction(self.name(), "record", "Record")]

    def apply_action(
        self, series: OHLCVSeries, params: dict[str, Any], action_key: str,
        event: ChartEvent,
    ) -> ChartExtensionMutation | None:
        _EVENTS.append(("apply_action", series.symbol))
        assert event.bar_index is not None
        bar_ms = int(series.bars[event.bar_index].timestamp.timestamp() * 1000)
        _ACTION_EVENT_MATCHES_SERIES.append(bar_ms == event.timestamp_ms)
        return None



@pytest.fixture(autouse=True)
def _isolated_state() -> Iterator[None]:
    # The test tool's context action would appear in other tests' menus;
    # register it only for these tests.
    saved = _registry.snapshot()
    register_extension(_CrossLoadTool)
    fplt.windows.clear()
    _EVENTS.clear()
    _ACTION_EVENT_MATCHES_SERIES.clear()
    yield
    fplt.windows.clear()
    _EVENTS.clear()
    _ACTION_EVENT_MATCHES_SERIES.clear()
    _registry.restore(saved)


class _DragEvent:
    """The parts of a pyqtgraph MouseDragEvent the chart's drag patch reads,
    positioned at the centre of the price panel."""

    def __init__(
        self,
        viewbox: Any,
        finish: bool,
        modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
    ) -> None:
        self._scene_pos = viewbox.sceneBoundingRect().center()
        self._local_pos = viewbox.mapFromScene(self._scene_pos)
        self._finish = finish
        self._modifiers = modifiers

    def button(self) -> Qt.MouseButton:
        return Qt.MouseButton.LeftButton

    def modifiers(self) -> Qt.KeyboardModifier:
        return self._modifiers

    def isStart(self) -> bool:
        return not self._finish

    def isFinish(self) -> bool:
        return self._finish

    def pos(self) -> QPointF:
        return self._local_pos

    def lastPos(self) -> QPointF:
        return self._local_pos

    def buttonDownPos(self, _button: object = None) -> QPointF:
        return self._local_pos

    def scenePos(self) -> QPointF:
        return self._scene_pos

    def accept(self) -> None:
        pass

    def ignore(self) -> None:
        pass


def _record_action(menu: QMenu) -> QAction:
    return next(action for action in menu.actions() if action.text() == "Record")


def _window(qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> MainWindow:
    monkeypatch.setattr(
        controller_module,
        "initialize_keyring_credential_store",
        lambda: CredentialStoreAccess(UnavailableCredentialStore("test"), False, "test"),
    )
    production_load = MainWindow._load
    monkeypatch.setattr(MainWindow, "_load", lambda window: None)
    monkeypatch.setattr(MainWindow, "_refresh_watchlist_snapshots", lambda window: None)
    monkeypatch.setattr(MainWindow, "_refresh_level1", lambda window: None)
    monkeypatch.setattr(controller_module, "show_warning", lambda *_args: None)
    window = MainWindow(str(tmp_path / "test.db"))
    qtbot.addWidget(window)
    monkeypatch.setattr(MainWindow, "_load", production_load)
    window.resize(1200, 800)
    window.show()
    qtbot.waitExposed(window)
    return window


def _series(symbol: str, count: int, base: float, first: datetime = _FIRST) -> OHLCVSeries:
    return OHLCVSeries(
        symbol,
        Timeframe.MIN5,
        [
            Bar(first + timedelta(minutes=5 * i), base, base + 1, base - 1, base + 0.5, 1_000)
            for i in range(count)
        ],
    )


def _start_load(window: MainWindow, symbol: str, timeframe: Timeframe = Timeframe.MIN5) -> None:
    """Begin a production load without dispatching the fetch worker."""
    window._state.symbol = symbol
    window._state.timeframe = timeframe
    window._load()
    window._chart_load_timer.stop()


def _deliver(window: MainWindow, series: OHLCVSeries) -> None:
    request = window._current_chart_request
    assert request is not None
    window._on_chart_fetch_done(request, series)


def _display(window: MainWindow, series: OHLCVSeries) -> None:
    _start_load(window, series.symbol, series.timeframe)
    _deliver(window, series)


def _with_tool(window: MainWindow) -> None:
    window._state.extensions.append(ChartExtensionState(name="test_cross_load_tool", params={}))
    window._reload_extensions(draw_bars=False)


def test_trend_line_started_on_one_symbol_cannot_finish_on_another(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    _display(window, _series("XBI", 50, 100.0))
    window._on_drawing_tool_selected("trend_line")
    window._on_bar_clicked(5.0, 100.0, 1.0, 1.0)
    assert window._drawing_session is not None

    _start_load(window, "SPY")
    assert window._drawing_session is None
    _deliver(window, _series("SPY", 50, 600.0))
    window._on_bar_clicked(7.0, 600.0, 1.0, 1.0)

    with Cache(str(tmp_path / "test.db")) as cache:
        assert cache.get_extension_records("trend_line.drawings", "SPY") == []
        assert cache.get_extension_records("trend_line.drawings", "XBI") == []
    # The tool stays selected; the click on SPY started a fresh line.
    assert window._active_drawing_tool == "trend_line"
    assert window._drawing_session is not None


def test_timeframe_change_cancels_a_drawing_in_progress(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    _display(window, _series("XBI", 50, 100.0))
    window._on_drawing_tool_selected("trend_line")
    window._on_bar_clicked(5.0, 100.0, 1.0, 1.0)

    _start_load(window, "XBI", Timeframe.MIN15)

    assert window._drawing_session is None
    assert window._active_drawing_tool == "trend_line"


def test_reload_during_a_drag_cancels_it_and_the_release_writes_nothing(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    _display(window, _series("XBI", 300, 100.0))
    _with_tool(window)
    interactions = window._chart.interactions
    viewbox = interactions._price_ax.vb  # type: ignore[attr-defined]
    viewbox.mouseDragEvent(_DragEvent(viewbox, finish=False))
    assert interactions._drag_active

    # A symbol refresh reloads the same symbol while the mouse is held.
    _start_load(window, "XBI")
    viewbox.mouseDragEvent(_DragEvent(viewbox, finish=False))
    _deliver(window, _series("XBI", 500, 100.0))
    # The held mouse keeps moving over the new bars before release.
    viewbox.mouseDragEvent(_DragEvent(viewbox, finish=False))
    viewbox.mouseDragEvent(_DragEvent(viewbox, finish=True))

    assert _EVENTS == [("cancel_drag", "")]
    assert not interactions._drag_active
    assert window._extension_runtime._drag_session is None


    # The next gesture grabs and drags normally.
    viewbox.mouseDragEvent(_DragEvent(viewbox, finish=False))
    viewbox.mouseDragEvent(_DragEvent(viewbox, finish=True))
    assert _EVENTS == [("cancel_drag", ""), ("finish_drag", "XBI")]


def test_right_click_while_a_new_symbol_loads_does_nothing(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    _display(window, _series("XBI", 50, 100.0))
    _with_tool(window)
    menus: list[QMenu] = []

    def choose_first(menu: QMenu, *_args: object) -> QAction | None:
        menus.append(menu)
        return _record_action(menu)

    monkeypatch.setattr(QMenu, "exec", choose_first)

    _start_load(window, "SPY")
    window._on_bar_right_clicked(5.0, 100.0, 1.0, 1.0)

    assert menus == []
    assert _EVENTS == []


def test_context_action_is_dropped_when_a_load_starts_while_its_menu_is_open(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    _display(window, _series("XBI", 50, 100.0))
    _with_tool(window)

    def reload_then_choose(menu: QMenu, *_args: object) -> QAction | None:
        # QMenu.exec runs a nested event loop: a symbol refresh reload and its
        # result can both arrive before it returns, replacing the series the
        # click was resolved against.
        _display(window, _series("XBI", 80, 100.0))
        return _record_action(menu)

    monkeypatch.setattr(QMenu, "exec", reload_then_choose)

    window._on_bar_right_clicked(5.0, 100.0, 1.0, 1.0)

    assert _EVENTS == []


def test_context_action_still_applies_when_the_same_load_merges_history(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    _start_load(window, "XBI")
    request = window._current_chart_request
    assert request is not None
    window._on_chart_recent_ready(
        request, _series("XBI", 50, 100.0, _FIRST + timedelta(minutes=5 * 30)),
    )
    _with_tool(window)

    def merge_then_choose(menu: QMenu, *_args: object) -> QAction | None:
        # The same load's full history, 30 bars older, arrives while the
        # menu is open.
        window._on_chart_fetch_done(request, _series("XBI", 80, 100.0))
        return _record_action(menu)

    monkeypatch.setattr(QMenu, "exec", merge_then_choose)

    window._on_bar_right_clicked(5.0, 100.0, 1.0, 1.0)

    assert _EVENTS == [("apply_action", "XBI")]
    # The action receives the series its event was resolved against.
    assert _ACTION_EVENT_MATCHES_SERIES == [True]


def test_cancelled_drag_released_with_a_modifier_does_not_block_the_next_drag(
    qtbot: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(qtbot, tmp_path, monkeypatch)
    _display(window, _series("XBI", 300, 100.0))
    _with_tool(window)
    interactions = window._chart.interactions
    viewbox = interactions._price_ax.vb  # type: ignore[attr-defined]
    viewbox.mouseDragEvent(_DragEvent(viewbox, finish=False))
    assert interactions.cancel_drag()  # Escape while the mouse is held

    viewbox.mouseDragEvent(
        _DragEvent(viewbox, finish=True, modifiers=Qt.KeyboardModifier.ShiftModifier)
    )
    viewbox.mouseDragEvent(_DragEvent(viewbox, finish=False))
    viewbox.mouseDragEvent(_DragEvent(viewbox, finish=True))

    assert _EVENTS == [("cancel_drag", ""), ("finish_drag", "XBI")]
