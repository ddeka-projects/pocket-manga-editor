@echo off
rem Installed by Pocket Manga Editor; the server fills in its Python environment.
setlocal DisableDelayedExpansion
chcp 65001 >NUL
set "splitterPython=@POCKET_PYTHON@"
if not exist "%splitterPython%" (
    echo Splitter Python not found: "%splitterPython%"
    echo Run scripts\bootstrap-windows.ps1, then restart-server.cmd in the repository.
    pause
    exit /b 1
)
"%splitterPython%" "%~dp0_splitter.py"
set "splitterExit=%ERRORLEVEL%"
echo.
pause
exit /b %splitterExit%
