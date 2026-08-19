@echo off
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build-windows.ps1" %*
set "BUILD_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%BUILD_EXIT_CODE%"=="0" (
    echo Windows packaging failed with exit code %BUILD_EXIT_CODE%.
) else (
    echo Windows packaging completed successfully.
)
echo Press any key to close this window.
pause >nul

exit /b %BUILD_EXIT_CODE%
