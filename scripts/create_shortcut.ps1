$ErrorActionPreference = "Stop"
try {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
    $DesktopPath = [Environment]::GetFolderPath("Desktop")
    $LauncherPath = Join-Path $ProjectRoot ".venv\Scripts\simplechart-gui.exe"
    $IconPath = Join-Path $ProjectRoot "assets\simple-chart-light.ico"
    if (-not (Test-Path -LiteralPath $LauncherPath -PathType Leaf)) {
        throw "GUI launcher missing. Run setup.bat to install Simple Chart first."
    }
    if (-not (Test-Path -LiteralPath $IconPath -PathType Leaf)) {
        throw "Shortcut icon missing. Restore assets\simple-chart.ico and rerun setup.bat."
    }
    if ([string]::IsNullOrWhiteSpace($DesktopPath) -or
        -not (Test-Path -LiteralPath $DesktopPath -PathType Container)) {
        throw "The current user's Desktop folder could not be found."
    }
    $ShortcutPath = Join-Path $DesktopPath "Simple Chart.lnk"
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $LauncherPath
    $Shortcut.Arguments = ""
    $Shortcut.WorkingDirectory = $ProjectRoot
    $Shortcut.IconLocation = "$IconPath,0"
    $Shortcut.Description = "Launch Simple Chart"
    $Shortcut.Save()
    Write-Host "Desktop shortcut created at: $ShortcutPath" -ForegroundColor Green
} catch {
    [Console]::Error.WriteLine("Could not create the Simple Chart shortcut: " + $_.Exception.Message)
    exit 1
}
