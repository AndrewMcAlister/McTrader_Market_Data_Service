@echo off
REM Wrapper so the task installer runs from an elevated cmd prompt without touching the machine's
REM execution policy (Restricted here, which refuses .ps1 files).
REM
REM   install-task.cmd -Status
REM   install-task.cmd -Install     (from an ELEVATED prompt; it prompts for your password once)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-task.ps1" %*
exit /b %ERRORLEVEL%
