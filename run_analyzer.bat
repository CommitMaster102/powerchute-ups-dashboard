@echo off
REM PowerChute UPS analyzer launcher
REM Double-click this file to run the analysis and open the dashboard.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found at .venv\
    echo Run setup first:
    echo    python -m venv .venv
    echo    .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

echo Running UPS analyzer...
echo.
".venv\Scripts\python.exe" analyze_ups.py

echo.
echo Done. Press any key to close.
pause >nul
