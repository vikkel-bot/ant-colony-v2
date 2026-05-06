param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 7497,
    [string]$GatewayPath = "",
    [int]$WaitSeconds = 60,
    [string]$LogPath = "C:\Trading\ANT_LOGS\ibgateway_restart.log"
)

$ErrorActionPreference = "Stop"

function Write-IbgLog {
    param([string]$Message)
    $timestamp = (Get-Date).ToString("s")
    $line = "$timestamp $Message"
    $logDir = Split-Path -Parent $LogPath
    if ($logDir -and -not (Test-Path -LiteralPath $logDir)) {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }
    Add-Content -Path $LogPath -Value $line
    Write-Host $line
}

function Test-IbgPort {
    param(
        [string]$TargetHost,
        [int]$TargetPort,
        [int]$TimeoutMs = 1000
    )
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect($TargetHost, $TargetPort, $null, $null)
        $ok = $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)
        if (-not $ok) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Find-IbgatewayExe {
    if ($GatewayPath -and (Test-Path -LiteralPath $GatewayPath)) {
        return $GatewayPath
    }

    $defaultPath = "C:\Jts\ibgateway\1030\ibgateway.exe"
    if (Test-Path -LiteralPath $defaultPath) {
        return $defaultPath
    }

    $basePath = "C:\Jts\ibgateway"
    if (Test-Path -LiteralPath $basePath) {
        $candidate = Get-ChildItem -Path $basePath -Filter "ibgateway.exe" -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($candidate) {
            return $candidate.FullName
        }
    }

    return $null
}

Write-IbgLog "IB Gateway restart check gestart | host=$HostName port=$Port"

if (Test-IbgPort -TargetHost $HostName -TargetPort $Port) {
    Write-IbgLog "IB Gateway poort is al bereikbaar | host=$HostName port=$Port"
    exit 0
}

$exe = Find-IbgatewayExe
if (-not $exe) {
    Write-IbgLog "IB Gateway executable niet gevonden"
    exit 2
}

Write-IbgLog "IB Gateway niet bereikbaar; start executable | path=$exe"
Start-Process -FilePath $exe -WindowStyle Hidden

$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if (Test-IbgPort -TargetHost $HostName -TargetPort $Port) {
        Write-IbgLog "IB Gateway poort bereikbaar na herstart | host=$HostName port=$Port"
        exit 0
    }
}

Write-IbgLog "IB Gateway poort niet bereikbaar binnen ${WaitSeconds}s"
exit 1
