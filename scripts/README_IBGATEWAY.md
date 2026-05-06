# IB Gateway nachtelijke auto-restart

IB Gateway verbreekt rond 23:45 CET automatisch de sessie. Zet daarom de
ingebouwde auto-restart aan, zodat de equities-ants bij marktopening weer data
kunnen ophalen.

## Handmatige IB Gateway instelling

1. Open IB Gateway.
2. Ga naar `Configure` -> `Settings` -> `Auto restart`.
3. Zet auto-restart aan voor de nachtelijke disconnect.
4. Stel de herstarttijd in op `00:15 CET`.
5. Controleer dat `Enable ActiveX and Socket Clients` actief blijft in de API settings.

## Backup-script

Als de ingebouwde auto-restart faalt, kan de Colony het backup-script starten:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\restart_ibgateway.ps1
```

Het script:

- controleert poort `7497` op `127.0.0.1`;
- start standaard `C:\Jts\ibgateway\1030\ibgateway.exe` als de poort dicht is;
- zoekt automatisch naar `ibgateway.exe` onder `C:\Jts\ibgateway` als het
  standaardpad niet bestaat;
- wacht maximaal 60 seconden tot de poort bereikbaar is;
- schrijft naar `C:\Trading\ANT_LOGS\ibgateway_restart.log`.

Optionele parameters:

```powershell
powershell.exe -ExecutionPolicy Bypass -File scripts\restart_ibgateway.ps1 `
  -HostName 127.0.0.1 `
  -Port 7497 `
  -GatewayPath "C:\Jts\ibgateway\1030\ibgateway.exe" `
  -WaitSeconds 60
```

De IBKRAdapter start dit script best-effort wanneer een connection-refused of
verbroken socket wordt gezien. Als IB Gateway daarna nog niet bereikbaar is,
blijft yfinance fallback actief zodat de Colony tick niet vastloopt.
