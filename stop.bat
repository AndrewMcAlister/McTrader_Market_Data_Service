@echo off
setlocal
REM ============================================================================
REM  Stop McTrader_Market_Data_Service.
REM ============================================================================
REM
REM  Asks the service to stop itself, via POST /admin/shutdown.
REM
REM  It does not try to kill the process, and that is not laziness: the service
REM  runs in SESSION 0 (Task Scheduler started it) while your shell is session 2,
REM  and a session-2 process cannot terminate a session-0 process -- "Access is
REM  denied", at any run level. A process can always end itself, so the service
REM  is asked to.
REM
REM  Nothing restarts it afterwards: the keep-alive task is boot-only.
REM
REM  If this reports "no answer", the process is not running, or it is wedged and
REM  cannot serve HTTP. A wedged process needs the task route instead:
REM      schtasks /run /tn "McTrader_Market_Data_Service_KeepAlive_Stop"
REM  That task is created by install-task.ps1 -Install. It is not needed for the
REM  normal case and stop.bat does not depend on it.
REM
REM  For scripted use:
REM      curl -X POST http://127.0.0.1:8003/admin/shutdown
REM ============================================================================

set PORT=8003

echo.
echo   Stopping McTrader_Market_Data_Service...
echo.

powershell -NoProfile -Command "try { Invoke-RestMethod -Method Post 'http://127.0.0.1:%PORT%/admin/shutdown' -TimeoutSec 10 -ErrorAction Stop | Out-Null; Write-Host '   shutdown requested' } catch { Write-Host '   no answer on %PORT% -- not running, or wedged' }"

REM up to 15 seconds for the port to close
powershell -NoProfile -Command "$deadline = (Get-Date).AddSeconds(15); while ((Get-Date) -lt $deadline) { if (-not (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue)) { break }; Start-Sleep -Milliseconds 500 }; $c = Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue; if ($c) { Write-Host ('   STILL LISTENING on %PORT% (pid ' + $c[0].OwningProcess + ') -- a wedged process needs the task route') } else { Write-Host '   stopped -- nothing is listening on %PORT%' }"

echo.
echo   Start it again with start.bat
echo.
pause
endlocal & exit /b 0
