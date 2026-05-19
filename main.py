"""
main.py

Simple Chart entry point.

Run with:
    python main.py
    python main.py --provider alpaca   # once Alpaca is configured
    python main.py --db /path/to/db    # custom database location

The SQLite database defaults to ~/.simplechart/simplechart.db, which
keeps chart data and AVWAP anchors out of the project directory and
persistent across reinstalls.
"""

import argparse
import sys
from pathlib import Path

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QApplication

from app.controller import MainWindow


_DEFAULT_DB = Path.home() / ".simplechart" / "simplechart.db"
_APP_ROOT = Path(__file__).resolve().parent
_ICON_PATH = _APP_ROOT / "assets" / "simple-chart.svg"
_APP_NAME = "Simple Chart"
_ICON_THEME_NAME = "simplechart"
_DESKTOP_FILE_ID = "io.simplechart.SimpleChart"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simple Chart — stock charting app")
    parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB,
        help=f"Path to SQLite database (default: {_DEFAULT_DB})",
    )
    parser.add_argument(
        "--provider",
        default="yfinance",
        help="Data provider name (default: yfinance)",
    )
    return parser.parse_args()


def _load_app_icon() -> QIcon:
    """Load the checked-in icon, then fall back to theme or generated art."""
    if _ICON_PATH.is_file():
        icon = QIcon(str(_ICON_PATH))
        if not icon.isNull():
            return icon

    theme_icon = QIcon.fromTheme(_ICON_THEME_NAME)
    if not theme_icon.isNull():
        return theme_icon

    return _build_fallback_icon()


def _build_fallback_icon(size: int = 256) -> QIcon:
    """Generate a simple chart icon so startup never depends on external files."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    painter.setPen(QPen(QColor("#6f665c"), 12))
    painter.setBrush(QColor("#f1eadf"))
    painter.drawRoundedRect(QRectF(18, 18, size - 36, size - 36), 34, 34)

    painter.setPen(QPen(QColor("#c8beb0"), 4))
    painter.setBrush(QColor("#fffdfa"))
    painter.drawRoundedRect(QRectF(42, 42, size - 84, size - 84), 18, 18)

    painter.setPen(QPen(QColor("#5e5850"), 6))
    for x_pos, top, bottom in (
        (74, 92, 192),
        (106, 70, 182),
        (138, 104, 202),
        (170, 82, 170),
    ):
        painter.drawLine(x_pos, top, x_pos, bottom)

    painter.setPen(Qt.PenStyle.NoPen)
    for x_pos, y_pos, height, color in (
        (60, 124, 42, "#d94a4a"),
        (92, 104, 52, "#00b56a"),
        (124, 138, 34, "#d94a4a"),
        (156, 112, 38, "#00b56a"),
    ):
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(QRectF(x_pos, y_pos, 28, height), 5, 5)

    painter.end()
    return QIcon(pixmap)


def main() -> None:
    args = _parse_args()

    # Ensure the database directory exists before SQLite tries to open the file.
    args.db.parent.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)
    app.setApplicationName(_APP_NAME)
    app.setApplicationDisplayName(_APP_NAME)
    app.setDesktopFileName(_DESKTOP_FILE_ID)
    app_icon = _load_app_icon()
    app.setWindowIcon(app_icon)

    window = MainWindow(
        db_path=str(args.db),
        provider_name=args.provider,
    )
    window.setWindowIcon(app_icon)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
