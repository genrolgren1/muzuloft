@echo off
setlocal
cd /d "%~dp0"
set "VPY=%LOCALAPPDATA%\KingdomServices\venv\Scripts\python.exe"
if not exist "%VPY%" set "VPY=py -3"
"%VPY%" "%~dp0BOT_SELF_TEST.py"
echo.
pause
