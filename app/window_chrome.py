import logging
from typing import Literal

from PyQt6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QHoverEvent, QIcon, QMouseEvent, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QToolButton,
    QWidget,
)


_TITLE_HEIGHT = 36
_CONTROL_SIZE = 28
_LOGGER = logging.getLogger(__name__)
_ControlIcon = Literal["minimize", "maximize", "restore", "close"]


def is_wayland_platform(platform_name: str) -> bool:
    return platform_name.lower().startswith("wayland")


def resize_edges_at(
    position: QPointF,
    size: QSize,
    resize_width: int,
) -> Qt.Edge | None:
    left = position.x() < resize_width
    right = position.x() >= size.width() - resize_width
    top = position.y() < resize_width
    bottom = position.y() >= size.height() - resize_width

    edges = Qt.Edge(0)
    if left:
        edges |= Qt.Edge.LeftEdge
    elif right:
        edges |= Qt.Edge.RightEdge
    if top:
        edges |= Qt.Edge.TopEdge
    elif bottom:
        edges |= Qt.Edge.BottomEdge
    return edges if edges else None


def _window_control_icon(control: _ControlIcon) -> QIcon:
    icon = QIcon()
    for size in (16, 32):
        scale = size / 16
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#303030"), 1.5 * scale)
        pen.setCapStyle(Qt.PenCapStyle.SquareCap)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)

        if control == "minimize":
            painter.drawLine(
                QPointF(4 * scale, 11.5 * scale),
                QPointF(12 * scale, 11.5 * scale),
            )
        elif control == "maximize":
            painter.drawRect(
                QRectF(3.5 * scale, 3.5 * scale, 9 * scale, 9 * scale)
            )
        elif control == "restore":
            painter.drawRect(
                QRectF(5 * scale, 3.5 * scale, 7.5 * scale, 7.5 * scale)
            )
            painter.drawRect(
                QRectF(3.5 * scale, 5 * scale, 7.5 * scale, 7.5 * scale)
            )
        else:
            painter.drawLine(
                QPointF(4 * scale, 4 * scale),
                QPointF(12 * scale, 12 * scale),
            )
            painter.drawLine(
                QPointF(4 * scale, 12 * scale),
                QPointF(12 * scale, 4 * scale),
            )
        painter.end()
        icon.addPixmap(pixmap)
    return icon


