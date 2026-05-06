# Colony + Watchtower autostart installeren

Deze setup maakt drie Windows Task Scheduler taken:

- `Watchtower-Startup`: bij systeemstart en dagelijks om `00:25 CET`
- `AntColony-Startup`: bij systeemstart en dagelijks om `00:30 CET`
- `AntColony-Watchdog`: elke 5 minuten health check op `http://127.0.0.1:8000/health`

Alle taken draaien als de huidige Windows gebruiker. Het setup-script moet wel
als administrator worden uitgevoerd omdat Windows anders geen taken mag
registreren.

## 1. Installeer de taken

Open PowerShell als administrator:

```powershell
cd C:\Users\Gebruiker\ant-colony-v2\ant-colony-v2
powershell.exe -ExecutionPolicy Bypass -File scripts\setup_autostart.ps1
```

## 2. Controleer Task Scheduler

```powershell
Get-ScheduledTask -TaskName AntColony-Startup,Watchtower-Startup,AntColony-Watchdog |
  Select-Object TaskName, State
```

Je kunt de taken ook openen via:

```powershell
taskschd.msc
```

## 3. Test handmatig

Start eerst Watchtower:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\start_watchtower_auto.ps1
```

Start daarna Colony:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\start_colony_auto.ps1
```

Controleer health:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8011/health
```

## 4. Test na herstart

Herstart PC2 en controleer na inloggen:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8011/health
Get-Content C:\Trading\ANT_LOGS\colony_autostart.log -Tail 40
Get-Content C:\Trading\ANT_LOGS\watchtower_autostart.log -Tail 40
Get-Content C:\Trading\ANT_LOGS\colony_watchdog.log -Tail 40
```

## Lange stroomuitval

Als PC2 langer dan 8 uur offline was, start `start_colony_auto.ps1` met:

- `COLONY_READONLY_MODE=true`
- `ANT_COLONY_READONLY=true`
- `IBKR_PAPER_MODE=true`
- `--capital 0`

Dit voorkomt automatische handelsactiviteit tot er een menselijke check is
gedaan. Na controle kun je de normale taak opnieuw starten of de state-file
verwijderen:

```powershell
Remove-Item C:\Trading\ANT_LOGS\colony_autostart_last_seen.txt
```
