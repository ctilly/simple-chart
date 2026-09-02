import json
import os
import subprocess
import sys
import sysconfig
import tomllib
from pathlib import Path

from simplechart.claude_qc.packet import PACKET_FIELDS


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _packet_raw() -> bytes:
    values = {field: f"synthetic {field}" for field in PACKET_FIELDS}
    values.update(
        {
            "schema_version": "1",
            "review_kind": "design",
            "implementation_diff": "",
            "verification_summary": "",
        }
    )
    return json.dumps(values).encode()


def test_pyproject_registers_separate_claude_qc_executable() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        configuration = tomllib.load(stream)

    scripts = configuration["project"]["scripts"]
    assert scripts["simplechart-claude-qc"] == "simplechart.claude_qc.cli:main"


def test_module_preview_smoke_isolated_from_application_startup() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "simplechart.claude_qc.cli", "preview"],
        input=_packet_raw(),
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        timeout=10.0,
    )

    envelope = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert completed.stderr == b""
    assert envelope["status"] == "complete"
    assert envelope["category"] == "preview"


def test_generated_console_script_runs_preview_from_isolated_install(
    tmp_path: Path,
) -> None:
    environment = tmp_path / "environment"
    installed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "--ignore-installed",
            "--prefix",
            str(environment),
            str(PROJECT_ROOT),
        ],
        capture_output=True,
        check=False,
        timeout=60.0,
    )
    assert installed.returncode == 0, installed.stderr.decode(errors="replace")

    scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
    executable = scripts / (
        "simplechart-claude-qc.exe"
        if sys.platform == "win32"
        else "simplechart-claude-qc"
    )
    completed = subprocess.run(
        [str(executable), "preview"],
        input=_packet_raw(),
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": sysconfig.get_path(
                "purelib",
                vars={"base": str(environment), "platbase": str(environment)},
            ),
        },
        capture_output=True,
        check=False,
        timeout=10.0,
    )

    envelope = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert completed.stderr == b""
    assert envelope["status"] == "complete"
    assert envelope["category"] == "preview"


def test_normal_package_import_does_not_import_claude_qc_cli() -> None:
    script = (
        "import sys; import simplechart; "
        "assert 'simplechart.claude_qc.cli' not in sys.modules"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        timeout=10.0,
    )

    assert completed.returncode == 0
    assert completed.stderr == b""


def test_preview_handler_does_not_import_application_or_provider_layers() -> None:
    script = r'''
import importlib.abc
import sys

blocked = (
    "app",
    "chart",
    "data",
    "indicators",
    "tools",
    "PyQt6",
    "sqlite3",
    "simplechart.plugins",
)

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise RuntimeError("blocked application import: " + fullname)
        return None

assert not any(
    name == blocked_name or name.startswith(blocked_name + ".")
    for name in sys.modules
    for blocked_name in blocked
)
sys.meta_path.insert(0, Blocker())
from simplechart.claude_qc import cli
sys.argv = ["simplechart-claude-qc", "preview"]
raise SystemExit(cli.main())
'''

    completed = subprocess.run(
        [sys.executable, "-c", script],
        input=_packet_raw(),
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        timeout=10.0,
    )

    envelope = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert completed.stderr == b""
    assert envelope["category"] == "preview"
