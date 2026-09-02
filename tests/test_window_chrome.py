import logging
from typing import Any

import pytest
from PyQt6.QtCore import QEvent, QPointF, QSize, Qt
from PyQt6.QtGui import QHoverEvent, QMouseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QMainWindow,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtTest import QTest

import app.controller as controller_module
from app.controller import MainWindow
from app.window_chrome import (
    MainWindowTitleBar,
    WindowResizeController,
    is_wayland_platform,
    resize_edges_at,
)


class _WindowHandle:
    def __init__(self) -> None:
        self.move_requests = 0
        self.resize_requests: list[Qt.Edge] = []

    def startSystemMove(self) -> bool:
        self.move_requests += 1
        return True

    def startSystemResize(self, edges: Qt.Edge) -> bool:
        self.resize_requests.append(edges)
        return True


class _DecliningWindowHandle(_WindowHandle):
    def startSystemMove(self) -> bool:
        self.move_requests += 1
        return False

    def startSystemResize(self, edges: Qt.Edge) -> bool:
        self.resize_requests.append(edges)
        return False


@pytest.mark.parametrize(
    ("platform_name", "expected"),
    (
        ("wayland", True),
        ("wayland-egl", True),
        ("Wayland", True),
        ("xcb", False),
        ("offscreen", False),
        ("windows", False),
        ("cocoa", False),
    ),
)
def test_wayland_platform_detection(
    platform_name: str,
    expected: bool,
) -> None:
    assert is_wayland_platform(platform_name) is expected


def test_title_bar_structure_and_accessibility(qtbot: Any) -> None:
    window = QMainWindow()
    window.setWindowTitle("Simple Chart")
    qtbot.addWidget(window)
    title_bar = MainWindowTitleBar(window)
    qtbot.addWidget(title_bar)
    title_bar.show()
    QApplication.processEvents()

    label = title_bar.findChild(QLabel, "mainWindowTitle")
    left_reserve = title_bar.findChild(QWidget, "mainWindowTitleReserve")
    controls = title_bar.findChild(QWidget, "mainWindowTitleControls")
    buttons = title_bar.findChildren(QToolButton)

    assert title_bar.height() == 36
    assert "background: #dedede" in title_bar.styleSheet()
    assert "border-bottom: 1px solid #9d978d" in title_bar.styleSheet()
    assert "color: #202020" in title_bar.styleSheet()
    assert label is not None
    assert label.text() == window.windowTitle() == "Simple Chart"
    assert label.testAttribute(
        Qt.WidgetAttribute.WA_TransparentForMouseEvents
    )
    assert left_reserve is not None
    assert controls is not None
    assert left_reserve.width() == controls.width()
    assert len(buttons) == 3
    assert {button.toolTip() for button in buttons} == {
        "Minimize",
        "Maximize",
        "Close",
    }
    assert {button.accessibleName() for button in buttons} == {
        "Minimize window",
        "Maximize window",
        "Close window",
    }
    for button in buttons:
        assert not button.icon().isNull()
        assert button.focusPolicy() == Qt.FocusPolicy.TabFocus
    close_button = title_bar.findChild(
        QToolButton,
        "mainWindowCloseButton",
    )
    maximize_button = title_bar.findChild(
        QToolButton,
        "mainWindowMaximizeButton",
    )
    assert close_button is not None
    assert maximize_button is not None
    assert close_button.iconSize() == QSize(16, 16)
    assert maximize_button.iconSize() == QSize(16, 16)
    assert "QToolButton:hover { background: #c6c6c6; }" in (
        title_bar.styleSheet()
    )
    assert "QToolButton:pressed { background: #b5b5b5; }" in (
        title_bar.styleSheet()
    )
    assert "mainWindowCloseButton:hover" not in title_bar.styleSheet()

    close_image = close_button.icon().pixmap(QSize(16, 16)).toImage()
    maximize_image = maximize_button.icon().pixmap(QSize(16, 16)).toImage()
    close_pixels = [
        close_image.pixelColor(x, y)
        for y in range(close_image.height())
        for x in range(close_image.width())
        if close_image.pixelColor(x, y).alpha() > 20
    ]
    maximize_pixels = [
        maximize_image.pixelColor(x, y)
        for y in range(maximize_image.height())
        for x in range(maximize_image.width())
        if maximize_image.pixelColor(x, y).alpha() > 20
    ]
    assert close_pixels
    assert maximize_pixels
    for pixel in close_pixels + maximize_pixels:
        assert abs(pixel.red() - 48) <= 1
        assert pixel.red() == pixel.green() == pixel.blue()


