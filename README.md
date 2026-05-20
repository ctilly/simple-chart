![The Simple Chart UI](img/simple-chart.png?v=1)

# Simple Chart

Simple Chart is a free desktop stock charting application for swing traders with a 
clean and intuitive UI.  No more paying for a subscription to use Anchored VWAP or 
limited configuration indicators. The app comes with a set of core indicators that
are fully customizable, along with a robust API for creating [custom indicators](#custom-indicators) 
using the AI agent of your choice. 

Included indicators:

- Simple Moving Average
- Exponential Moving Average
- Anchored VWAP
- Pivot Points
- Fibonacci Retracement
- RSI

The app runs locally from this source repository on Linux, MacOS, and Windows.
It uses Python 3.13, and was built natively on Linux and seems to work well on
both MacOS and Windows.

## Quick Start

The setup process has four basic steps:

1. Create a virtual environment with Python 3.13.
2. Clone this repository.
3. Install the app into the virtual environment.
4. Launch Simple Chart.

Windows users can use `setup.bat`. MacOS and Linux users can use `setup.sh`.
Manual setup commands are also included below.

## Windows Setup

For Windows, the easiest path is the included `setup.bat` file. It installs
`uv` if needed, creates a Python 3.13 virtual environment, and installs Simple
Chart in that environment.

1. Clone or download this repository.
2. Open the project folder in File Explorer.
3. Double-click `setup.bat`.
4. Wait until it says setup is complete.

To launch the app after setup, open PowerShell in the project folder and run:

```powershell
.\.venv\Scripts\simplechart.exe
```

If you prefer to activate the environment first:

```powershell
.\.venv\Scripts\Activate.ps1
simplechart
```

If PowerShell blocks activation scripts, use the direct launch command instead:

```powershell
.\.venv\Scripts\simplechart.exe
```

## MacOS and Linux Setup

For macOS and Linux, the easiest path is the included `setup.sh` file. It
installs `uv` if needed, creates a Python 3.13 virtual environment, and installs
Simple Chart in that environment.

From the project folder, run:

```bash
sh setup.sh
```

To launch the app after setup:

```bash
. .venv/bin/activate
simplechart
```

## Manual Setup

Use this path if you prefer managing the environment yourself.

First, create and activate a Python 3.13 virtual environment.

macOS and Linux:

```bash
python3.13 -m venv .venv
. .venv/bin/activate
```

Windows PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Then install and launch the app:

```bash
python -m pip install --upgrade pip
python -m pip install -e .
simplechart
```

If `python3.13` or `py -3.13` is not available, install Python 3.13 first or use
a Python version manager such as `pyenv` or `uv`.

With `uv`, the setup can be as short as:

```bash
uv venv --python 3.13
uv pip install -e .
```

Then activate the environment and launch:

```bash
. .venv/bin/activate
simplechart
```

On Windows, you can launch the `uv` environment directly:

```powershell
.\.venv\Scripts\simplechart.exe
```

## Launch Options

By default, Simple Chart stores its local database here:

- macOS and Linux: `~/.simplechart/simplechart.db`
- Windows: `%USERPROFILE%\.simplechart\simplechart.db`

To use a different database file:

```bash
simplechart --db /path/to/simplechart.db
```

The default data provider is `yfinance`:

```bash
simplechart --provider yfinance
```

## Optional Linux Launcher

Running from a terminal after `pip install -e .` is enough to use the app.

On Linux, you can also install a desktop menu launcher:

```bash
python scripts/install_linux_desktop.py
```

Run that command from the same virtual environment where Simple Chart was
installed. If the launcher does not appear immediately, log out and back in or
restart the desktop shell.

## Troubleshooting

### `simplechart` command not found

The virtual environment is probably not active, or the install did not finish.
Activate the environment and reinstall:

```bash
python -m pip install -e .
```

On Windows, you can also launch directly without activating:

```powershell
.\.venv\Scripts\simplechart.exe
```

### Wrong Python version

Simple Chart requires Python 3.13. Check the active Python version:

```bash
python --version
```

If the version is not 3.13, create a new virtual environment using Python 3.13
and reinstall the app.

### The chart window does not open

Launch the app from a terminal so any error message stays visible. Common causes
are an incomplete dependency install, running outside a desktop environment, or
using the wrong Python environment.

### Windows setup cannot install `uv`

The setup script downloads `uv` using PowerShell. If that step fails, the most
common causes are no internet connection, antivirus quarantine, or organization
policy blocking PowerShell downloads. The script prints manual install options
when this happens.

## Custom Indicators

Simple Chart includes a public indicator API at `simplechart.api`. Custom
indicators can be added as project plugins under `indicators/` or as user
plugins under `~/.simplechart/plugins/`.

For indicator development, install the development extras:

```bash
python -m pip install -e ".[dev]"
```

Then read:

- `AGENTS.md` for project conventions and the public indicator API contract
- `docs/skills/compute-indicator.md` for standard computed indicators
- `docs/skills/interactive-indicator.md` for drawing tools, drag handles,
  context actions, and persistent indicator state

## Development Notes

This repository is intentionally source-installed. It does not currently ship
native installers, a macOS `.app` bundle, or a Windows shortcut installer.

Useful commands:

```bash
python -m pytest
python -m compileall app chart data indicators simplechart tests
python scripts/build_compiled.py
```

Compiled `.so` extension files are build artifacts generated by mypyc for hot
numeric indicator kernels. The Python source files remain the source of truth.
