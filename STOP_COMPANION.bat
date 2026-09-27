@echo off
setlocal
cd /d "%~dp0"
set "VPY=%LOCALAPPDATA%\KingdomServices\venv\Scripts\python.exe"
if not exist "%VPY%" exit /b 1
"%VPY%" companion_cli.py stop
"%VPY%" ldplayer_backend.py --stop-frida-test
