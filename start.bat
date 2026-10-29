@echo off
setlocal
REM ============================================================================
REM  Start (or restart) McTrader_Market_Data_Service on port 8003.
REM ============================================================================
REM
REM  This is the same call the scheduled task makes, so running it by hand
REM  exercises the real path rather than a parallel one.
REM
REM  The launcher stops whatever holds port 8003 and then starts it again. That
REM  is deliberate: it is what makes a plain run a RESTART, so the code on disk
REM  is picked up every time without a separate stop step.
REM
REM  The EODHD key comes from the per-user Windows credential store, which is why
REM  this runs as you rather than as a service account. The whole point of the
REM  account choice is that a LocalSystem process has its own empty vault.
REM
REM  It should not need elevation. The task is registered at the default run
REM  level; if it was ever re-registered with -RunLevel Highest, the service
REM  runs with an elevated token and only an elevated prompt can stop it.
REM
REM  Pauses at the end so a double-click gives you time to read the result.
REM  For scripted use, call scripts\Ensure-MarketDataService.ps1 directly.
REM ============================================================================

echo.
echo   Starting McTrader_Market_Data_Service on port 8003...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Ensure-MarketDataService.ps1"
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
    echo   Started.  Check: http://127.0.0.1:8003/healthz  ^(key_source should be keyring^)
    echo   Log:             %~dp0logs\data_service.log
) else (
    echo   Failed with exit code %RC%.
    echo   Launcher log:    %~dp0logs\ensure-history.txt
    echo   Service log:     %~dp0logs\data_service.log
)
echo.
pause
endlocal & exit /b %RC%
