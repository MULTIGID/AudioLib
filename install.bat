@echo off
chcp 65001 > nul
cd /d "%~dp0"

set "PYTHON_CMD="
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
    "%LocalAppData%\Programs\Python\Python312\python.exe" -c "import tkinter" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD="%LocalAppData%\Programs\Python\Python312\python.exe""
)
if not defined PYTHON_CMD (
    py -3 -c "import tkinter" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)
if not defined PYTHON_CMD (
    python -c "import tkinter" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo Python не найден.
    echo Установите Python 3.11 или новее с сайта https://www.python.org/downloads/
    echo При установке отметьте пункт "Add Python to PATH".
    pause
    exit /b 1
)

echo Создание локального окружения...
%PYTHON_CMD% -m venv .venv
if errorlevel 1 goto :error

echo Установка компонентов...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo Настройка восстановления аудио после аварийного завершения...
".venv\Scripts\python.exe" main.py --install-recovery-startup
if errorlevel 1 goto :error

echo.
echo Готово. Запустите программу файлом launch.bat
pause
exit /b 0

:error
echo.
echo Установка завершилась с ошибкой.
pause
exit /b 1
