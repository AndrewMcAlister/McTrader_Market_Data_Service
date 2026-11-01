@echo off
setlocal
REM ============================================================================
REM  Start (or restart) McTrader_Market_Data_Service on port 8003.
REM ============================================================================
REM
REM  This is the SCM service, not the old Task Scheduler launcher. The keep-alive
REM  task was removed when the service took over (install-service.ps1 -TakeOver),
REM  so "schtasks /run" no longer has anything to run.
REM
REM  WHAT IS RUNNING
REM
REM  NSSM hosts ".venv\python.exe -m data_service.service" as the service
REM  McTrader_Market_Data_Service, starts it at boot, orders it after SQL Server,
REM  and restarts the child if it exits. The service runs as you, not LocalSystem,
REM  because the EODHD key lives in your per-user Credential Manager -- healthz
REM  reports key_source=keyring when that is working.
REM
REM  Elevation: controlling any Windows service needs SERVICE_START, which an
REM  ordinary shell does not have, so this re-runs itself behind one UAC prompt.
REM
REM  For scripted use: .\install-service.ps1 -Start   (elevated)
REM ============================================================================

set SVC=McTrader_Market_Data_Service
set PORT=8003

net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Starting a Windows service needs an elevated shell -- asking for UAC now.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b 0
)

echo.
echo   Starting %SVC%...
sc start %SVC% >nul
powershell -NoProfile -Command "$s='%SVC%'; for ($i=0; $i -lt 40; $i++) { Start-Sleep -Milliseconds 500; if ((Get-Service $s).Status -eq 'Running') { break } }; Write-Host ('   state: ' + (Get-Service $s).Status)"

echo.
echo   healthz:
powershell -NoProfile -Command "try { $h = Invoke-RestMethod 'http://127.0.0.1:%PORT%/healthz' -TimeoutSec 10; Write-Host ('   ' + $h.status + '   db=' + $h.database + '   key=' + $h.key_source) } catch { Write-Host '   NOT ANSWERING -- see the log below and logs\nssm-stderr.log' }"

echo.
echo   Service log: %~dp0logs\data_service.log
echo.
pause
endlocal & exit /b 0
