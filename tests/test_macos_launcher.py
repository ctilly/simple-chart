import importlib.util
import os
import plistlib
import struct
import subprocess
from pathlib import Path

import pytest
from PyQt6.QtGui import QImage

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "install_macos_app", ROOT / "scripts" / "install_macos_app.py",
)
assert _SPEC is not None and _SPEC.loader is not None
installer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(installer)


def test_bundle_launches_gui_entry_point_quietly(tmp_path: Path) -> None:
    project = tmp_path / "a project's $folder"
    launcher = project / ".venv" / "bin" / "simplechart-gui"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        '#!/bin/sh\npwd > launched-from\necho routine-output\n'
        'echo routine-error >&2\nexit 7\n',
    )
    launcher.chmod(0o755)
    (project / "assets").mkdir()
    (project / "assets" / "simple-chart.svg").write_bytes(
        (ROOT / "assets" / "simple-chart.svg").read_bytes(),
    )
    bundle = tmp_path / "Desktop" / "Simple Chart.app"
    installer.install_app(project, bundle)
    # Re-running setup refreshes the same bundle.
    installer.install_app(project, bundle)
    contents = bundle / "Contents"
    info = plistlib.loads((contents / "Info.plist").read_bytes())
    assert info["CFBundlePackageType"] == "APPL"
    executable = contents / "MacOS" / info["CFBundleExecutable"]
    assert os.access(executable, os.X_OK)
    result = subprocess.run([str(executable)], cwd=tmp_path, capture_output=True, check=False)
    assert result.returncode == 7
    assert result.stdout == result.stderr == b""
    assert (project / "launched-from").read_text().strip() == str(project)
    # The same target retains output when invoked directly in a terminal.
    terminal = subprocess.run([str(launcher)], cwd=tmp_path, capture_output=True, check=False)
    assert terminal.stdout == b"routine-output\n"
    assert terminal.stderr == b"routine-error\n"
    icon = (contents / "Resources" / info["CFBundleIconFile"]).read_bytes()
    assert icon[:4] == b"icns"
    assert struct.unpack(">I", icon[4:8])[0] == len(icon)
    offset = 8
    sizes = []
    while offset < len(icon):
        length = struct.unpack(">I", icon[offset + 4:offset + 8])[0]
        image = QImage.fromData(icon[offset + 8:offset + length], "PNG")
        assert not image.isNull()
        sizes.append(image.width())
        offset += length
    assert sizes == [16, 32, 64, 128, 256, 512, 1024]


def test_missing_install_does_not_create_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "Simple Chart.app"
    with pytest.raises(FileNotFoundError, match="GUI launcher missing"):
        installer.install_app(tmp_path, bundle)
    assert not bundle.exists()


def test_setup_wires_macos_installer_after_package_install(tmp_path: Path) -> None:
    (tmp_path / "setup.sh").write_bytes((ROOT / "setup.sh").read_bytes())
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "uname").write_text('#!/bin/sh\necho Darwin\n')
    (binaries / "uname").chmod(0o755)
    (binaries / "uv").write_text('#!/bin/sh\necho "$*" >> setup-calls\n')
    (binaries / "uv").chmod(0o755)
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\necho "$*" >> setup-calls\nexit "${INSTALL_RC:-0}"\n')
    python.chmod(0o755)
    environment = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}"}
    result = subprocess.run(["sh", "setup.sh"], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert (tmp_path / "setup-calls").read_text().splitlines() == [
        "venv --python 3.13", "pip install -e .", "scripts/install_macos_app.py",
    ]
    assert "Double-click Simple Chart" in result.stdout
    failure = subprocess.run(["sh", "setup.sh"], cwd=tmp_path,
                             env={**environment, "INSTALL_RC": "1"},
                             capture_output=True, text=True, check=False)
    assert failure.returncode == 1
    assert "[OK] Setup complete!" not in failure.stdout
