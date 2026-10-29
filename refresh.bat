@echo off
setlocal
REM ============================================================================
REM  Refresh the market data service's daily bars.
REM ============================================================================
REM
REM  Calls POST /refresh on the running service. Measured: about 30 seconds and
REM  100 vendor units for a full session plus the ATR rebuild.
REM
REM  It is idempotent and it checks first: if a bulk pull is already recorded for
REM  today, this answers "already-fresh" in milliseconds and spends nothing.
REM  Use -Force to pull again anyway:
REM
REM      refresh.bat /force
REM
REM  The scheduled task runs this daily after the US close has settled. Nothing
REM  needs elevation -- it is an HTTP call to a service already running as you.
REM ============================================================================

set FORCE=
if /i "%~1"=="/force" set FORCE=-Force
if /i "%~1"=="-force" set FORCE=-Force

echo.
echo   Refreshing DailySnapshot%FORCE% ...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Refresh-DailyBars.ps1" %FORCE%
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
    echo   Done. Ledger: http://127.0.0.1:8003/stats  ^(usage_today^)
    echo   History:     %~dp0logs\refresh-history.txt
) else (
    echo   Failed with exit code %RC%.
    echo   History:     %~dp0logs\refresh-history.txt
    echo   Service log: %~dp0logs\data_service.log
)
echo.
pause
endlocal & exit /b %RC%
