![The Simple Chart UI](img/simple-chart2.png?v=1)

# Simple Chart

Simple Chart is a free desktop stock charting application for swing traders with a 
clean and intuitive UI.  No more paying for a subscription to use Anchored VWAP or 
limited configuration indicators. The app comes with customizable indicators and
drawing tools, along with a robust API for creating
[custom extensions](#custom-extensions) using the AI agent of your choice.

Included indicators:

- Simple Moving Average
- Exponential Moving Average
- Anchored VWAP
- Pivot Points
- RSI

Included drawing tools:

- Vertical Line
- Fibonacci Retracement

For usage guidance, see the [Simple Chart Help Guide](docs/help.md).

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


## MacOS and Linux Setup

For macOS and Linux, the easiest path is the included `setup.sh` file. It
installs `uv` if needed, creates a Python 3.13 virtual environment, and installs
Simple Chart in that environment.

On macOS, setup also creates **Simple Chart.app** on your desktop with the
app icon. Double-click it to launch without a terminal window. Keep the project
folder in place; if you move it, rerun setup to refresh the environment and
launcher. You can also drag the launcher to the Dock.

From the project folder, run:

```bash
sh setup.sh
```

To launch on Linux, or on macOS with terminal diagnostics:

```bash
. .venv/bin/activate
simplechart
```

Desktop launches discard routine output rather than writing a growing log file.
Launching `simplechart` from a terminal keeps diagnostic output visible.
For an existing macOS installation, create or refresh just the launcher with:

```bash
.venv/bin/python scripts/install_macos_app.py
```


## Windows Setup

For Windows, the easiest path is the included `setup.bat` file. It installs
`uv` if needed, creates a Python 3.13 virtual environment, and installs Simple
Chart in that environment, then creates a desktop shortcut with the app icon.

1. Clone or download this repository.
2. Open the project folder in File Explorer.
3. Double-click `setup.bat`.
4. Wait until it says setup is complete.  A shortcut will appear on your desktop.
5. Double-click the **Simple Chart** desktop shortcut to launch the app.

Keep the project folder in place after setup. If you move it, rerun `setup.bat`
to refresh the environment and shortcut. 



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

## Credential Security

Provider API credentials are stored only in the current user's operating-
system credential store: Windows Credential Locker, macOS Keychain, or a
supported Linux keyring. SimpleChart does not store credentials in its SQLite
database, environment variables, config files, command-line arguments, or an
automatic plaintext or in-memory fallback.

If secure credential storage or a provider dependency is unavailable, that
provider is disabled and Yahoo remains available. Each operating-system user
and computer must configure its own provider credentials.

See [Credential Security Contract](docs/credential-security.md) for the full
storage and provider implementation rules.

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

The selected provider supplies equity and cryptocurrency market data. Futures
symbols ending in `=F` always use Yahoo Finance. Company and fund names are
also resolved through Yahoo Finance, stored in the provider-neutral SQLite
reference table, and refreshed automatically after 30 days. A failed refresh
keeps the last known name and is retried later.

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

If the desktop launcher fails during startup, it displays an error dialog and
writes stack locations to `~/.simplechart/startup-error.log` on macOS or
`%USERPROFILE%\.simplechart\startup-error.log` on Windows.
Exception messages and local variables are omitted to keep credentials out of
the diagnostic file.

Launch the app from a terminal so any error message stays visible. Common causes
are an incomplete dependency install, running outside a desktop environment, or
using the wrong Python environment.

### Windows setup cannot install `uv`

The setup script downloads `uv` using PowerShell. If that step fails, the most
common causes are no internet connection, antivirus quarantine, or organization
policy blocking PowerShell downloads. The script prints manual install options
when this happens.

## Custom Extensions

Simple Chart includes a public extension API at `simplechart.api`. Custom
indicators can be added as project plugins under `indicators/`; drawing tools
live under `tools/`. Plain user plugins can be added under
`~/.simplechart/plugins/`.

For extension development, install the development extras:

```bash
python -m pip install -e ".[dev]"
```

Then read:

- `AGENTS.md` for project conventions and the public extension API contract
- `docs/skills/compute-indicator.md` for standard computed indicators
- `docs/skills/interactive-indicator.md` for interactive indicators with drag
  handles, context actions, and persistent state (e.g. Anchored VWAP)
- `docs/skills/drawing-tool.md` for toolbar drawing tools (vertical line,
  Fibonacci retracement) with per-tool timeframe/session persistence

## Development Notes

This repository is intentionally source-installed. It does not currently ship
native installers. Windows setup creates a desktop shortcut; macOS setup creates
a small `.app` launcher. Both use the source installation and its virtual
environment rather than bundling Python or dependencies.

Useful commands:

```bash
python -m pytest
python -m mypy app chart data simplechart tools indicators tests
python -m ruff check .
python -m compileall app chart data indicators simplechart tools tests
python scripts/build_compiled.py
```

For periodic dead-code review, run `python -m vulture main.py app chart data simplechart indicators tools tests` and review the findings manually; Qt callbacks, finplot dynamic attributes, and public extension APIs can appear as false positives.

Compiled `.so` extension files are build artifacts generated by mypyc for hot
numeric indicator kernels. The Python source files remain the source of truth.

## License

This project is licensed under the PolyForm Noncommercial License 1.0.0. It is free to use for personal, educational, and noncommercial purposes. Commercial use, repackaging, or selling of this software is strictly prohibited. See the [LICENSE](LICENSE) file for the full text.
