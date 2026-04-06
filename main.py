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

from PyQt6.QtWidgets import QApplication

from app.controller import MainWindow


_DEFAULT_DB = Path(__file__).parent / "simplechart.db"


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


def main() -> None:
    args = _parse_args()

    # Ensure the database directory exists before SQLite tries to open the file.
    args.db.parent.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)
    app.setApplicationName("Simple Chart")

    window = MainWindow(
        db_path=str(args.db),
        provider_name=args.provider,
    )
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
