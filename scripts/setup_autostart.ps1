#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Registreert "ANT COLONY v2" als Windows Taakplanner taak.
.USAGE
    powershell -ExecutionPolicy Bypass -File scripts\setup_autostart.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Paden ---
$ProjectRoot  = "C:\Users\Gebruiker\ant-colony-v2"
$PythonScript = "C:\Users\Gebruiker\ant-colony-v2\scripts\start_colony.py"
$EnvFile      = "C:\Users\Gebruiker\ant-colony-v2\.env"
$LogDir       = "C:\Trading\ANT_LOGS"
$LogFile      = Join-Path $LogDir "startup.log"
$TaskName     = "ANT COLONY v2"
$UserName     = "Gebruiker"

# --- Validatie ---
if (-not (Test-Path $PythonScript)) {
    Write-Error "Python script niet gevonden: $PythonScript"
    exit 1
}

# Log directory aanmaken als die nog niet bestaat
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    Write-Host "Log directory aangemaakt: $LogDir"
}

# --- .env laden voor API keys ---
# De keys worden als omgevingsvariabelen meegegeven aan de taak via een wrapper.
$EnvBlock = ""
if (Test-Path $EnvFile) {
    $lines = Get-Content $EnvFile | Where-Object { $_ -match "^\s*[^#].*=.*" }
    foreach ($line in $lines) {
        $parts = $line -split "=", 2
        if ($parts.Count -eq 2) {
            $key   = $parts[0].Trim()
            $value = $parts[1].Trim().Trim('"').Trim("'")
            $EnvBlock += "`$env:$key = '$value'; "
        }
    }
    Write-Host ".env geladen: $($lines.Count) variabelen gevonden."
} else {
    Write-Warning ".env bestand niet gevonden op: $EnvFile  (API keys worden niet geladen)"
}

# --- Wrapper commando ---
# Start python in een nieuw (zichtbaar) venster en schrijf startup naar logfile.
$Timestamp   = '$(Get-Date -Format ''yyyy-MM-dd HH:mm:ss'')'
$LogEntry    = "Add-Content -Path '$LogFile' -Value \`"[$Timestamp] ANT COLONY v2 gestart\`"; "
$PythonCmd   = "python `"$PythonScript`""
$FullCommand = $LogEntry + $EnvBlock + $PythonCmd

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -Command `"$FullCommand`"" `
    -WorkingDirectory $ProjectRoot

# --- Trigger: bij herstart, 60 seconden wachten ---
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Trigger.Delay = "PT60S"   # ISO 8601 — 60 seconden

# --- Instellingen ---
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

# --- Principal (huidige gebruiker) ---
$Principal = New-ScheduledTaskPrincipal `
    -UserId $UserName `
    -LogonType Interactive `
    -RunLevel Highest

# --- Registreer taak (overschrijf als deze al bestaat) ---
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Bestaande taak '$TaskName' verwijderd."
}

Register-ScheduledTask `
    -TaskName  $TaskName `
    -Action    $Action `
    -Trigger   $Trigger `
    -Settings  $Settings `
    -Principal $Principal `
    -Description "Start ANT COLONY v2 trading bot na systeemherstart (60s vertraging)." `
    | Out-Null

Write-Host ""
Write-Host "Taak '$TaskName' succesvol aangemaakt." -ForegroundColor Green
Write-Host "  Actie  : python $PythonScript"
Write-Host "  Trigger: bij herstart + 60s vertraging"
Write-Host "  Log    : $LogFile"
Write-Host "  Gebruiker: $UserName"
Write-Host ""
Write-Host "Controleer via: Get-ScheduledTask -TaskName '$TaskName' | Format-List"
