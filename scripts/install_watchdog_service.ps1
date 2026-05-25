param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$NssmPath = "C:\Trading\nssm.exe",
    [string]$PythonPath = "",
    [string]$ServiceName = "AntWatchdog"
)

$ErrorActionPreference = "Stop"

function Assert-File {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Label niet gevonden: $Path"
    }
}

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}

$watchdogScript = Join-Path $ProjectRoot "scripts\watchdog.py"
$logRoot = "C:\Trading\ANT_LOGS"

Assert-File -Path $NssmPath -Label "NSSM"
Assert-File -Path $PythonPath -Label "Python runtime"
Assert-File -Path $watchdogScript -Label "watchdog.py"

if (-not (Test-IsAdmin)) {
    Write-Warning "Voer dit script uit in een Administrator PowerShell om de service te installeren."
}

if (-not (Test-Path -LiteralPath $logRoot)) {
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
}

$existing = $false
& sc.exe query $ServiceName *> $null
if ($LASTEXITCODE -eq 0) {
    $existing = $true
}

if (-not $existing) {
    & $NssmPath install $ServiceName $PythonPath $watchdogScript
}
else {
    Write-Host "$ServiceName bestaat al; configuratie wordt bijgewerkt."
    & $NssmPath set $ServiceName Application $PythonPath | Out-Null
    & $NssmPath set $ServiceName AppParameters $watchdogScript | Out-Null
}

& $NssmPath set $ServiceName AppDirectory $ProjectRoot | Out-Null
& $NssmPath set $ServiceName DisplayName "ANT COLONY Watchdog" | Out-Null
& $NssmPath set $ServiceName Description "Controleert elke 60 seconden ANT COLONY /api/status en herstart AntColony bij stale ticks." | Out-Null
& $NssmPath set $ServiceName Start SERVICE_AUTO_START | Out-Null
& $NssmPath set $ServiceName AppStdout (Join-Path $logRoot "watchdog_service.out.log") | Out-Null
& $NssmPath set $ServiceName AppStderr (Join-Path $logRoot "watchdog_service.err.log") | Out-Null
& $NssmPath set $ServiceName AppRotateFiles 1 | Out-Null
& $NssmPath set $ServiceName AppRotateOnline 1 | Out-Null
& $NssmPath set $ServiceName AppRotateBytes 10485760 | Out-Null
& $NssmPath set $ServiceName AppRestartDelay 5000 | Out-Null
& $NssmPath set $ServiceName AppThrottle 5000 | Out-Null

Write-Host ""
Write-Host "$ServiceName geinstalleerd/geconfigureerd." -ForegroundColor Green
Write-Host "Start handmatig met:"
Write-Host "  & `"$NssmPath`" start $ServiceName"
Write-Host ""
Write-Host "Logs:"
Write-Host "  C:\Trading\ANT_LOGS\watchdog.log"
Write-Host "  C:\Trading\ANT_LOGS\watchdog_service.out.log"
Write-Host "  C:\Trading\ANT_LOGS\watchdog_service.err.log"
