"""Create a Finder launcher for the source installation."""

import plistlib
import shlex
import struct
import sys
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice, QRectF, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_icon(source: Path, destination: Path) -> None:
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise ValueError("App icon could not be loaded.")
    chunks = bytearray()
    for kind, size in ((b"icp4", 16), (b"icp5", 32), (b"icp6", 64),
                       (b"ic07", 128), (b"ic08", 256), (b"ic09", 512),
                       (b"ic10", 1024)):
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise OSError("App icon could not be encoded.")
        png = bytes(buffer.data())
        chunks.extend(kind + struct.pack(">I", len(png) + 8) + png)
    destination.write_bytes(b"icns" + struct.pack(">I", len(chunks) + 8) + chunks)


def install_app(project_root: Path, destination: Path) -> None:
    launcher = project_root / ".venv" / "bin" / "simplechart-gui"
    icon = project_root / "assets" / "simple-chart.svg"
    if not launcher.is_file():
        raise FileNotFoundError("GUI launcher missing. Run setup.sh first.")
    if not icon.is_file():
        raise FileNotFoundError("App icon missing. Restore assets/simple-chart.svg.")
    contents = destination / "Contents"
    executable = contents / "MacOS" / "SimpleChart"
    resources = contents / "Resources"
    executable.parent.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    _write_icon(icon, resources / "SimpleChart.icns")
    executable.write_text(
        "#!/bin/sh\n"
        "exec </dev/null >/dev/null 2>&1\n"
        f"cd {shlex.quote(str(project_root))} || exit 1\n"
        f"exec {shlex.quote(str(launcher))}\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump({
            "CFBundleName": "Simple Chart",
            "CFBundleDisplayName": "Simple Chart",
            "CFBundleIdentifier": "io.simplechart.SimpleChart",
            "CFBundleExecutable": "SimpleChart",
            "CFBundlePackageType": "APPL",
            "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundleVersion": "1",
            "CFBundleIconFile": "SimpleChart.icns",
            "NSHighResolutionCapable": True,
        }, stream)


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("This launcher installer is for macOS only.")
    destination = Path.home() / "Desktop" / "Simple Chart.app"
    try:
        install_app(REPO_ROOT, destination)
    except (OSError, ValueError) as error:
        raise SystemExit(f"Could not create the Simple Chart launcher: {error}") from None
    print(f"Desktop launcher created at: {destination}")


if __name__ == "__main__":
    main()
