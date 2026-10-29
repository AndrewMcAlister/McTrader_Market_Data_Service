@echo off
REM Wrapper for scheduling. Use run.ps1 directly from PowerShell in VS Code; use this from
REM Task Scheduler, cmd, or anything that is not a policy-tolerant shell.
REM
REM Task Scheduler runs with the machine's execution policy (Restricted here), so a task that
REM called run.ps1 directly would fail before Python ever started -- and a scheduled job that
REM fails silently for a week is worse than one that fails loudly once.
REM
REM   run.cmd -Daily
REM   run.cmd -Daily -Since 2026-10-15
REM   run.cmd -Test

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
exit /b %ERRORLEVEL%
