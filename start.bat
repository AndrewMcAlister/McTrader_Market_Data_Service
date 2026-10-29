@echo off
setlocal
REM ============================================================================
REM  Start (or restart) McTrader_Market_Data_Service on port 8003.
REM ============================================================================
REM
REM  This does not touch the process directly. It asks Task Scheduler to run the
REM  launcher, because the service lives in SESSION 0 (that is where Task
REM  Scheduler starts it) and a process in your interactive session cannot
REM  terminate it -- "Access is denied", at any run level. Running the launcher
REM  from inside the task puts it back in session 0, where the kill succeeds.
REM
REM  Starting a task that holds a stored password needs no elevation, so this is
REM  a plain double-click.
REM
REM  The EODHD key is read from your per-user credential store, which is why the
REM  task runs as you rather than as a service account: LOCAL SYSTEM has its own,
REM  empty vault.
REM
REM  For scripted use, call: schtasks /run /tn "McTrader_Market_Data_Service_KeepAlive"
REM ============================================================================

set TASK=McTrader_Market_Data_Service_KeepAlive
set PORT=8003

echo.
echo   Starting McTrader_Market_Data_Service...
echo.

schtasks /run /tn "%TASK%" >nul 2>&1
if errorlevel 1 (
    echo   Could not start the scheduled task "%TASK%".
    echo   It may not be registered. From an elevated prompt:
    echo       .\install-task.ps1 -Install
    echo.
    pause
    endlocal & exit /b 1
)

REM ping, not timeout: timeout refuses to run without a console
ping -n 9 127.0.0.1 >nul

echo   What the launcher did:
powershell -NoProfile -Command "Get-Content '%~dp0logs\ensure-history.txt' -Tail 5 | ForEach-Object { '     ' + $_ }" 2>nul

echo.
powershell -NoProfile -Command "try { $h = Invoke-RestMethod 'http://127.0.0.1:%PORT%/healthz' -TimeoutSec 10; Write-Host ('   healthz: ' + $h.status + '   db=' + $h.database + '   key=' + $h.key_source) } catch { Write-Host '   healthz: NOT ANSWERING -- see the service log below' }"

echo.
echo   Service log: %~dp0logs\data_service.log
echo.
pause
endlocal & exit /b 0
