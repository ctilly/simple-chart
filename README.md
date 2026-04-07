# Simple Chart

Simple Chart is a desktop stock charting application for swing traders that use the AVWAP.

This repository is structured for source-based local installation on Linux,
macOS, and Windows. The intended flow is:

1. Clone the repo.
2. Create a Python 3.13 virtual environment.
3. Install with `pip install -e .`.
4. Launch with `simplechart`.

No hard-coded personal paths are required.

## Requirements

- Python 3.13
- `pip`
- A desktop environment capable of running PyQt6 applications
- Internet access if dependencies need to be downloaded during install

## Fast Path

### Linux and macOS

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
simplechart
```

If `python3.13` is not available, use whichever command on the machine resolves
to Python 3.13 and verify with:

```bash
python --version
```

### Windows PowerShell

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
simplechart
```

If `py -3.13` is unavailable, install Python 3.13 first or use an equivalent
launcher that targets Python 3.13.

## What Gets Installed

`pip install -e .` installs:

- Python dependencies from `pyproject.toml`
- the `simplechart` command defined by the project entry point

The application stores its local SQLite database in a per-user location by
default:

- Linux and macOS: `~/.simplechart/simplechart.db`
- Windows: `%USERPROFILE%\.simplechart\simplechart.db`

You can override that location:

```bash
simplechart --db /path/to/simplechart.db
```

## Linux Desktop Launcher

Running from a terminal after `pip install -e .` is enough to use the app.

If you also want a desktop launcher and icon in Linux application menus:

```bash
python scripts/install_linux_desktop.py
```

That script installs:

- `io.simplechart.SimpleChart.desktop`
- `assets/simple-chart.svg`

into the current user's XDG data directories. It also rewrites the installed
desktop file so `Exec` and `TryExec` point at the `simplechart` executable from
the environment where the script was run.

## Platform Notes

### Linux

- Use the fast path above.
- If a launcher icon does not appear immediately after installing the desktop
  entry, log out and back in or restart the shell session for the desktop.

### macOS

- Use the fast path above.
- This repo currently targets source-based execution, not a native `.app`
  bundle.
- If a future release needs a native app bundle, use PyInstaller or py2app.

### Windows

- Use the PowerShell fast path above.
- This repo currently targets source-based execution, not a native installer.
- If a future release needs a native installer or shortcut setup, use
  PyInstaller or another Windows packaging tool.

## Troubleshooting

### `simplechart` command not found

The virtual environment is probably not active, or the editable install did not
complete successfully. Activate the venv and rerun:

```bash
python -m pip install -e .
```

### Wrong Python version

This project requires Python 3.13. Check with:

```bash
python --version
```

### GUI does not launch

Common causes:

- running in a headless environment
- missing desktop GUI support
- dependency install failure earlier in the setup flow

Try launching from the same terminal where the install was performed so any
Python traceback remains visible.

## Notes for LLM-Assisted Setup

An agent installing this repository should:

1. Read `README.md`.
2. Read `pyproject.toml`.
3. Create a Python 3.13 virtual environment.
4. Run `python -m pip install -e .`.
5. Launch `simplechart`.
6. On Linux only, optionally run `python scripts/install_linux_desktop.py`.

Constraints for an agent:

- Prefer the documented source-install flow over introducing PyInstaller or
  other packaging changes.
- Do not rewrite project structure unless setup fails for a concrete,
  platform-specific reason.
- Make the minimum necessary platform-specific adjustments and explain them.
