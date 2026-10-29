import tempfile
import tomllib
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from simplechart import gui_launcher


class GuiLauncherTests(unittest.TestCase):
    def test_entry_point_calls_application(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["project"]["gui-scripts"]["simplechart-gui"], "simplechart.gui_launcher:main")
        application = ModuleType("main")
        run = Mock()
        application.main = run  # type: ignore[attr-defined]
        with patch.dict("sys.modules", {"main": application}):
            gui_launcher.main()
        run.assert_called_once_with()

    def test_failure_diagnostics_omit_secret(self) -> None:
        application = ModuleType("main")
        application.main = Mock(side_effect=RuntimeError("synthetic-secret"))  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.dict("sys.modules", {"main": application}),
                patch.object(Path, "home", return_value=Path(directory)),
                patch.object(gui_launcher.sys, "platform", "win32"),
                patch.object(gui_launcher.ctypes, "windll", create=True) as native,
                self.assertRaises(SystemExit) as caught,
            ):
                gui_launcher.main()
            self.assertEqual(caught.exception.code, 1)
            log = (Path(directory) / ".simplechart" / "startup-error.log").read_text()
            self.assertIn("gui_launcher.py", log)
            self.assertNotIn("synthetic-secret", log)
            native.user32.MessageBoxW.assert_called_once()
            self.assertNotIn("synthetic-secret", str(native.user32.MessageBoxW.call_args))

    def test_normal_exit_is_preserved(self) -> None:
        application = ModuleType("main")
        application.main = Mock(side_effect=SystemExit(0))  # type: ignore[attr-defined]
        with (
            patch.dict("sys.modules", {"main": application}),
            patch.object(Path, "home") as home,
            self.assertRaises(SystemExit) as caught,
        ):
            gui_launcher.main()
        self.assertEqual(caught.exception.code, 0)
        home.assert_not_called()

    @unittest.skipUnless(__import__("sys").platform == "win32", "Windows shortcut integration")
    def test_production_shortcut_helper(self) -> None:
        import subprocess

        root = Path(__file__).resolve().parents[1]
        script = r'''
$ErrorActionPreference = 'Stop'
$script:shortcut = [pscustomobject]@{
    TargetPath = ''; Arguments = ''; WorkingDirectory = ''
    IconLocation = ''; Description = ''; Saved = $false
}
$script:shortcut | Add-Member ScriptMethod Save { $this.Saved = $true }
function Test-Path { param($LiteralPath, $PathType) return $true }
function New-Object {
    param($ComObject)
    if ($ComObject -ne 'WScript.Shell') { throw 'Unexpected COM request' }
    $shell = [pscustomobject]@{}
    $shell | Add-Member ScriptMethod CreateShortcut { param($path) return $script:shortcut }
    return $shell
}
. ./scripts/create_shortcut.ps1
if (-not $script:shortcut.Saved) { throw 'Shortcut not saved' }
if ($script:shortcut.TargetPath -ne (Join-Path $PWD '.venv\Scripts\simplechart-gui.exe')) { throw 'Wrong launcher' }
if ($script:shortcut.WorkingDirectory -ne $PWD.Path) { throw 'Wrong working directory' }
if ($script:shortcut.IconLocation -ne ((Join-Path $PWD 'assets\simple-chart.ico') + ',0')) { throw 'Wrong icon' }
if ($script:shortcut.Arguments -ne '') { throw 'Unexpected arguments' }
'''
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            cwd=root, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        setup = (root / "setup.bat").read_text()
        self.assertLess(setup.index("if !PIP_RC! neq 0"), setup.index('-File "%~dp0scripts\\create_shortcut.ps1"'))
        self.assertLess(setup.index('-File "%~dp0scripts\\create_shortcut.ps1"'), setup.index('[OK] Setup complete!'))
