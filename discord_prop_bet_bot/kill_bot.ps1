# Stop all Python processes running this project's bot.py.
# Usage: .\kill_bot.ps1

$ErrorActionPreference = "Stop"
$BotScript = Join-Path $PSScriptRoot "bot.py"

$targets = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -in @("python.exe", "python3.exe", "pythonw.exe") -and
        $_.CommandLine -and
        $_.CommandLine -like "*$BotScript*"
    }

if (-not $targets) {
    Write-Host "No bot processes found."
    exit 0
}

foreach ($proc in $targets) {
    Write-Host "Stopping PID $($proc.ProcessId): $($proc.CommandLine)"
    Stop-Process -Id $proc.ProcessId -Force
}

Write-Host "Stopped $($targets.Count) bot process(es)."
