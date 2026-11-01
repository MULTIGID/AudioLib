@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo Python environment was not found. Run install.bat first.
    timeout /t 5 /nobreak > nul
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" main.py
exit /b 0
