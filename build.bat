@echo off
cd /d "%~dp0"

rem --- find Python: py launcher, then the usual install folder, then python ---
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo Python not found. Install it from python.org and tick "Add python.exe to PATH".
    pause
    exit /b 1
)
echo Using Python: %PY%
echo.

echo Installing libraries...
"%PY%" -m pip install customtkinter pynput pillow pystray pyinstaller
echo.

echo Closing running Skywalk (if any)...
taskkill /im Skywalk.exe /f >nul 2>nul
timeout /t 1 /nobreak >nul
echo.
echo Building Skywalk.exe...
"%PY%" -m PyInstaller --noconsole --onefile --noconfirm --name "Skywalk" --icon icon.ico --add-data "icon.ico;." --hidden-import pystray._win32 skywalk.py
if errorlevel 1 (
    echo.
    echo BUILD FAILED. Send a screenshot of this window.
    pause
    exit /b 1
)

echo.
echo Done! The program is in the dist folder.
explorer dist
pause
