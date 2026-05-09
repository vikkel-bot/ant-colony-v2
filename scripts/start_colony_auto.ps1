param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = "",
    [int]$Port = 8000,
    [string]$HostName = "127.0.0.1",
    [int]$IbPort = 7497,
    [string]$LogPath = "C:\Trading\ANT_LOGS\colony_autostart.log",
    [string]$StatePath = "C:\Trading\ANT_LOGS\colony_autostart_last_seen.txt"
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

function Resolve-ColonyRoot {
    param([string]$Root)
    try {
        $resolved = (Resolve-Path -LiteralPath $Root -ErrorAction Stop).Path
    }
    catch {
        $resolved = $Root
    }
    $leaf = Split-Path -Leaf $resolved
    $parent = Split-Path -Parent $resolved
    if ($leaf -eq "ant-colony-v2" -and (Split-Path -Leaf $parent) -eq "ant-colony-v2") {
        $candidateStartScript = Join-Path $parent "scripts\start_colony.py"
        if (Test-Path -LiteralPath $candidateStartScript) {
            Write-AutoLog "Dubbele projectroot gecorrigeerd | oud=$resolved nieuw=$parent"
            return $parent
        }
    }
    $resolved = (Resolve-Path -LiteralPath $resolved -ErrorAction Stop).Path
    return $resolved
}

function Test-Port {
    param([string]$TargetHost, [int]$TargetPort, [int]$TimeoutMs = 1000)
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $async = $client.BeginConnect($TargetHost, $TargetPort, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) {
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

function Stop-ColonyPortOwner {
    param([int]$TargetPort)
    $ownerPids = @(Get-PortProcessIds -TargetPort $TargetPort)
    foreach ($ownerPid in $ownerPids) {
        if ($ownerPid -and $ownerPid -ne $PID) {
            try {
                Write-AutoLog "Stop oude Colony instantie op poort $TargetPort | pid=$ownerPid"
                Stop-Process -Id $ownerPid -Force
            }
            catch {
                Write-AutoLog "Kon oude Colony instantie niet stoppen | pid=$ownerPid | $($_.Exception.Message)"
            }
        }
    }
}

function Prepare-ColonyPort {
    param([int]$TargetPort)
    Stop-ColonyPortOwner -TargetPort $TargetPort
    Start-Sleep -Seconds 2
    if (Test-Port -TargetHost $HostName -TargetPort $TargetPort -TimeoutMs 1000) {
        Write-AutoLog "Poort $TargetPort blijft bezet na stop-poging; Colony start geblokkeerd"
        return $false
    }
    Write-AutoLog "Poort $TargetPort vrij"
    return $true
}

function Wait-Network {
    param([int]$MaxSeconds = 60)
    $deadline = (Get-Date).AddSeconds($MaxSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port -TargetHost "1.1.1.1" -TargetPort 53 -TimeoutMs 1000) {
            Write-AutoLog "Netwerk beschikbaar"
            return $true
        }
        Start-Sleep -Seconds 2
    }
    Write-AutoLog "Netwerk niet beschikbaar binnen ${MaxSeconds}s"
    return $false
}

function Test-IbGatewayOptional {
    if (Test-Port -TargetHost $HostName -TargetPort $IbPort -TimeoutMs 1000) {
        Write-AutoLog "IB Gateway bereikbaar | ${HostName}:${IbPort}"
        return $true
    }
    Write-AutoLog "WARNING: IB Gateway niet bereikbaar | ${HostName}:${IbPort} | Colony start zonder IBKR"
    return $false
}

function Test-LongOfflineWindow {
    param([int]$Hours = 8)
    if (-not (Test-Path -LiteralPath $StatePath)) {
        return $false
    }
    try {
        $lastSeenRaw = Get-Content -LiteralPath $StatePath -Raw
        $lastSeen = [datetime]::Parse($lastSeenRaw.Trim()).ToUniversalTime()
        $offlineHours = ((Get-Date).ToUniversalTime() - $lastSeen).TotalHours
        return ($offlineHours -gt $Hours)
    }
    catch {
        return $false
    }
}

function Update-LastSeen {
    $dir = Split-Path -Parent $StatePath
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    (Get-Date).ToUniversalTime().ToString("o") | Set-Content -Path $StatePath
}

$ProjectRoot = Resolve-ColonyRoot -Root $ProjectRoot

if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}

$startScript = Join-Path $ProjectRoot "scripts\start_colony.py"
if (-not (Test-Path -LiteralPath $startScript)) {
    Write-AutoLog "start_colony.py niet gevonden: $startScript"
    exit 2
}
if (-not (Test-Path -LiteralPath $PythonPath)) {
    Write-AutoLog "Python niet gevonden: $PythonPath"
    exit 2
}

Set-Location -LiteralPath $ProjectRoot
Write-AutoLog "Colony autostart gestart | root=$ProjectRoot port=$Port"

while ($true) {
    $preflightOk = $false
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        Write-AutoLog "Preflight poging $attempt/3"
        $networkOk = Wait-Network -MaxSeconds 60
        Test-IbGatewayOptional | Out-Null
        if ($networkOk) {
            $preflightOk = $true
            break
        }
        Write-AutoLog "Preflight mislukt; nieuwe poging over 60s"
        Start-Sleep -Seconds 60
    }

    if (-not $preflightOk) {
        Write-AutoLog "Preflight definitief mislukt na 3 pogingen; script stopt zodat Task Scheduler kan herstarten"
        exit 1
    }

    if (Test-LongOfflineWindow -Hours 8) {
        $env:COLONY_READONLY_MODE = "true"
        $env:ANT_COLONY_READONLY = "true"
        $env:IBKR_PAPER_MODE = "true"
        Write-AutoLog "READONLY mode actief: systeem was langer dan 8 uur offline; menselijke check vereist"
    }
    else {
        $env:COLONY_READONLY_MODE = "false"
        $env:ANT_COLONY_READONLY = "false"
    }

    Update-LastSeen
    if (-not (Prepare-ColonyPort -TargetPort $Port)) {
        Write-AutoLog "Colony start overgeslagen; retry over 60s"
        Start-Sleep -Seconds 60
        continue
    }
    Write-AutoLog "Colony start | port=$Port"
    & $PythonPath $startScript --port $Port
    $exitCode = $LASTEXITCODE
    Write-AutoLog "Colony proces gestopt | exit_code=$exitCode | herstart over 30s"
    Update-LastSeen
    Start-Sleep -Seconds 30
}
