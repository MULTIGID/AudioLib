@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Python environment was not found. Run install.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check pyinstaller
if errorlevel 1 goto :error

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --noupx --name AudioLib --icon "assets\audiolib_icon.ico" --add-data "assets\audiolib_icon.png;assets" --add-data "assets\audiolib_icon.ico;assets" --add-data "assets\favorites_icon.png;assets" --add-data "assets\favorites_icon.svg;assets" --version-file version_info.txt --additional-hooks-dir build_hooks --exclude-module PIL.AvifImagePlugin --exclude-module PIL.ImageCms --exclude-module PIL.ImageMath --exclude-module PIL._imagingcms --exclude-module PIL._imagingmath --collect-all pystray --collect-all miniaudio --collect-all cffi --hidden-import _cffi_backend main.py
if errorlevel 1 goto :error

if exist "data" (
    xcopy "data" "dist\AudioLib\data\" /E /I /Y > nul
    if errorlevel 1 goto :error
    if exist "dist\AudioLib\data\audio_routing.log" del /Q "dist\AudioLib\data\audio_routing.log"
    if exist "dist\AudioLib\data\audio_routing_state.json" del /Q "dist\AudioLib\data\audio_routing_state.json"
    if exist "dist\AudioLib\data\app_settings.json" del /Q "dist\AudioLib\data\app_settings.json"
    rem Device endpoint IDs are unique to each PC. Let a new user choose a microphone.
    if exist "dist\AudioLib\data\audio_device_settings.json" del /Q "dist\AudioLib\data\audio_device_settings.json"
    rem Favorites are personal and must not be included in distributed builds.
    if exist "dist\AudioLib\data\library.sqlite3" (
        ".venv\Scripts\python.exe" -c "import sqlite3; p=r'dist\AudioLib\data\library.sqlite3'; c=sqlite3.connect(p); exists=c.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',('table','favorites')).fetchone(); c.execute('DELETE FROM favorites') if exists else None; c.commit(); c.close()"
        if errorlevel 1 goto :error
    )
)

echo Build completed: dist\AudioLib\AudioLib.exe
echo The data folder with your library was copied into the build.
pause
exit /b 0

:error
echo Build failed.
pause
exit /b 1