def test_title_bar_requests_system_move_and_toggles_maximize(
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = QMainWindow()
    window.setWindowTitle("Simple Chart")
    qtbot.addWidget(window)
    title_bar = MainWindowTitleBar(window)
    handle = _WindowHandle()
    monkeypatch.setattr(window, "windowHandle", lambda: handle)

    press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(10, 10),
        QPointF(110, 110),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    title_bar.mousePressEvent(press)

    assert handle.move_requests == 1
    assert press.isAccepted()

    double_click = QMouseEvent(
        QEvent.Type.MouseButtonDblClick,
        QPointF(10, 10),
        QPointF(110, 110),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    title_bar.mouseDoubleClickEvent(double_click)
    assert window.isMaximized()
    title_bar.mouseDoubleClickEvent(double_click)
    assert not window.isMaximized()


def test_title_bar_tracks_external_window_state_changes(qtbot: Any) -> None:
    window = QMainWindow()
    window.setWindowTitle("Simple Chart")
    qtbot.addWidget(window)
    title_bar = MainWindowTitleBar(window)
    maximize_button = title_bar.findChild(
        QToolButton,
        "mainWindowMaximizeButton",
    )
    assert maximize_button is not None

    window.setWindowState(Qt.WindowState.WindowMaximized)
    QApplication.processEvents()

    assert maximize_button.toolTip() == "Restore"
    assert maximize_button.accessibleName() == "Restore window"

    window.setWindowState(Qt.WindowState.WindowNoState)
    QApplication.processEvents()

    assert maximize_button.toolTip() == "Maximize"
    assert maximize_button.accessibleName() == "Maximize window"


def test_title_bar_initializes_maximized_presentation(qtbot: Any) -> None:
    window = QMainWindow()
    window.setWindowTitle("Simple Chart")
    window.setWindowState(Qt.WindowState.WindowMaximized)
    qtbot.addWidget(window)

    title_bar = MainWindowTitleBar(window)
    maximize_button = title_bar.findChild(
        QToolButton,
        "mainWindowMaximizeButton",
    )

    assert maximize_button is not None
    assert maximize_button.toolTip() == "Restore"
    assert maximize_button.accessibleName() == "Restore window"


def test_title_bar_reports_unavailable_compositor_move(
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    window = QMainWindow()
    window.setWindowTitle("Simple Chart")
    qtbot.addWidget(window)
    title_bar = MainWindowTitleBar(window)
    handle = _DecliningWindowHandle()
    monkeypatch.setattr(window, "windowHandle", lambda: handle)
    caplog.set_level(logging.WARNING, logger="app.window_chrome")
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(10, 10),
        QPointF(110, 110),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    title_bar.mousePressEvent(press)
    assert "declined the main-window move request" in caplog.text

    caplog.clear()
    monkeypatch.setattr(window, "windowHandle", lambda: None)
    title_bar.mousePressEvent(press)
    assert "without a window handle" in caplog.text


@pytest.mark.parametrize(
    ("point", "expected"),
    (
        (QPointF(0, 0), Qt.Edge.TopEdge | Qt.Edge.LeftEdge),
        (QPointF(99, 0), Qt.Edge.TopEdge | Qt.Edge.RightEdge),
        (QPointF(0, 79), Qt.Edge.BottomEdge | Qt.Edge.LeftEdge),
        (QPointF(99, 79), Qt.Edge.BottomEdge | Qt.Edge.RightEdge),
        (QPointF(50, 2), Qt.Edge.TopEdge),
        (QPointF(50, 77), Qt.Edge.BottomEdge),
        (QPointF(2, 40), Qt.Edge.LeftEdge),
        (QPointF(97, 40), Qt.Edge.RightEdge),
        (QPointF(50, 40), None),
    ),
)
def test_resize_edge_mapping(
    point: QPointF,
    expected: Qt.Edge | None,
) -> None:
    assert resize_edges_at(point, QSize(100, 80), 5) == expected


def test_resize_controller_tracks_hover_and_requests_system_resize(
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = QFrame()
    frame.resize(100, 80)
    qtbot.addWidget(frame)
    frame.show()
    controller = WindowResizeController(frame)
    handle = _WindowHandle()
    monkeypatch.setattr(frame, "windowHandle", lambda: handle)

    assert frame.hasMouseTracking()

    hover = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(1, 1),
        QPointF(101, 101),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    controller.eventFilter(frame, hover)
    assert frame.cursor().shape() == Qt.CursorShape.SizeFDiagCursor

    press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(1, 1),
        QPointF(101, 101),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    assert controller.eventFilter(frame, press)
    assert handle.resize_requests == [Qt.Edge.TopEdge | Qt.Edge.LeftEdge]

    interior = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(50, 40),
        QPointF(150, 140),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    controller.eventFilter(frame, interior)
    assert frame.cursor().shape() == Qt.CursorShape.ArrowCursor


def test_resize_controller_suppresses_invalid_requests(
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = QFrame()
    frame.resize(100, 80)
    qtbot.addWidget(frame)
    controller = WindowResizeController(frame)
    handle = _WindowHandle()
    monkeypatch.setattr(frame, "windowHandle", lambda: handle)

    right_press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(1, 40),
        QPointF(101, 140),
        Qt.MouseButton.RightButton,
        Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.NoModifier,
    )
    assert not controller.eventFilter(frame, right_press)

    monkeypatch.setattr(frame, "isMaximized", lambda: True)
    left_press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(1, 40),
        QPointF(101, 140),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    assert not controller.eventFilter(frame, left_press)
    assert handle.resize_requests == []


def test_resize_cursor_clears_after_pointer_enters_child_content(
    qtbot: Any,
) -> None:
    frame = QFrame()
    frame.resize(100, 80)
    frame.setStyleSheet("QFrame { border: 3px solid #9d978d; }")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(0, 0, 0, 0)
    child = QWidget(frame)
    layout.addWidget(child)
    qtbot.addWidget(frame)
    WindowResizeController(frame, 3)
    frame.show()
    QApplication.processEvents()

    QTest.mouseMove(frame, QPointF(50, 1).toPoint())
    QApplication.processEvents()
    assert frame.cursor().shape() == Qt.CursorShape.SizeVerCursor

    QTest.mouseMove(child, QPointF(40, 20).toPoint())
    QApplication.processEvents()
    assert frame.cursor().shape() == Qt.CursorShape.ArrowCursor


def test_resize_cursor_clears_on_hover_leave(qtbot: Any) -> None:
    frame = QFrame()
    frame.resize(100, 80)
    qtbot.addWidget(frame)
    controller = WindowResizeController(frame, 3)
    frame.setCursor(Qt.CursorShape.SizeFDiagCursor)
    hover_leave = QHoverEvent(
        QEvent.Type.HoverLeave,
        QPointF(-1, -1),
        QPointF(1, 1),
        Qt.KeyboardModifier.NoModifier,
    )

    controller.eventFilter(frame, hover_leave)

    assert frame.cursor().shape() == Qt.CursorShape.ArrowCursor


def test_resize_controller_reports_unavailable_compositor_resize(
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    frame = QFrame()
    frame.resize(100, 80)
    qtbot.addWidget(frame)
    controller = WindowResizeController(frame)
    handle = _DecliningWindowHandle()
    monkeypatch.setattr(frame, "windowHandle", lambda: handle)
    caplog.set_level(logging.WARNING, logger="app.window_chrome")
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(1, 40),
        QPointF(101, 140),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    controller.eventFilter(frame, press)
    assert "declined the main-window resize request" in caplog.text

    caplog.clear()
    monkeypatch.setattr(frame, "windowHandle", lambda: None)
    controller.eventFilter(frame, press)
    assert "without a window handle" in caplog.text


@pytest.mark.parametrize("custom_chrome", (True, False))
def test_main_window_wires_custom_chrome_only_on_wayland(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    custom_chrome: bool,
) -> None:
    monkeypatch.setattr(
        controller_module,
        "is_wayland_platform",
        lambda platform_name: custom_chrome,
    )
    monkeypatch.setattr(MainWindow, "_load", lambda window: None)
    monkeypatch.setattr(
        MainWindow,
        "_refresh_watchlist_snapshots",
        lambda window: None,
    )

    window = MainWindow(str(tmp_path / "test.db"))
    qtbot.addWidget(window)
    window.show()
    QApplication.processEvents()

    frame = window.findChild(QFrame, "appFrame")
    title_bar = window.findChild(MainWindowTitleBar)
    assert frame is not None
    assert bool(
        window.windowFlags() & Qt.WindowType.FramelessWindowHint
    ) is custom_chrome
    assert (title_bar is not None) is custom_chrome
    assert (
        window._window_resize_controller is not None
    ) is custom_chrome

    if title_bar is not None:
        title_label = title_bar.findChild(QLabel, "mainWindowTitle")
        assert title_label is not None
        assert title_label.text() == window.windowTitle()
        assert title_bar.geometry().left() == frame.frameWidth() == 3
        layout = frame.layout()
        assert layout is not None
        assert layout.itemAt(0).widget() is title_bar
        assert layout.itemAt(1).widget() is window._app_header
    else:
        assert frame.frameWidth() == 5
