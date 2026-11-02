@echo off
setlocal
cd /d "%~dp0"
title Building Music-Discord.exe

where python >nul 2>nul
if errorlevel 1 (
  echo [!] Python not found. Install it from python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

echo [1/3] Installing dependencies...
python -m pip install --quiet --upgrade pip pyinstaller -r requirements.txt
if errorlevel 1 goto fail

echo [2/3] Building exe...
python -m PyInstaller --noconfirm --clean --onefile --noconsole --name "Music-Discord" --icon icon.ico --add-data "icon.ico;." --collect-all winrt --hidden-import winrt.windows.media.control --hidden-import pystray._win32 --hidden-import winrt.windows.foundation --hidden-import winrt.windows.foundation.collections --hidden-import winrt.windows.storage.streams --hidden-import winrt.windows.security.cryptography --hidden-import lz4.block --log-level WARN vlc_discord.py
if errorlevel 1 goto fail

echo [3/3] Cleaning up...
copy /y "dist\Music-Discord.exe" "Music-Discord.exe" >nul
rmdir /s /q build dist >nul 2>nul
del /q "Music-Discord.spec" >nul 2>nul

echo.
echo Done: Music-Discord.exe is next to this file.
explorer /select,"%~dp0Music-Discord.exe"
timeout /t 5 >nul
exit /b 0

:fail
echo.
echo [!] Build failed - send me the text above.
pause
exit /b 1
