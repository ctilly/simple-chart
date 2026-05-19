:; # ----------------------------------------------------------------------
:; # POSIX guard: if a Mac/Linux user runs this in bash, exit gracefully.
:; # The ':' is a no-op in sh, and '; #' comments out the rest of the line.
:; # Windows cmd.exe ignores the leading ':;' lines as labels.
:; echo ""
:; echo "  =============================================================="
:; echo "   This installer is for Windows only."
:; echo "   You appear to be running it in a Unix shell (macOS / Linux)."
:; echo ""
:; echo "   Please use the macOS/Linux installer (setup.sh) instead."
:; echo "  =============================================================="
:; echo ""
:; exit 1

@echo off
setlocal enabledelayedexpansion

REM ----------------------------------------------------------------------
REM Force UTF-8 so the terminal handles any unicode in output cleanly.
REM (We still use plain ASCII for our own status markers below, so they
REM  render correctly regardless of the user's console font.)
REM ----------------------------------------------------------------------
chcp 65001 >nul 2>&1

REM Operate from the directory this .bat lives in, not from wherever
REM the user happened to double-click from.
cd /d "%~dp0"

title Application Setup

echo.
echo  ==============================================================
echo                      Application Setup
echo  ==============================================================
echo.
echo  [i] This will install everything needed to run the application.
echo  [i] It may take a few minutes on the first run.
echo.

REM ======================================================================
REM Step 1: Check if 'uv' is already globally available on PATH.
REM ======================================================================
echo  [i] Step 1 of 5: Checking for 'uv' package manager...

set "UV_EXE="

where uv >nul 2>&1
if !errorlevel! equ 0 (
    set "UV_EXE=uv"
    echo  [OK] Found 'uv' on system PATH.
    goto :create_venv
)

echo  [i] 'uv' not found on PATH. Proceeding to install it...
echo.

REM ======================================================================
REM Step 2: Download and install 'uv' using Astral's official installer.
REM
REM This uses the EXACT command from the official uv documentation:
REM   powershell -ExecutionPolicy ByPass -c "irm <url> | iex"
REM
REM IMPORTANT: We deliberately do NOT use 'curl | powershell -Command -'.
REM Piping a script into PowerShell via stdin on Windows is unreliable:
REM the script can be truncated by buffering, and execution policy is
REM enforced before stdin is even read. Using -ExecutionPolicy ByPass and
REM letting PowerShell itself download via 'irm' is the supported path.
REM ======================================================================
echo  [i] Step 2 of 5: Installing the 'uv' tool itself...
echo      (This step only installs uv, the tool that will then
echo      download Python and your application in later steps.
echo      Output from the uv installer is shown below.)
echo.
echo  --- begin output from Astral's uv installer --------------------
echo.

powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
set "INSTALL_RC=!errorlevel!"

echo.
echo  --- end output from Astral's uv installer ----------------------
echo.
echo  [i] Note: the installer's "everything's installed!" message
echo      above refers only to the uv tool itself. Python and your
echo      application have NOT been installed yet -- that happens in
echo      Steps 4 and 5 below.
echo.

if !INSTALL_RC! neq 0 (
    echo  [X] ERROR: The uv installer reported exit code !INSTALL_RC!.
    echo.
    echo      Possible causes:
    echo        - No internet connection
    echo        - A firewall or antivirus is blocking the download
    echo        - PowerShell execution is restricted by Group Policy
    echo        - astral.sh is unreachable from your network
    echo.
    echo      Please check your connection and try again. If your
    echo      organization restricts PowerShell, contact your IT
    echo      administrator or install uv manually from:
    echo        https://docs.astral.sh/uv/getting-started/installation/
    echo.
    pause
    exit /b 1
)

echo  [OK] 'uv' tool is now on your computer.
echo.

REM ======================================================================
REM Step 3: Self-healing path check.
REM
REM Windows has a well-known PATH propagation lag: a fresh install in the
REM same cmd.exe session won't make 'uv' available via 'where uv' until a
REM new shell is opened. So we look for the binary directly. The official
REM uv installer places the binary at %USERPROFILE%\.local\bin\uv.exe;
REM we check a few legacy / alternate locations as well, and finally fall
REM back to a recursive search of the user profile.
REM ======================================================================
echo  [i] Step 3 of 5: Locating freshly installed 'uv.exe'...

set "UV_CANDIDATE_1=%USERPROFILE%\.local\bin\uv.exe"
set "UV_CANDIDATE_2=%APPDATA%\astral\uv\uv.exe"
set "UV_CANDIDATE_3=%LOCALAPPDATA%\Programs\uv\uv.exe"
set "UV_CANDIDATE_4=%APPDATA%\uv\uv.exe"
set "UV_CANDIDATE_5=%USERPROFILE%\.cargo\bin\uv.exe"

if exist "!UV_CANDIDATE_1!" (
    set "UV_EXE=!UV_CANDIDATE_1!"
    echo  [OK] Found uv at: !UV_EXE!
    goto :create_venv
)

if exist "!UV_CANDIDATE_2!" (
    set "UV_EXE=!UV_CANDIDATE_2!"
    echo  [OK] Found uv at: !UV_EXE!
    goto :create_venv
)

if exist "!UV_CANDIDATE_3!" (
    set "UV_EXE=!UV_CANDIDATE_3!"
    echo  [OK] Found uv at: !UV_EXE!
    goto :create_venv
)

if exist "!UV_CANDIDATE_4!" (
    set "UV_EXE=!UV_CANDIDATE_4!"
    echo  [OK] Found uv at: !UV_EXE!
    goto :create_venv
)

if exist "!UV_CANDIDATE_5!" (
    set "UV_EXE=!UV_CANDIDATE_5!"
    echo  [OK] Found uv at: !UV_EXE!
    goto :create_venv
)

REM Maybe PATH did update in this session after all.
where uv >nul 2>&1
if !errorlevel! equ 0 (
    set "UV_EXE=uv"
    echo  [OK] Found 'uv' on system PATH.
    goto :create_venv
)

REM ---- Last resort: recursive search of %USERPROFILE% for uv.exe ----
echo  [i] Standard locations didn't have uv.exe. Searching your user
echo      folder (this may take 10-20 seconds)...

for /f "delims=" %%F in ('where /r "%USERPROFILE%" uv.exe 2^>nul') do (
    if not defined UV_EXE (
        set "UV_EXE=%%F"
    )
)

if defined UV_EXE (
    echo  [OK] Found uv at: !UV_EXE!
    goto :create_venv
)

echo.
echo  [X] ERROR: The installer ran but 'uv.exe' is nowhere on disk.
echo.
echo      This usually means antivirus quarantined the binary, or the
echo      installer was blocked by Group Policy and exited silently.
echo.
echo      WHAT TO DO:
echo        1. Check Windows Defender / your antivirus quarantine.
echo        2. Try installing manually. Open PowerShell and run:
echo               powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
echo        3. If that fails, install via WinGet:
echo               winget install --id=astral-sh.uv -e
echo.
pause
exit /b 1

REM ======================================================================
REM Step 4: Create a Python 3.13 virtual environment.
REM
REM Quote handling: if UV_EXE is the bare string "uv" (resolved via PATH)
REM we must NOT wrap it in quotes. If UV_EXE is a full path (which may
REM contain spaces such as "C:\Users\John Doe\..."), we MUST wrap it in
REM quotes.
REM ======================================================================
:create_venv
echo.
echo  [i] Step 4 of 5: Setting up Python 3.13...
echo      (If Python 3.13 isn't already on your computer, uv will
echo      download it now. This can take a minute or two.
echo      uv's output is shown below.)
echo.
echo  --- begin uv venv output ---------------------------------------
echo.

if "!UV_EXE!"=="uv" (
    uv venv --python 3.13
) else (
    "!UV_EXE!" venv --python 3.13
)
set "VENV_RC=!errorlevel!"

echo.
echo  --- end uv venv output -----------------------------------------
echo.

if !VENV_RC! neq 0 (
    echo  [X] ERROR: Failed to create the virtual environment.
    echo.
    echo      'uv' could not set up Python 3.13. This may indicate
    echo      a network issue while downloading Python, or a disk
    echo      permissions problem in this folder.
    echo.
    pause
    exit /b 1
)

echo  [OK] Python 3.13 is ready.
echo.

REM ======================================================================
REM Step 5: Install the project and its dependencies in editable mode.
REM ======================================================================
echo  [i] Step 5 of 5: Installing application and dependencies...
echo      (This may take a few minutes the first time.
echo      uv's output is shown below.)
echo.
echo  --- begin uv pip install output --------------------------------
echo.

if "!UV_EXE!"=="uv" (
    uv pip install -e .
) else (
    "!UV_EXE!" pip install -e .
)
set "PIP_RC=!errorlevel!"

echo.
echo  --- end uv pip install output ----------------------------------
echo.

if !PIP_RC! neq 0 (
    echo  [X] ERROR: Failed to install application dependencies.
    echo.
    echo      Common causes:
    echo        - No internet connection during package download
    echo        - A missing or malformed 'pyproject.toml' in this folder
    echo        - A dependency conflict
    echo.
    echo      Please review the messages above for details.
    echo.
    pause
    exit /b 1
)

echo  [OK] Application and all dependencies installed.

REM ======================================================================
REM All done!
REM ======================================================================
echo.
echo  ==============================================================
echo   [OK] Setup complete! Everything is now installed.
echo  ==============================================================
echo.
echo   The application has been installed successfully.
echo   You can now close this window.
echo.
pause
endlocal
exit /b 0