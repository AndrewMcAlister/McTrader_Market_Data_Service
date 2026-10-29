@echo off
setlocal
REM ============================================================================
REM  Stop McTrader_Market_Data_Service.
REM ============================================================================
REM
REM  Stops whatever holds port 8003 and stops there -- it does not start the
REM  service again. Nothing brings it back until you run start.bat, because the
REM  scheduled task is boot-only.
REM
REM  It should not need elevation. The task is registered at the default run
REM  level; if it was ever re-registered with -RunLevel Highest, the service
REM  runs with an elevated token and this will be refused with "Access is
REM  denied" -- which is reported, not swallowed.
REM
REM  Pauses at the end so a double-click gives you time to read the result.
REM  For scripted use, call scripts\Ensure-MarketDataService.ps1 -Stop directly.
REM ============================================================================

echo.
echo   Stopping McTrader_Market_Data_Service...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Ensure-MarketDataService.ps1" -Stop
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
    echo   Stopped, or nothing was running.
) else (
    echo   Failed with exit code %RC%.
    echo   Launcher log:    %~dp0logs\ensure-history.txt
)
echo.
pause
endlocal & exit /b %RC%
