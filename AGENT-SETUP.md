# Agent Setup Notes

This repository is installed from source, not packaged as a native installer.

## Default Agent Workflow

1. Clone the repository.
2. Read `README.md`.
3. Read `pyproject.toml`.
4. Create a Python 3.13 virtual environment.
5. Install with `python -m pip install -e .` for normal app use, or
   `python -m pip install -e ".[dev]"` if you will build indicators, run tests,
   type-check, or compile kernels.
6. Launch with `simplechart`.
7. On Linux only, if the user wants a menu launcher, run:
   `python scripts/install_linux_desktop.py`

## Dev install — when to use it

Use `pip install -e ".[dev]"` whenever you will:

- build a custom indicator (project plugin, or any user plugin that needs a
  compiled kernel)
- run `pytest`
- run `mypy`
- compile kernels via `scripts/build_compiled.py`

The dev extras add:

- `pytest` and `pytest-qt` — verification
- `mypy` — type checking and mypyc compilation
- `setuptools` — required by the mypyc build step in
  `scripts/build_compiled.py`

The normal install is sufficient for plain `.py` user plugins dropped into
`~/.simplechart/plugins/` that do not need compiled kernels or tests.

## Agent Rules

- Prefer the documented install flow.
- Do not introduce PyInstaller, py2app, cx_Freeze, or other packaging tools
  unless the user explicitly asks for packaging work.
- Do not hard-code user-specific paths into source files.
- Keep platform-specific changes minimal and explain them before applying them.
- If setup fails, fix the smallest concrete issue blocking launch on that host.

## Important Files

- `README.md` — human-facing install and troubleshooting instructions
- `AGENTS.md` — project conventions, domain vocabulary, and the
  `simplechart.api` contract; read this before editing code
- `pyproject.toml` — dependencies, dev extras, mypyc compile targets, and the
  `simplechart` CLI entry point
- `docs/skills/compute-indicator.md` — step-by-step skill file for compute
  indicators
- `docs/skills/interactive-indicator.md` — step-by-step skill file for
  interactive indicators (drag handles, context actions, persistent state)
- `scripts/install_linux_desktop.py` — optional Linux desktop integration that
  writes the installed launcher with the active environment's `simplechart`
  path
- `io.simplechart.SimpleChart.desktop` — generic desktop entry used by the
  Linux install helper
