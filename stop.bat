@echo off
setlocal
REM ============================================================================
REM  Stop McTrader_Market_Data_Service.
REM ============================================================================
REM
REM  THIS STOPS THE SERVICE, NOT THE PROCESS.
REM
REM  POST /admin/shutdown is no longer how this is done. NSSM is installed with
REM  "AppExit Default Restart", so a child that exits -- even cleanly, even on
REM  purpose -- is started again five seconds later. Only a service stop tells
REM  the supervisor to stay down.
REM
REM  WHY THIS ASKS FOR ELEVATION
REM
REM  Controlling a Windows service needs SERVICE_STOP, which the filtered token
REM  of an ordinary shell does not have: nssm itself answers "OpenService():
REM  Access is denied" even though the account is an administrator. So this
REM  re-runs itself behind one UAC prompt. Nothing else in the project needs it.
REM
REM  If the child is wedged and the service will not stop, the process can still
REM  end itself -- that is what /admin/shutdown is for:
REM      curl -X POST http://127.0.0.1:8003/admin/shutdown
REM  NSSM restarts it a few seconds later, which is usually the fix.
REM
REM  For scripted use: .\install-service.ps1 -Stop   (elevated)
REM ============================================================================

set SVC=McTrader_Market_Data_Service
set PORT=8003

net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Stopping a Windows service needs an elevated shell -- asking for UAC now.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b 0
)

echo.
echo   Stopping %SVC%...
sc stop %SVC% >nul
powershell -NoProfile -Command "$s='%SVC%'; for ($i=0; $i -lt 24; $i++) { Start-Sleep -Milliseconds 500; if ((Get-Service $s).Status -eq 'Stopped') { break } }; $svc = Get-Service $s; $listen = [bool](Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue); Write-Host ('   state: ' + $svc.Status + '   listening on %PORT%: ' + $listen)"

echo.
echo   Start it again with start.bat
echo.
pause
endlocal & exit /b 0
