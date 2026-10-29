@echo off
REM Wrapper so the installer runs without changing the machine's execution policy.
REM
REM PowerShell's default policy on this machine is Restricted, which refuses .ps1 files outright.
REM Rather than change a security setting for the whole account, this passes -ExecutionPolicy
REM Bypass for the duration of this one process. Same arguments as the .ps1.
REM
REM   install-service.cmd -Status
REM   install-service.cmd -Install     (from an elevated prompt)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-service.ps1" %*
exit /b %ERRORLEVEL%
