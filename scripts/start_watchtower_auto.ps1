param(
    [string]$WatchtowerRoot = "D:\wachttoren",
    [int]$Port = 8011,
    [string]$HealthUrl = "http://127.0.0.1:8011/health",
    [string]$LogPath = "C:\Trading\ANT_LOGS\watchtower_autostart.log"
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

function Write-AutoLog {
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

function Get-PortProcessIds {
    param([int]$TargetPort)
    try {
        Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique
    }
    catch {
        @()
    }
}

function Stop-PortOwner {
    param([int]$TargetPort)
    $pids = @(Get-PortProcessIds -TargetPort $TargetPort)
    foreach ($ownerPid in $pids) {
        if ($ownerPid -and $ownerPid -ne $PID) {
            try {
                Write-AutoLog "Stop oud Watchtower proces op poort $TargetPort | pid=$ownerPid"
                Stop-Process -Id $ownerPid -Force
            }
            catch {
                Write-AutoLog "Kon oud proces niet stoppen | pid=$ownerPid | $($_.Exception.Message)"
            }
        }
    }
}

function Wait-Health {
    param([int]$MaxSeconds = 30)
    $deadline = (Get-Date).AddSeconds($MaxSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 5
            if ($response) {
                Write-AutoLog "Watchtower health OK"
                return $true
            }
        }
        catch {
            Start-Sleep -Seconds 2
        }
    }
    Write-AutoLog "Watchtower health reageert niet binnen ${MaxSeconds}s"
    return $false
}

if (-not (Test-Path -LiteralPath $WatchtowerRoot)) {
    Write-AutoLog "Watchtower root niet gevonden: $WatchtowerRoot"
    exit 2
}

$bat = Join-Path $WatchtowerRoot "START_WATCHTOWER.bat"
if (-not (Test-Path -LiteralPath $bat)) {
    Write-AutoLog "START_WATCHTOWER.bat niet gevonden: $bat"
    exit 2
}

Write-AutoLog "Watchtower autostart gestart | root=$WatchtowerRoot port=$Port"

while ($true) {
    $started = $false
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        Write-AutoLog "Watchtower start poging $attempt/3"
        Stop-PortOwner -TargetPort $Port
        Start-Sleep -Seconds 2

        Set-Location -LiteralPath $WatchtowerRoot
        $proc = Start-Process -FilePath "cmd.exe" `
            -ArgumentList "/c", "`"$bat`"" `
            -WorkingDirectory $WatchtowerRoot `
            -PassThru `
            -WindowStyle Hidden

        Write-AutoLog "Watchtower proces gestart | pid=$($proc.Id)"
        if (Wait-Health -MaxSeconds 30) {
            $started = $true
            break
        }

        Write-AutoLog "Watchtower startpoging mislukt; stop proces en retry over 60s"
        try {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
        catch {}
        Start-Sleep -Seconds 60
    }

    if (-not $started) {
        Write-AutoLog "Watchtower definitief niet gestart na 3 pogingen; script stopt zodat Task Scheduler kan herstarten"
        exit 1
    }

    while ($true) {
        Start-Sleep -Seconds 30
        if (-not (Wait-Health -MaxSeconds 5)) {
            Write-AutoLog "Watchtower health verloren; herstart over 60s"
            Stop-PortOwner -TargetPort $Port
            Start-Sleep -Seconds 60
            break
        }
    }
}
