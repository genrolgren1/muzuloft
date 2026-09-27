@echo off
setlocal
cd /d "%~dp0"
title GemOps - Return Main LDPlayer To RoK
set "VPY=%LOCALAPPDATA%\KingdomServices\venv\Scripts\python.exe"
if not exist "%VPY%" set "VPY=py -3"

echo Closing isolated Frida Test Lab...
"%VPY%" "%~dp0ldplayer_backend.py" --stop-frida-test

for /f "usebackq delims=" %%I in (`"%VPY%" "%~dp0ldplayer_backend.py" --select-main`) do set "IDX=%%I"
if not defined IDX (
  echo Could not find the main LDPlayer.
  pause
  exit /b 1
)

echo Main LDPlayer index: %IDX%
echo Launching Rise of Kingdoms...
"%VPY%" "%~dp0ldplayer_backend.py" --launch-rok --index %IDX%
if errorlevel 1 (
  echo Failed to launch RoK.
  pause
  exit /b 1
)

echo.
echo Ready. Refresh the GemOps web page.
pause
