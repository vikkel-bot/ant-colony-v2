param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$HealthUrl = "http://127.0.0.1:8000/health",
    [string]$LogPath = "C:\Trading\ANT_LOGS\colony_watchdog.log"
)

$ErrorActionPreference = "Stop"

function Rotate-Log {
    param([string]$Path, [int64]$MaxBytes = 10MB)
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path
        if ($item.Length -ge $MaxBytes) {
            $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
            Move-Item -LiteralPath $Path -Destination "$Path.$stamp" -Force
        }
    }
}

function Write-WatchdogLog {
    param([string]$Message)
    $dir = Split-Path -Parent $LogPath
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    Rotate-Log -Path $LogPath
    $line = "{0} {1}" -f (Get-Date).ToString("s"), $Message
    Add-Content -Path $LogPath -Value $line
    Write-Host $line
}

try {
    $health = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 5
    if ($health.status -eq "ok") {
        Write-WatchdogLog "Colony health OK | uptime=$($health.uptime_seconds)s agents=$($health.agents)"
        exit 0
    }
    Write-WatchdogLog "Colony health onverwacht antwoord: $($health | ConvertTo-Json -Compress)"
}
catch {
    Write-WatchdogLog "Colony health faalt: $($_.Exception.Message)"
}

$autoScript = Join-Path $ProjectRoot "scripts\start_colony_auto.ps1"
if (-not (Test-Path -LiteralPath $autoScript)) {
    Write-WatchdogLog "start_colony_auto.ps1 niet gevonden: $autoScript"
    exit 2
}

$running = Get-CimInstance Win32_Process |
    Where-Object {
        $_.CommandLine -like "*start_colony_auto.ps1*" -and
        $_.ProcessId -ne $PID
    } |
    Select-Object -First 1

if ($running) {
    Write-WatchdogLog "Colony autostart draait al | pid=$($running.ProcessId)"
    exit 0
}

Write-WatchdogLog "Start Colony autostart via watchdog"
Start-Process -FilePath "powershell.exe" `
    -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$autoScript`"" `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden
