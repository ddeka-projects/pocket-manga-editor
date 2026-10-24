@echo off
rem Double-click to export one manga's selected pages. Installed by Pocket Manga Editor.
setlocal
where py >NUL 2>NUL
if not errorlevel 1 (
    py -3 "%~dp0_exporter.py"
) else (
    python "%~dp0_exporter.py"
)
echo.
pause
