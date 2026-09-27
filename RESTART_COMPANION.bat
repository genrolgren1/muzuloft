@echo off
setlocal
cd /d "%~dp0"
set "VPY=%LOCALAPPDATA%\KingdomServices\venv\Scripts\python.exe"
if not exist "%VPY%" (
 echo Run RUN_GEM_BOT.bat first.
 pause
 exit /b 1
)
"%VPY%" companion_cli.py doctor
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
