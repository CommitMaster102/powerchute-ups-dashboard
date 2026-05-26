@echo off
REM PCSS battery tray icon — launches with no console window.
REM Uses pythonw.exe so it stays in the system tray silently.
REM
REM First-time setup:
REM   python -m venv .venv
REM   .venv\Scripts\pip install -r requirements.txt
REM
REM Then double-click this file. On first run it will create credentials.txt;
REM edit that file with your PCSS username/password and re-run.
REM
REM Logs go to output\tray_status.log

cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERROR] Virtual environment not found at .venv\
    echo Run setup first:
    echo    python -m venv .venv
    echo    .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" tray_status.py
