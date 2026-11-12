import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
KERNELS = (
    "indicators.ema._kernel",
    "indicators.rsi._kernel",
)


def _load_build_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_compiled_under_test", ROOT / "scripts" / "build_compiled.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_each_target_is_compiled_in_its_own_mypyc_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = _load_build_script()
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
        assert cwd == build.ROOT
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(build.subprocess, "run", fake_run)
    build.main()

    compiled = [cmd[3:] for cmd in calls]
    assert compiled == [
        [str(Path(*target.split(".")).with_suffix(".py"))] for target in KERNELS
    ]


def _compiled_kernels_present() -> bool:
    return all(
        any((ROOT / Path(*target.split(".")[:-1])).glob("_kernel.*.so"))
        for target in KERNELS
    )


@pytest.mark.skipif(
    not _compiled_kernels_present(), reason="compiled kernels are not built"
)
def test_compiled_kernels_import_without_project_root_on_path(
    tmp_path: Path,
) -> None:
    # The console-script launcher does not put the project root on sys.path;
    # -I drops the current directory and PYTHONPATH the same way.
    result = subprocess.run(
        [sys.executable, "-I", "-c", "import " + ", ".join(KERNELS)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
