# Stop only this project's verified bot process.
$ErrorActionPreference = "Stop"
$botPath = Join-Path $PSScriptRoot "bot.py"
$listener = Get-NetTCPConnection -LocalPort 17890 -State Listen -ErrorAction SilentlyContinue
if (-not $listener) {
    Write-Output "Bot is not running."
    exit 0
}
$botProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
$scriptPattern = '(?i)(?:^|[\s"])' + [regex]::Escape($botPath) + '(?=$|[\s"])'
if (-not $botProcess -or $botProcess.Name -notmatch '^pythonw?\.exe$' -or $botProcess.CommandLine -notmatch $scriptPattern) {
    throw "Port 17890 belongs to another process. Nothing was stopped."
}
Stop-Process -Id $botProcess.ProcessId -ErrorAction Stop
Write-Output "Bot stopped (PID $($botProcess.ProcessId))."
