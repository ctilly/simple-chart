# Agent Setup Notes

This repository is intended to be installed from source, not packaged into a
native installer during normal setup.

## Default Agent Workflow

1. Clone the repository.
2. Read `README.md`.
3. Read `pyproject.toml`.
4. Create a Python 3.13 virtual environment.
5. Install with `python -m pip install -e .`.
6. Launch with `simplechart`.
7. On Linux only, if the user wants a menu launcher, run:
   `python scripts/install_linux_desktop.py`

## Agent Rules

- Prefer the documented install flow.
- Do not introduce PyInstaller, py2app, cx_Freeze, or other packaging tools
  unless the user explicitly asks for packaging work.
- Do not hard-code user-specific paths into source files.
- Keep platform-specific changes minimal and explain them before applying them.
- If setup fails, fix the smallest concrete issue blocking launch on that host.

## Important Files

- `README.md`: human-facing install and troubleshooting instructions
- `pyproject.toml`: dependencies and the `simplechart` CLI entry point
- `scripts/install_linux_desktop.py`: optional Linux desktop integration that
  writes the installed launcher with the active environment's `simplechart`
  path
- `io.simplechart.SimpleChart.desktop`: generic desktop entry used by the Linux
  install helper
