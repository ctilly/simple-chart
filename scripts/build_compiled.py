"""
scripts/build_compiled.py

Compiles the modules listed in pyproject.toml [tool.simplechart.compile]
to native C extensions using mypyc.

Run from the project root:
    python scripts/build_compiled.py

What it does:
  1. Reads the target module paths from pyproject.toml
  2. Converts dotted module paths to file paths (e.g. indicators._fast.ma
     → indicators/_fast/ma.py)
  3. Runs mypyc on all targets in one invocation

The compiled .so files are placed alongside the .py source files.
Python automatically imports the .so if present, falling back to the
.py if not — so you can delete the .so files at any time to revert to
interpreted mode.

To skip compilation during development:
    SIMPLECHART_NO_COMPILE=1 python main.py
    (main.py doesn't call this script; the flag is a convention for
    Makefiles or CI pipelines that might invoke the build step)
"""

import subprocess
import sys
from pathlib import Path

# Project root is one level up from this script.
ROOT = Path(__file__).parent.parent


def read_targets() -> list[str]:
    """Read compile targets from pyproject.toml."""
    try:
        import tomllib
    except ImportError:
        # tomllib is in the stdlib from Python 3.11+. If somehow running
        # an older Python, fall back to tomli.
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            print("Error: tomllib not available. Requires Python 3.11+.")
            sys.exit(1)

    toml_path = ROOT / "pyproject.toml"
    with open(toml_path, "rb") as f:
        config = tomllib.load(f)

    targets = (
        config
        .get("tool", {})
        .get("simplechart", {})
        .get("compile", {})
        .get("targets", [])
    )

    if not targets:
        print("No compile targets found in pyproject.toml. Nothing to do.")
        sys.exit(0)

    return targets


def module_to_path(module: str) -> Path:
    """Convert a dotted module path to a .py file path relative to ROOT."""
    return ROOT / Path(module.replace(".", "/")).with_suffix(".py")


def main() -> None:
    targets = read_targets()

    # Verify all target files exist before invoking mypyc.
    missing = [t for t in targets if not module_to_path(t).exists()]
    if missing:
        for m in missing:
            print(f"Error: source file not found for target '{m}'")
            print(f"  Expected: {module_to_path(m)}")
        sys.exit(1)

    print(f"Compiling {len(targets)} module(s) with mypyc:")
    for t in targets:
        print(f"  {t}")

    # mypyc is invoked as a module so it uses the same Python interpreter
    # that is running this script (important in virtual environments).
    cmd = [sys.executable, "-m", "mypyc"] + targets
    result = subprocess.run(cmd, cwd=ROOT)

    if result.returncode == 0:
        print("\nBuild succeeded.")
        print("Run 'python main.py' to launch with compiled extensions.")
    else:
        print("\nBuild failed. See mypyc output above for details.")
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
