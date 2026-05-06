#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Configureert Windows Task Scheduler voor Colony, Watchtower en watchdog.

.USAGE
    powershell -ExecutionPolicy Bypass -File scripts\setup_autostart.ps1
#>

param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$WatchtowerRoot = "D:\wachttoren",
    [string]$UserId = "$env:USERDOMAIN\$env:USERNAME"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-File {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Bestand niet gevonden: $Path"
    }
}

function New-PowerShellAction {
    param(
        [string]$ScriptPath,
        [string]$WorkingDirectory,
        [string]$ScriptArguments = ""
    )
    $argument = "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""
    if ($ScriptArguments) {
        $argument = "$argument $ScriptArguments"
    }
    New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument $argument `
        -WorkingDirectory $WorkingDirectory
}

function Register-OrReplaceTask {
    param(
        [string]$TaskName,
        [object]$Action,
        [object[]]$Triggers,
        [string]$Description
    )

    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Bestaande taak verwijderd: $TaskName"
    }

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 5)

    $principal = New-ScheduledTaskPrincipal `
        -UserId $UserId `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Triggers `
        -Settings $settings `
        -Principal $principal `
        -Description $Description | Out-Null

    Write-Host "Taak aangemaakt: $TaskName" -ForegroundColor Green
}

$colonyAuto = Join-Path $ProjectRoot "scripts\start_colony_auto.ps1"
$watchtowerAuto = Join-Path $ProjectRoot "scripts\start_watchtower_auto.ps1"
$watchdog = Join-Path $ProjectRoot "scripts\watchdog_colony.ps1"

Assert-File $colonyAuto
Assert-File $watchtowerAuto
Assert-File $watchdog

if (-not (Test-Path -LiteralPath "C:\Trading\ANT_LOGS")) {
    New-Item -ItemType Directory -Path "C:\Trading\ANT_LOGS" -Force | Out-Null
}

$colonyStartup = New-ScheduledTaskTrigger -AtStartup
$colonyStartup.Delay = "PT90S"
$colonyDaily = New-ScheduledTaskTrigger -Daily -At "00:30"

$watchtowerStartup = New-ScheduledTaskTrigger -AtStartup
$watchtowerStartup.Delay = "PT60S"
$watchtowerDaily = New-ScheduledTaskTrigger -Daily -At "00:25"

$watchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)

Register-OrReplaceTask `
    -TaskName "AntColony-Startup" `
    -Action (New-PowerShellAction -ScriptPath $colonyAuto -WorkingDirectory $ProjectRoot) `
    -Triggers @($colonyStartup, $colonyDaily) `
    -Description "Start ANT COLONY v2 bij systeemstart en dagelijks om 00:30 CET."

Register-OrReplaceTask `
    -TaskName "Watchtower-Startup" `
    -Action (New-PowerShellAction `
        -ScriptPath $watchtowerAuto `
        -WorkingDirectory $ProjectRoot `
        -ScriptArguments "-WatchtowerRoot `"$WatchtowerRoot`"") `
    -Triggers @($watchtowerStartup, $watchtowerDaily) `
    -Description "Start Watchtower bij systeemstart en dagelijks om 00:25 CET."

Register-OrReplaceTask `
    -TaskName "AntColony-Watchdog" `
    -Action (New-PowerShellAction -ScriptPath $watchdog -WorkingDirectory $ProjectRoot) `
    -Triggers @($watchdogTrigger) `
    -Description "Controleert elke 5 minuten /health en start Colony opnieuw bij uitval."

Write-Host ""
Write-Host "Autostart configuratie klaar." -ForegroundColor Green
Write-Host "Project root   : $ProjectRoot"
Write-Host "Watchtower root: $WatchtowerRoot"
Write-Host "Gebruiker      : $UserId"
Write-Host ""
Write-Host "Controleer met:"
Write-Host "  Get-ScheduledTask -TaskName AntColony-Startup,Watchtower-Startup,AntColony-Watchdog"