class MainWindowTitleBar(QFrame):
    def __init__(
        self,
        window: QMainWindow,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._window = window
        self.setObjectName("mainWindowTitleBar")
        self.setFixedHeight(_TITLE_HEIGHT)
        self.setStyleSheet(
            "QFrame#mainWindowTitleBar {"
            " background: #dedede;"
            " border-bottom: 1px solid #9d978d;"
            "}"
            "QLabel#mainWindowTitle {"
            " color: #202020;"
            " font-weight: bold;"
            "}"
            "QToolButton {"
            " background: transparent;"
            " border: 1px solid transparent;"
            " border-radius: 3px;"
            "}"
            "QToolButton:hover { background: #c6c6c6; }"
            "QToolButton:pressed { background: #b5b5b5; }"
            "QToolButton:focus { border-color: #6f6f6f; }"
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(0)

        reserve_width = 3 * _CONTROL_SIZE
        reserve = QWidget(self)
        reserve.setObjectName("mainWindowTitleReserve")
        reserve.setFixedWidth(reserve_width)
        row.addWidget(reserve)

        title = QLabel(window.windowTitle(), self)
        title.setObjectName("mainWindowTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
        )
        row.addWidget(title, 1)

        controls = QWidget(self)
        controls.setObjectName("mainWindowTitleControls")
        controls.setFixedWidth(reserve_width)
        control_layout = QHBoxLayout(controls)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.setSpacing(0)

        self._minimize_button = self._make_button(
            "mainWindowMinimizeButton",
            "Minimize",
            "Minimize window",
            "minimize",
        )
        self._maximize_button = self._make_button(
            "mainWindowMaximizeButton",
            "Maximize",
            "Maximize window",
            "maximize",
        )
        self._close_button = self._make_button(
            "mainWindowCloseButton",
            "Close",
            "Close window",
            "close",
        )
        control_layout.addWidget(self._minimize_button)
        control_layout.addWidget(self._maximize_button)
        control_layout.addWidget(self._close_button)
        row.addWidget(controls)

        self._minimize_button.clicked.connect(window.showMinimized)
        self._maximize_button.clicked.connect(self._toggle_maximized)
        self._close_button.clicked.connect(window.close)
        window.installEventFilter(self)
        self._update_maximize_button()

    def _make_button(
        self,
        object_name: str,
        tooltip: str,
        accessible_name: str,
        icon: _ControlIcon,
    ) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName(object_name)
        button.setFixedSize(_CONTROL_SIZE, _CONTROL_SIZE)
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        button.setToolTip(tooltip)
        button.setAccessibleName(accessible_name)
        button.setIcon(_window_control_icon(icon))
        button.setIconSize(QSize(16, 16))
        return button

    def eventFilter(self, watched: QObject | None, event: QEvent | None) -> bool:
        if (
            watched is self._window
            and event is not None
            and event.type() == QEvent.Type.WindowStateChange
        ):
            self._update_maximize_button()
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if event is None or event.button() != Qt.MouseButton.LeftButton:
            return
        window_handle = self._window.windowHandle()
        if window_handle is None:
            _LOGGER.warning("Cannot move the main window without a window handle.")
        elif not window_handle.startSystemMove():
            _LOGGER.warning("The compositor declined the main-window move request.")
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent | None) -> None:
        if event is None or event.button() != Qt.MouseButton.LeftButton:
            return
        self._toggle_maximized()
        event.accept()

    def _toggle_maximized(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()
        self._update_maximize_button()

    def _update_maximize_button(self) -> None:
        if self._window.isMaximized():
            self._maximize_button.setIcon(_window_control_icon("restore"))
            self._maximize_button.setToolTip("Restore")
            self._maximize_button.setAccessibleName("Restore window")
        else:
            self._maximize_button.setIcon(_window_control_icon("maximize"))
            self._maximize_button.setToolTip("Maximize")
            self._maximize_button.setAccessibleName("Maximize window")


class WindowResizeController(QObject):
    def __init__(self, frame: QFrame, resize_width: int = 5) -> None:
        super().__init__(frame)
        self._frame = frame
        self._resize_width = resize_width
        frame.setMouseTracking(True)
        frame.setAttribute(Qt.WidgetAttribute.WA_Hover)
        frame.installEventFilter(self)

    def eventFilter(self, watched: QObject | None, event: QEvent | None) -> bool:
        if watched is not self._frame or event is None:
            return super().eventFilter(watched, event)
        if event.type() in (QEvent.Type.Leave, QEvent.Type.HoverLeave):
            self._frame.unsetCursor()
            return False
        if isinstance(event, QHoverEvent):
            self._update_resize_cursor(event.position())
            return False
        if not isinstance(event, QMouseEvent):
            return super().eventFilter(watched, event)

        window = self._frame.window()
        if window is None:
            self._frame.unsetCursor()
            return False
        if window.isMaximized() or window.isFullScreen():
            self._frame.unsetCursor()
            return False

        edges = resize_edges_at(
            event.position(),
            self._frame.size(),
            self._resize_width,
        )
        if event.type() == QEvent.Type.MouseMove:
            self._set_resize_cursor(edges)
            return False
        if (
            event.type() != QEvent.Type.MouseButtonPress
            or event.button() != Qt.MouseButton.LeftButton
            or edges is None
        ):
            return False

        window_handle = window.windowHandle()
        if window_handle is None:
            _LOGGER.warning("Cannot resize the main window without a window handle.")
        elif not window_handle.startSystemResize(edges):
            _LOGGER.warning(
                "The compositor declined the main-window resize request."
            )
        event.accept()
        return True

    def _update_resize_cursor(self, position: QPointF) -> None:
        window = self._frame.window()
        if window is None or window.isMaximized() or window.isFullScreen():
            self._frame.unsetCursor()
            return
        self._set_resize_cursor(
            resize_edges_at(
                position,
                self._frame.size(),
                self._resize_width,
            )
        )

    def _set_resize_cursor(self, edges: Qt.Edge | None) -> None:
        cursors = {
            Qt.Edge.TopEdge: Qt.CursorShape.SizeVerCursor,
            Qt.Edge.BottomEdge: Qt.CursorShape.SizeVerCursor,
            Qt.Edge.LeftEdge: Qt.CursorShape.SizeHorCursor,
            Qt.Edge.RightEdge: Qt.CursorShape.SizeHorCursor,
            Qt.Edge.TopEdge
            | Qt.Edge.LeftEdge: Qt.CursorShape.SizeFDiagCursor,
            Qt.Edge.BottomEdge
            | Qt.Edge.RightEdge: Qt.CursorShape.SizeFDiagCursor,
            Qt.Edge.TopEdge
            | Qt.Edge.RightEdge: Qt.CursorShape.SizeBDiagCursor,
            Qt.Edge.BottomEdge
            | Qt.Edge.LeftEdge: Qt.CursorShape.SizeBDiagCursor,
        }
        cursor = cursors.get(edges) if edges is not None else None
        if cursor is None:
            self._frame.unsetCursor()
        else:
            self._frame.setCursor(cursor)
