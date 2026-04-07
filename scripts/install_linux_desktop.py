#!/usr/bin/env python3
"""
Install the Simple Chart desktop launcher and icon into the current user's
XDG data directories.

Typical usage after cloning the repo:
    python -m pip install -e .
    python scripts/install_linux_desktop.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


APP_ID = "io.simplechart.SimpleChart"
ICON_NAME = "simplechart"
REPO_ROOT = Path(__file__).resolve().parent.parent
DESKTOP_SOURCE = REPO_ROOT / f"{APP_ID}.desktop"
ICON_SOURCE = REPO_ROOT / "assets" / "simple-chart.svg"


def _xdg_data_home() -> Path:
    value = os.environ.get("XDG_DATA_HOME")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".local" / "share"


def _desktop_destination() -> Path:
    return _xdg_data_home() / "applications" / f"{APP_ID}.desktop"


def _icon_destination() -> Path:
    return _xdg_data_home() / "icons" / "hicolor" / "scalable" / "apps" / f"{ICON_NAME}.svg"


def _install_icon(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Missing required file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _resolve_simplechart_executable() -> str:
    executable = shutil.which("simplechart")
    if executable is None:
        raise RuntimeError(
            "Could not find 'simplechart' on PATH. Run 'python -m pip install -e .' "
            "in the target environment before installing the desktop launcher."
        )
    return executable


def _install_desktop_file(source: Path, destination: Path, executable: str) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Missing required file: {source}")

    content = source.read_text(encoding="utf-8")
    content = content.replace("TryExec=simplechart", f"TryExec={executable}")
    content = content.replace("Exec=simplechart", f"Exec={executable}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")


def _update_desktop_database(data_home: Path) -> None:
    applications_dir = data_home / "applications"
    if shutil.which("update-desktop-database") is None:
        return
    subprocess.run(
        ["update-desktop-database", str(applications_dir)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _update_icon_cache(data_home: Path) -> None:
    icons_root = data_home / "icons" / "hicolor"
    if shutil.which("gtk-update-icon-cache") is None:
        return
    subprocess.run(
        ["gtk-update-icon-cache", "-f", "-t", str(icons_root)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    data_home = _xdg_data_home()
    executable = _resolve_simplechart_executable()

    _install_desktop_file(DESKTOP_SOURCE, _desktop_destination(), executable)
    _install_icon(ICON_SOURCE, _icon_destination())

    _update_desktop_database(data_home)
    _update_icon_cache(data_home)

    print(f"Installed desktop entry to {_desktop_destination()}")
    print(f"Installed icon to {_icon_destination()}")
    print(f"Launcher command: {executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
