@echo off
setlocal
cd /d "%~dp0"
set "VPY=%LOCALAPPDATA%\KingdomServices\venv\Scripts\python.exe"
if not exist "%VPY%" exit /b 1
"%VPY%" -m compileall -q .
if errorlevel 1 exit /b 1
"%VPY%" BOT_SELF_TEST.py
if errorlevel 1 exit /b 1
"%VPY%" -m unittest discover -s tests -p "test_*.py" -v
set "RC=%ERRORLEVEL%"
pause
exit /b %RC%
