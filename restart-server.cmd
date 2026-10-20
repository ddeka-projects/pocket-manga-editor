@echo off
setlocal DisableDelayedExpansion
if not exist "%~dp0scripts\restart-server.ps1" (
  echo Restart script missing: "%~dp0scripts\restart-server.ps1"
  pause
  exit /b 1
)
if not exist "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" (
  echo Windows PowerShell was not found. The scheduled task was not changed.
  pause
  exit /b 1
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart-server.ps1" -Pause
exit /b %ERRORLEVEL%
